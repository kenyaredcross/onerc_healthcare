# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""R3 data capture (Phase 3): estimated date of birth, attendance, outcome dialogs and backfill.

Requirement: Phase 3 user decisions 2026-11-13 (exact DOB or estimated age; first attendance in the
calendar year with reception / HIS overrides; referred in; visit outcome), BRD FR-29..31, SDD §5.8.
Expected values are worked out by hand in each test.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_years, getdate, random_string, today

from onerc_healthcare.clinical import referral as ref
from onerc_healthcare.constants import (
	ATTENDANCE_BASIS_HIS,
	ATTENDANCE_BASIS_RECEPTION,
	ATTENDANCE_BASIS_RULE,
	ATTENDANCE_NEW,
	ATTENDANCE_REVISIT,
	DEPT_MENTAL_HEALTH,
	DEPT_OUTPATIENT,
	OUTCOME_ADMITTED,
	OUTCOME_DIED,
	OUTCOME_DISCHARGED,
	OUTCOME_LEFT,
	OUTCOME_NOT_APPLICABLE,
	OUTCOME_REFERRED_INTERNAL,
	OUTCOME_REFERRED_OUT,
	REFERRAL_FACILITY_DOCTYPE,
	VISIT_DOCTYPE,
)
from onerc_healthcare.constants import (
	PATIENT_DOB_ESTIMATED_FIELD as FLAG,
)
from onerc_healthcare.constants import (
	PATIENT_DOB_ESTIMATED_ON_FIELD as ON,
)
from onerc_healthcare.constants import (
	PATIENT_ESTIMATED_AGE_MONTHS_FIELD as MONTHS,
)
from onerc_healthcare.constants import (
	PATIENT_ESTIMATED_AGE_YEARS_FIELD as YEARS,
)
from onerc_healthcare.registration import dob
from onerc_healthcare.registration.similarity import find_similar
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import add_diagnosis, as_user, routed_visit, start, users
from onerc_healthcare.tests.factories import get_facility, make_patient
from onerc_healthcare.tests.visit_factories import make_visit
from onerc_healthcare.visit import attendance, engine, outcome

SETTINGS = "EMR Registration Settings"
OVERRIDE_FIELD = "allow_reception_attendance_override"
FACILITY_NAME = "T-Lodwar County Referral Hospital"


def estimated_patient(years: int = 0, months: int = 0, **kw):
	return make_patient(dob=None, **{YEARS: years, MONTHS: months}, **kw)


def close(visit: str, reason: str = "Other", user: str | None = None, **kw):
	if user:
		with as_user(user):
			return engine.apply_action(visit, "close_visit", closure_reason=reason, comment="T- closed", **kw)
	return engine.transition(
		visit, "close_visit", closure_reason=reason, comment="T- closed", system=True, **kw
	)


def visit_row(visit: str, *fields):
	return frappe.db.get_value(VISIT_DOCTYPE, visit, list(fields), as_dict=True)


def referral_facility() -> str:
	if not frappe.db.exists(REFERRAL_FACILITY_DOCTYPE, FACILITY_NAME):
		frappe.get_doc(
			{
				"doctype": REFERRAL_FACILITY_DOCTYPE,
				"facility_name": FACILITY_NAME,
				"facility_type": "Hospital",
				"county": "Turkana",
			}
		).insert(ignore_permissions=True)
	return frappe.db.get_value(REFERRAL_FACILITY_DOCTYPE, {"facility_name": FACILITY_NAME}, "name")


# --------------------------------------------------------------------------- DOB estimate


class TestDobEstimateMath(EMRTestCase):
	def test_estimate_convention(self):
		"""Estimate date minus years and months, same day of month, clamped at month end (W0 contract)."""
		cases = (
			(("2026-11-14", 30, 0), "1996-11-14"),
			(("2024-02-29", 1, 0), "2023-02-28"),  # 29 Feb, no 29 Feb a year earlier
			(("2024-02-29", 4, 0), "2020-02-29"),  # leap year to leap year
			(("2026-03-31", 0, 1), "2026-02-28"),  # month end clamped
			(("2024-03-31", 0, 1), "2024-02-29"),  # clamped to a leap-year 29 Feb
			(("2024-02-29", 1, 1), "2023-01-29"),  # years and months together (13 months), no double clamp
			(("2026-05-31", 2, 3), "2024-02-29"),  # 27 months back lands on February of a leap year
			(("2026-01-15", 0, 11), "2025-02-15"),
			(("2026-11-14", 0, 0), "2026-11-14"),
		)
		for (on, years, months), expected in cases:
			self.assertEqual(dob.estimate_dob(on, years, months), getdate(expected), (on, years, months))

	def test_age_ranges(self):
		self.assertEqual(dob.validate_age(120, 11), (120, 11))
		self.assertEqual(dob.validate_age(None, None), (0, 0))
		for years, months in ((121, 0), (-1, 0), (0, 12), (0, -1)):
			with self.assertRaises(dob.EstimatedAgeError, msg=(years, months)):
				dob.validate_age(years, months)


class TestDobEstimateRules(EMRTestCase):
	def test_new_patient_needs_dob_or_age(self):
		with self.assertRaises(dob.DateOfBirthRequiredError):
			make_patient(dob=None)
		with self.assertRaises(dob.DateOfBirthRequiredError):
			estimated_patient(0, 0)
		with self.assertRaises(dob.EstimatedAgeError):
			estimated_patient(121, 0)
		with self.assertRaises(dob.EstimatedAgeError):
			estimated_patient(3, 12)

	def test_estimated_age_sets_dob_and_flag(self):
		p = estimated_patient(30)
		self.assertEqual(p.get(FLAG), 1)
		self.assertEqual(getdate(p.get(ON)), getdate(today()))
		self.assertEqual(getdate(p.dob), getdate(add_years(today(), -30)))

	def test_infant_months_only(self):
		p = estimated_patient(0, 5)
		self.assertEqual(p.get(FLAG), 1)
		self.assertEqual(getdate(p.dob), dob.estimate_dob(today(), 0, 5))

	def test_back_entered_estimate_date(self):
		"""Back-entry of a paper registration: 30 Jun 2025 minus 1 year 4 months = 30 Feb 2024 -> 29 Feb."""
		p = estimated_patient(1, 4, **{ON: "2025-06-30"})
		self.assertEqual(getdate(p.dob), getdate("2024-02-29"))
		self.assertEqual(getdate(p.get(ON)), getdate("2025-06-30"))
		future = estimated_patient(1, 0, **{ON: add_years(today(), 1)})
		self.assertEqual(getdate(future.get(ON)), getdate(today()))  # never in the future

	def test_exact_dob_clears_age_on_insert(self):
		p = make_patient(dob="1990-01-01")
		self.assertEqual((p.get(FLAG), p.get(YEARS), p.get(MONTHS), p.get(ON)), (0, 0, 0, None))

	def test_dob_and_age_conflict_on_insert(self):
		with self.assertRaises(dob.EstimatedAgeError):
			make_patient(dob="1990-01-01", **{YEARS: 30})
		# The form ticks the flag when an age is typed: the estimate wins and the DOB is recomputed.
		p = make_patient(dob="1990-01-01", **{YEARS: 30, FLAG: 1})
		self.assertEqual(getdate(p.dob), getdate(add_years(today(), -30)))

	def test_exact_dob_later_clears_the_flag(self):
		p = estimated_patient(30)
		p.reload()
		p.dob = "1995-03-04"
		p.save(ignore_permissions=True)
		p.reload()
		self.assertEqual(getdate(p.dob), getdate("1995-03-04"))
		self.assertEqual((p.get(FLAG), p.get(YEARS), p.get(MONTHS), p.get(ON)), (0, 0, 0, None))

	def test_changing_age_recomputes_from_original_date(self):
		p = estimated_patient(1, 4, **{ON: "2025-06-30"})
		p.reload()
		p.set(YEARS, 2)
		p.save(ignore_permissions=True)
		p.reload()
		# 30 Jun 2025 minus 2 years 4 months = 30 Feb 2023 -> 28 Feb 2023; the reference date is kept.
		self.assertEqual(getdate(p.dob), getdate("2023-02-28"))
		self.assertEqual(getdate(p.get(ON)), getdate("2025-06-30"))

	def test_unrelated_save_keeps_the_estimate(self):
		p = estimated_patient(1, 4, **{ON: "2025-06-30"})
		p.reload()
		p.middle_name = "T-Middle"
		p.save(ignore_permissions=True)
		p.reload()
		self.assertEqual(getdate(p.dob), getdate("2024-02-29"))
		self.assertEqual(p.get(FLAG), 1)

	def test_existing_patient_without_dob_not_blocked(self):
		"""Patients registered before the rule (no DOB) save normally until the DOB fields are touched."""
		p = make_patient()
		frappe.db.set_value("Patient", p.name, "dob", None, update_modified=False)
		p = frappe.get_doc("Patient", p.name)
		p.middle_name = "T-Legacy"
		p.save(ignore_permissions=True)  # unrelated save: allowed
		self.assertFalse(frappe.db.get_value("Patient", p.name, "dob"))
		p.reload()
		p.set(YEARS, 40)
		p.save(ignore_permissions=True)  # giving an age later: estimated
		p.reload()
		self.assertEqual(p.get(FLAG), 1)
		self.assertEqual(getdate(p.dob), getdate(add_years(today(), -40)))

	def test_clearing_dob_is_refused(self):
		p = make_patient(dob="1990-01-01")
		p.reload()
		p.dob = None
		with self.assertRaises(dob.DateOfBirthRequiredError):
			p.save(ignore_permissions=True)
		e = estimated_patient(30)
		e.reload()
		e.set(YEARS, 0)
		e.dob = None
		e.set(FLAG, 0)
		with self.assertRaises(dob.DateOfBirthRequiredError):
			e.save(ignore_permissions=True)

	def test_age_given_to_exact_patient_later(self):
		"""An age entered on an exact record (DOB untouched) becomes the estimate."""
		p = make_patient(dob="1990-01-01")
		p.reload()
		p.set(YEARS, 20)
		p.save(ignore_permissions=True)
		p.reload()
		self.assertEqual(p.get(FLAG), 1)
		self.assertEqual(getdate(p.dob), getdate(add_years(today(), -20)))


class TestSimilarityWindow(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.surname = f"T-{random_string(8)}"

	def reasons(self, **kw) -> list[str]:
		found = find_similar("Halima", self.surname, "Female", **kw)
		return found[0]["reasons"] if found else []

	def test_exact_dobs_need_the_same_year(self):
		make_patient(first_name="Halima", last_name=self.surname, sex="Female", dob="1990-05-01")
		self.assertNotIn("date of birth within 2 years (estimated)", self.reasons(dob="1992-03-01"))

	def test_window_when_the_new_dob_is_estimated(self):
		make_patient(first_name="Halima", last_name=self.surname, sex="Female", dob="1990-05-01")
		self.assertIn(
			"date of birth within 2 years (estimated)", self.reasons(dob="1992-03-01", dob_estimated=True)
		)
		# 1992-05-02 is two years and a day later: outside the window.
		self.assertNotIn(
			"date of birth within 2 years (estimated)", self.reasons(dob="1992-05-02", dob_estimated=True)
		)

	def test_window_when_the_existing_dob_is_estimated(self):
		existing = make_patient(
			first_name="Halima", last_name=self.surname, sex="Female", dob=None, **{YEARS: 30}
		)
		other = add_years(existing.dob, 2)
		found = find_similar("Halima", self.surname, "Female", dob=str(other))
		self.assertEqual(found[0]["name"], existing.name)
		self.assertEqual(found[0]["dob_estimated"], 1)
		self.assertIn("date of birth within 2 years (estimated)", found[0]["reasons"])

	def test_estimated_window_widens_the_strong_candidate_set(self):
		"""Set 2 (one name + DOB + sex) uses the window, so the match is scored even when it is old."""
		match = make_patient(first_name="Zawadi", last_name=self.surname, sex="Female", dob="1990-05-01")
		found = find_similar("Halima", self.surname, "Female", dob="1991-06-01", dob_estimated=True)
		self.assertIn(match.name, [f["name"] for f in found])


# --------------------------------------------------------------------------- attendance


class TestAttendanceRule(EMRTestCase):
	def test_first_visit_of_the_year_is_new(self):
		v = make_visit()
		self.assertEqual(
			(v.attendance_type, v.attendance_basis, v.attendance_note),
			(ATTENDANCE_NEW, ATTENDANCE_BASIS_RULE, None),
		)

	def test_second_visit_is_reattendance(self):
		v1 = make_visit()
		close(v1.name)
		v2 = make_visit(patient=v1.patient)
		self.assertEqual(
			(v2.attendance_type, v2.attendance_basis), (ATTENDANCE_REVISIT, ATTENDANCE_BASIS_RULE)
		)
		self.assertIn(v1.name, v2.attendance_note)

	def test_other_facility_is_new(self):
		try:
			other = get_facility(1)
		except IndexError:
			self.skipTest("only one facility on this site")
		v1 = make_visit()
		close(v1.name)
		v2 = make_visit(patient=v1.patient, facility=other)
		self.assertEqual(v2.attendance_type, ATTENDANCE_NEW)

	def test_new_calendar_year_is_new(self):
		last_year = add_years(today(), -1)
		v1 = make_visit(system=True, visit_date=last_year)
		self.assertEqual(v1.attendance_type, ATTENDANCE_NEW)
		close(v1.name)
		v2 = make_visit(patient=v1.patient)
		self.assertEqual(v2.attendance_type, ATTENDANCE_NEW)
		v3_patient = v1.patient
		close(v2.name)
		self.assertEqual(make_visit(patient=v3_patient).attendance_type, ATTENDANCE_REVISIT)

	def test_registered_in_error_and_superseded_are_ignored(self):
		for reason in ("Registered in error", "Superseded by a new visit"):
			v1 = make_visit()
			close(v1.name, reason)
			v2 = make_visit(patient=v1.patient)
			self.assertEqual(v2.attendance_type, ATTENDANCE_NEW, reason)

	def test_smartcare_override(self):
		v = make_visit(seen_in_smartcare=1)
		self.assertEqual(
			(v.attendance_type, v.attendance_basis), (ATTENDANCE_REVISIT, ATTENDANCE_BASIS_RECEPTION)
		)
		self.assertTrue(v.attendance_note)

	def test_smartcare_tick_on_a_revisit_keeps_the_rule(self):
		v1 = make_visit()
		close(v1.name)
		v2 = make_visit(patient=v1.patient, seen_in_smartcare=1)
		self.assertEqual(
			(v2.attendance_type, v2.attendance_basis), (ATTENDANCE_REVISIT, ATTENDANCE_BASIS_RULE)
		)

	def test_smartcare_override_can_be_turned_off(self):
		frappe.db.set_single_value(SETTINGS, OVERRIDE_FIELD, 0)
		self.assertFalse(attendance.reception_override_allowed())
		v = make_visit(seen_in_smartcare=1)
		self.assertEqual((v.attendance_type, v.attendance_basis), (ATTENDANCE_NEW, ATTENDANCE_BASIS_RULE))
		self.assertEqual(v.seen_in_smartcare, 1)
		frappe.db.set_single_value(SETTINGS, OVERRIDE_FIELD, 1)
		self.assertTrue(attendance.reception_override_allowed())

	def test_settings_defaults(self):
		self.assertEqual(attendance.get_year_basis(), attendance.YEAR_BASIS_CALENDAR)
		self.assertEqual(attendance.year_bounds("2024-02-29"), (getdate("2024-01-01"), getdate("2024-12-31")))

	def test_patient_correction_recomputes(self):
		seen = make_visit()
		close(seen.name)
		v = make_visit()
		self.assertEqual(v.attendance_type, ATTENDANCE_NEW)
		v.reload()
		v.patient = seen.patient
		v.save(ignore_permissions=True)
		self.assertEqual(v.attendance_type, ATTENDANCE_REVISIT)


class TestInternalReferralAttendance(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.users = users()

	def refer(self, referral_type="Internal", backdate_source=False):
		visit = routed_visit(DEPT_OUTPATIENT)
		if backdate_source:
			# Year boundary: the source visit began last year (e.g. 31 December), the referral is sent today.
			frappe.db.set_value(VISIT_DOCTYPE, visit.name, "visit_date", add_years(today(), -1))
		enc = start(visit.name, self.users["clinician"])
		add_diagnosis(enc)
		values = {"to_department": DEPT_MENTAL_HEALTH}
		if referral_type == "External":
			values = {"to_facility": referral_facility()}
		with as_user(self.users["clinician"]):
			out = ref.create_referral(enc, referral_type, "T- review", send=True, **values)
		return visit.name, out

	def test_internal_referral_is_not_a_new_attendance(self):
		source, out = self.refer()
		target = visit_row(out["target_visit"], "attendance_type", "attendance_basis", "attendance_note")
		self.assertEqual(
			(target.attendance_type, target.attendance_basis), (ATTENDANCE_REVISIT, ATTENDANCE_BASIS_RULE)
		)
		self.assertIn("Internal referral", target.attendance_note)
		self.assertIn(source, target.attendance_note)
		self.assertEqual(visit_row(source, "outcome").outcome, OUTCOME_REFERRED_INTERNAL)

	def test_internal_referral_across_the_year_boundary(self):
		"""The source visit is last year's, so only the referral (not the year rule) makes it a revisit."""
		source, out = self.refer(backdate_source=True)
		target = visit_row(out["target_visit"], "attendance_type", "attendance_note")
		self.assertEqual(target.attendance_type, ATTENDANCE_REVISIT)
		self.assertIn(source, target.attendance_note)

	def test_internal_referral_source_detection(self):
		source, out = self.refer()
		target = frappe.get_doc(VISIT_DOCTYPE, out["target_visit"])
		self.assertEqual(attendance.internal_referral_source(target), source)  # via referred_from
		# A later, ordinary visit is not mistaken for the referral (the referral has its target now).
		close(target.name)
		later = make_visit(patient=target.patient)
		self.assertIsNone(attendance.internal_referral_source(later))
		self.assertEqual(later.attendance_type, ATTENDANCE_REVISIT)  # calendar-year rule


# --------------------------------------------------------------------------- corrections


class TestAttendanceCorrections(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.users = users()

	def correct(
		self, user_key, visit, attendance_type=ATTENDANCE_REVISIT, reason="T- seen in March on paper"
	):
		with as_user(self.users[user_key]):
			return attendance.correct_attendance(visit, attendance_type, reason)

	def test_reception_while_registered(self):
		v = make_visit()
		out = self.correct("reception", v.name)
		self.assertEqual(
			(out["attendance_type"], out["attendance_basis"]),
			(ATTENDANCE_REVISIT, ATTENDANCE_BASIS_RECEPTION),
		)
		doc = frappe.get_doc(VISIT_DOCTYPE, v.name)
		last = doc.status_log[-1]
		self.assertEqual(
			(last.action, last.from_status, last.to_status),
			("correct_attendance", "Registered", "Registered"),
		)
		self.assertIn("T- seen in March on paper", last.comment)
		# L9: the permlevel 1 note is neutral; the reason is only in the (permlevel 2) status log.
		self.assertNotIn("T- seen in March on paper", doc.attendance_note)
		self.assertIn(ATTENDANCE_BASIS_RECEPTION, doc.attendance_note)

	def test_reception_refused_after_registered(self):
		v = make_visit()
		engine.transition(v.name, "start_triage", system=True)
		with self.assertRaises(frappe.PermissionError):
			self.correct("reception", v.name)

	def test_reception_refused_when_overrides_are_off(self):
		frappe.db.set_single_value(SETTINGS, OVERRIDE_FIELD, 0)
		v = make_visit()
		with self.assertRaises(frappe.PermissionError):
			self.correct("reception", v.name)
		out = self.correct("his", v.name)  # the HIS Officer always may
		self.assertEqual(out["attendance_basis"], ATTENDANCE_BASIS_HIS)
		frappe.db.set_single_value(SETTINGS, OVERRIDE_FIELD, 1)

	def test_his_any_time_including_closed(self):
		v = make_visit()
		close(v.name)
		out = self.correct("his", v.name)
		self.assertEqual(
			(out["attendance_type"], out["attendance_basis"]), (ATTENDANCE_REVISIT, ATTENDANCE_BASIS_HIS)
		)
		out = self.correct("his", v.name, ATTENDANCE_NEW, "T- paper visit was another patient")
		self.assertEqual(out["attendance_type"], ATTENDANCE_NEW)

	def test_other_roles_refused(self):
		v = make_visit()
		for key in ("triage", "clinician", "mho", "lab", "pharmacist", "mic", "hoo", "sysadmin"):
			with self.assertRaises(frappe.PermissionError, msg=key):
				self.correct(key, v.name)
		self.assertEqual(visit_row(v.name, "attendance_type").attendance_type, ATTENDANCE_NEW)

	def test_reason_value_and_change_required(self):
		v = make_visit()
		with self.assertRaises(attendance.AttendanceCorrectionError):
			self.correct("his", v.name, reason="  ")
		with self.assertRaises(attendance.AttendanceCorrectionError):
			self.correct("his", v.name, attendance_type="First")
		self.correct("his", v.name)
		with self.assertRaises(attendance.AttendanceCorrectionError):
			self.correct("his", v.name)  # nothing changes

	def test_can_correct_matrix(self):
		a, r = attendance.ACTION_CORRECT_ATTENDANCE, attendance.ACTION_CORRECT_REFERRED_IN
		expected = {
			# (user, status): (attendance, referred in)
			("reception", "Registered"): (True, True),
			("reception", "In Triage"): (False, False),
			("reception", "Discharged"): (False, False),
			("his", "Registered"): (True, True),
			("his", "Discharged"): (True, True),
			("triage", "Registered"): (False, False),
			("mic", "Registered"): (False, False),
		}
		for (key, status), (att, refin) in expected.items():
			user = self.users[key]
			self.assertEqual(attendance.can_correct(a, status, user), att, (key, status))
			self.assertEqual(attendance.can_correct(r, status, user), refin, (key, status))
		self.assertTrue(attendance.can_correct(a, "Discharged", "Administrator"))

	def test_correct_referred_in(self):
		facility = referral_facility()
		v = make_visit()
		with as_user(self.users["reception"]):
			out = attendance.correct_referred_in(v.name, 1, "T- letter found", referred_in_from=facility)
		self.assertEqual((out["referred_in"], out["referred_in_from"]), (1, facility))
		self.assertEqual(frappe.get_doc(VISIT_DOCTYPE, v.name).status_log[-1].action, "correct_referred_in")
		close(v.name)
		with as_user(self.users["reception"]), self.assertRaises(frappe.PermissionError):
			attendance.correct_referred_in(v.name, 0, "T- wrong")
		with as_user(self.users["his"]):
			out = attendance.correct_referred_in(v.name, 0, "T- not referred", referred_in_from=facility)
		self.assertEqual((out["referred_in"], out["referred_in_from"]), (0, None))
		with as_user(self.users["his"]), self.assertRaises(attendance.AttendanceCorrectionError):
			attendance.correct_referred_in(v.name, 1, "T- x", referred_in_from="T-No Such Facility")
		with as_user(self.users["his"]), self.assertRaises(attendance.AttendanceCorrectionError):
			attendance.correct_referred_in(v.name, 0, "T- nothing")
		with as_user(self.users["clinician"]), self.assertRaises(frappe.PermissionError):
			attendance.correct_referred_in(v.name, 1, "T- x")

	def test_intake_values_at_registration(self):
		facility = referral_facility()
		v = make_visit(referred_in=1, referred_in_from=facility)
		self.assertEqual((v.referred_in, v.referred_in_from), (1, facility))


# --------------------------------------------------------------------------- outcome


class TestOutcomeEndpoints(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.users = users()

	def choices(self, user_key, visit, action, reason=None):
		with as_user(self.users[user_key]):
			return outcome.get_outcome_choices(visit, action, reason)

	def test_close_visit_choices(self):
		v = make_visit().name
		info = self.choices("reception", v, "close_visit", "Left before being seen")
		self.assertEqual((info["choices"], info["default"]), ([OUTCOME_LEFT], OUTCOME_LEFT))
		info = self.choices("reception", v, "close_visit", "Other")
		self.assertEqual(
			info["choices"], [OUTCOME_NOT_APPLICABLE, OUTCOME_DISCHARGED, OUTCOME_ADMITTED, OUTCOME_LEFT]
		)
		self.assertEqual(info["default"], OUTCOME_NOT_APPLICABLE)
		self.assertFalse(info["keeps_current"])
		self.assertEqual(self.choices("mic", v, "close_visit", "Died")["choices"], [OUTCOME_DIED])
		self.assertEqual(self.choices("reception", v, "close_visit", "Died")["choices"], [])

	def test_died_refused_for_reception(self):
		v = make_visit().name
		self.assertNotIn(OUTCOME_DIED, self.choices("reception", v, "complete_consultation")["choices"])
		self.assertIn(OUTCOME_DIED, self.choices("clinician", v, "complete_consultation")["choices"])
		with as_user(self.users["reception"]), self.assertRaises(frappe.PermissionError):
			engine.apply_action(v, "close_visit", closure_reason="Died", comment="T- x")
		with as_user(self.users["reception"]), self.assertRaises(engine.VisitOutcomeError):
			# "Other" never offers Died: a death is recorded with the "Died" reason.
			engine.apply_action(
				v, "close_visit", closure_reason="Other", comment="T- x", outcome=OUTCOME_DIED
			)
		# The server path behind the Complete Consultation dialog refuses Died without a DIED_ROLES role.
		doc = frappe.get_doc(VISIT_DOCTYPE, v)
		with as_user(self.users["reception"]), self.assertRaises(frappe.PermissionError):
			engine.resolve_outcome(doc, "complete_consultation", OUTCOME_DIED)
		with as_user(self.users["clinician"]):
			self.assertEqual(engine.resolve_outcome(doc, "complete_consultation", OUTCOME_DIED), OUTCOME_DIED)
		self.assertEqual(visit_row(v, "status").status, "Registered")

	def test_complete_consultation_died(self):
		from onerc_healthcare.clinical.consultation import complete_consultation

		visit = routed_visit().name
		encounter = start(visit, self.users["clinician"])
		with as_user(self.users["clinician"]):
			complete_consultation(encounter, no_diagnosis_reason="T- none", outcome=OUTCOME_DIED)
		row = visit_row(visit, "outcome", "outcome_by", "status")
		self.assertEqual((row.outcome, row.outcome_by), (OUTCOME_DIED, self.users["clinician"]))

	def test_close_keeps_consultation_outcome(self):
		from onerc_healthcare.clinical.consultation import complete_consultation
		from onerc_healthcare.tests.clinical_factories import add_lab_row

		visit = routed_visit().name
		encounter = start(visit, self.users["clinician"])
		add_lab_row(encounter)
		with as_user(self.users["clinician"]):
			from onerc_healthcare.clinical.consultation import send_lab_orders

			send_lab_orders(encounter)
			complete_consultation(
				encounter, reason="T- results later", no_diagnosis_reason="T- none", outcome=OUTCOME_ADMITTED
			)
		info = self.choices("clinician", visit, "close_visit", "Other")
		self.assertTrue(info["keeps_current"])
		self.assertIsNone(info["default"])
		self.assertEqual(info["current"], OUTCOME_ADMITTED)

	def test_endpoint_needs_visit_read(self):
		v = make_visit().name
		with as_user(self.users["sysadmin"]), self.assertRaises(frappe.PermissionError):
			outcome.get_outcome_choices(v, "close_visit", "Other")
		with as_user(self.users["sysadmin"]), self.assertRaises(frappe.PermissionError):
			outcome.get_visit_capture(v)

	def test_visit_capture_flags(self):
		v = make_visit().name
		expected = {
			"reception": (True, True, False, False),
			"his": (True, True, False, False),
			"clinician": (False, False, True, True),
			"mho": (False, False, True, True),
			"mic": (False, False, True, True),  # D57: the Medical In-Charge records services
			"triage": (False, False, False, False),
			"hoo": (False, False, False, False),
		}
		for key, flags in expected.items():
			with as_user(self.users[key]):
				c = outcome.get_visit_capture(v)
			got = (
				c["can_correct_attendance"],
				c["can_correct_referred_in"],
				c["can_view_services"],
				c["can_record_services"],
			)
			self.assertEqual(got, flags, key)


class TestOutcomeBackfill(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.users = users()

	def legacy(self, visit: str):
		"""Make `visit` look closed before outcomes existed."""
		frappe.db.set_value(
			VISIT_DOCTYPE,
			visit,
			{"outcome": None, "outcome_at": None, "outcome_by": None},
			update_modified=False,
		)

	def consultation(self):
		visit = routed_visit().name
		encounter = start(visit, self.users["clinician"])
		add_diagnosis(encounter)
		return visit, encounter

	def test_backfill(self):
		from onerc_healthcare.clinical.consultation import complete_consultation

		cases = {}
		for reason, expected in (
			("Left before being seen", OUTCOME_LEFT),
			("Registered in error", OUTCOME_NOT_APPLICABLE),
			("Other", OUTCOME_NOT_APPLICABLE),
			("Died", OUTCOME_DIED),
		):
			v = make_visit().name
			close(v, reason)
			cases[v] = expected
		completed, enc = self.consultation()
		with as_user(self.users["clinician"]):
			complete_consultation(enc, outcome=OUTCOME_ADMITTED)
		cases[completed] = OUTCOME_DISCHARGED  # history only says the consultation completed
		for referral_type, expected in (
			("Internal", OUTCOME_REFERRED_INTERNAL),
			("External", OUTCOME_REFERRED_OUT),
		):
			v, enc = self.consultation()
			values = (
				{"to_department": DEPT_MENTAL_HEALTH}
				if referral_type == "Internal"
				else {"to_facility": referral_facility()}
			)
			with as_user(self.users["clinician"]):
				ref.create_referral(enc, referral_type, "T- review", send=True, **values)
			cases[v] = expected
		kept = make_visit().name
		close(kept, "Other", outcome=OUTCOME_ADMITTED)

		for v in cases:
			self.legacy(v)
		closed_at = {v: visit_row(v, "closed_at").closed_at for v in cases}

		first = outcome.backfill_outcomes()
		self.assertGreaterEqual(first["updated"], len(cases))
		for v, expected in cases.items():
			row = visit_row(v, "outcome", "outcome_at", "outcome_by")
			self.assertEqual(row.outcome, expected, v)
			self.assertEqual(row.outcome_at, closed_at[v], v)
			self.assertIsNone(row.outcome_by, v)
		self.assertEqual(visit_row(kept, "outcome").outcome, OUTCOME_ADMITTED)  # never overwritten
		log_lengths = {v: len(frappe.get_doc(VISIT_DOCTYPE, v).status_log) for v in cases}

		second = outcome.backfill_outcomes()  # idempotent
		self.assertEqual(second["updated"], 0)
		for v in cases:
			self.assertEqual(len(frappe.get_doc(VISIT_DOCTYPE, v).status_log), log_lengths[v])

	def test_patch_module_runs(self):
		from onerc_healthcare.patches import backfill_visit_outcome

		v = make_visit().name
		close(v, "Left before completion")
		self.legacy(v)
		backfill_visit_outcome.execute()
		self.assertEqual(visit_row(v, "outcome").outcome, OUTCOME_LEFT)


# --------------------------------------------------------------------------- fix round (M4, L7, L10)


class TestAttendanceAfterMerge(EMRTestCase):
	def test_merge_recomputes_rule_attendance(self):
		"""M4: P1 (New) and duplicate P2 (New) at the same facility this year; after merging P2 into P1
		only P1's first visit stays New. An HIS override on P2 is kept and listed for review."""
		from onerc_healthcare.registration.merge import merge_patients

		v1 = make_visit()
		close(v1.name)
		v2 = make_visit()  # duplicate record of the same person, registered later
		close(v2.name)
		v3 = make_visit(patient=v2.patient)
		close(v3.name)
		self.assertEqual((v1.attendance_type, v2.attendance_type), (ATTENDANCE_NEW, ATTENDANCE_NEW))
		engine.annotate(
			v3.name,
			"correct_attendance",
			"T- HIS",
			{"attendance_type": ATTENDANCE_NEW, "attendance_basis": ATTENDANCE_BASIS_HIS},
		)
		out = merge_patients(v2.patient, v1.patient, "T- same person")

		self.assertEqual(visit_row(v1.name, "attendance_type").attendance_type, ATTENDANCE_NEW)
		row = visit_row(v2.name, "attendance_type", "attendance_basis", "attendance_note")
		self.assertEqual(
			(row.attendance_type, row.attendance_basis), (ATTENDANCE_REVISIT, ATTENDANCE_BASIS_RULE)
		)
		self.assertIn(f"merge of {v2.patient} into {v1.patient}", row.attendance_note)
		last = frappe.get_doc(VISIT_DOCTYPE, v2.name).status_log[-1]
		self.assertEqual(last.action, "correct_attendance")
		self.assertEqual(visit_row(v3.name, "attendance_basis").attendance_basis, ATTENDANCE_BASIS_HIS)
		self.assertEqual(
			out["attendance_recomputed"],
			[{"visit": v2.name, "from": ATTENDANCE_NEW, "to": ATTENDANCE_REVISIT}],
		)
		self.assertEqual([o["visit"] for o in out["attendance_overrides"]], [v3.name])
		log = frappe.parse_json(frappe.db.get_value("Patient", v2.patient, "krcs_merge_log"))
		self.assertEqual([o["visit"] for o in log["attendance_overrides"]], [v3.name])

	def test_rule_attendance_oracle(self):
		"""Hand-built history: excluded closures never count as the earlier visit; a new year restarts."""
		d = frappe._dict
		visits = [
			d(
				name="a",
				patient="P",
				facility="F",
				visit_date="2025-12-30",
				closure_reason=None,
				referred_from=None,
			),
			d(
				name="b",
				patient="P",
				facility="F",
				visit_date="2026-01-02",
				closure_reason="Registered in error",
				referred_from=None,
			),
			d(
				name="c",
				patient="P",
				facility="F",
				visit_date="2026-01-03",
				closure_reason=None,
				referred_from=None,
			),
			d(
				name="d",
				patient="P",
				facility="G",
				visit_date="2026-01-04",
				closure_reason=None,
				referred_from=None,
			),
			d(
				name="e",
				patient="P",
				facility="F",
				visit_date="2026-02-01",
				closure_reason=None,
				referred_from=None,
			),
			d(
				name="f",
				patient="P",
				facility="G",
				visit_date="2026-02-02",
				closure_reason=None,
				referred_from="REF-1",
			),
		]
		self.assertEqual(
			attendance.rule_attendance(visits),
			{
				"a": ATTENDANCE_NEW,
				"b": ATTENDANCE_NEW,
				"c": ATTENDANCE_NEW,  # b was registered in error
				"d": ATTENDANCE_NEW,
				"e": ATTENDANCE_REVISIT,
				"f": ATTENDANCE_REVISIT,
			},
		)


class TestAttendanceBackfill(EMRTestCase):
	def test_backfill_attendance(self):
		from onerc_healthcare.patches import backfill_visit_attendance

		v1 = make_visit()
		close(v1.name)
		v2 = make_visit(patient=v1.patient)
		close(v2.name)
		kept = make_visit()
		for v in (v1.name, v2.name):
			frappe.db.set_value(
				VISIT_DOCTYPE,
				v,
				{"attendance_type": None, "attendance_basis": None, "attendance_note": None},
				update_modified=False,
			)
		frappe.db.set_value(VISIT_DOCTYPE, kept.name, "attendance_type", ATTENDANCE_REVISIT)
		backfill_visit_attendance.execute()
		self.assertEqual(visit_row(v1.name, "attendance_type").attendance_type, ATTENDANCE_NEW)
		row = visit_row(v2.name, "attendance_type", "attendance_basis")
		self.assertEqual(
			(row.attendance_type, row.attendance_basis), (ATTENDANCE_REVISIT, ATTENDANCE_BASIS_RULE)
		)
		self.assertEqual(visit_row(kept.name, "attendance_type").attendance_type, ATTENDANCE_REVISIT)
		self.assertEqual(attendance.backfill_attendance()["updated"], 0)  # idempotent


class TestOutcomeChoicesPermlevel(EMRTestCase):
	def test_current_outcome_hidden_from_status_only_readers(self):
		"""L7: the recorded outcome (permlevel 1) is not returned to the Head of Operations."""
		u = users()
		v = make_visit().name
		close(v, "Other", outcome=OUTCOME_ADMITTED)
		with as_user(u["hoo"]):
			self.assertIsNone(outcome.get_outcome_choices(v, "close_visit", "Other")["current"])
		with as_user(u["his"]):
			self.assertEqual(
				outcome.get_outcome_choices(v, "close_visit", "Other")["current"], OUTCOME_ADMITTED
			)
