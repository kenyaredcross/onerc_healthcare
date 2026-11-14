# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 3 W0 contract: visit outcome, the "Died" rule, annotate, intake fields, age bands, base filters."""

from datetime import date

import frappe

from onerc_healthcare.constants import (
	ATTENDANCE_BASIS_HIS,
	ATTENDANCE_REVISIT,
	OUTCOME_ADMITTED,
	OUTCOME_DIED,
	OUTCOME_DISCHARGED,
	OUTCOME_LEFT,
	OUTCOME_NOT_APPLICABLE,
	ROLE_MOH_API,
	VISIT_DOCTYPE,
)
from onerc_healthcare.reporting import ages, filters, registry
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import as_user, routed_visit, start, users
from onerc_healthcare.tests.visit_factories import make_visit
from onerc_healthcare.visit import engine


class TestVisitOutcome(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.users = users()

	def close(self, visit, reason, user=None, **kw):
		with as_user(user or self.users["mic"]):
			return engine.apply_action(visit, "close_visit", closure_reason=reason, comment="T- closed", **kw)

	def test_closure_reason_implies_outcome(self):
		visit = make_visit().name
		self.close(visit, "Left before being seen", user=self.users["reception"])
		row = frappe.db.get_value(VISIT_DOCTYPE, visit, ["outcome", "outcome_by", "outcome_at"], as_dict=True)
		self.assertEqual(row.outcome, OUTCOME_LEFT)
		self.assertEqual(row.outcome_by, self.users["reception"])
		self.assertTrue(row.outcome_at)

	def test_other_reason_lets_the_user_choose(self):
		visit = make_visit().name
		self.close(visit, "Other", outcome=OUTCOME_ADMITTED)
		self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit, "outcome"), OUTCOME_ADMITTED)
		other = make_visit().name
		self.close(other, "Other")
		self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, other, "outcome"), OUTCOME_NOT_APPLICABLE)

	def test_outcome_must_fit_the_reason(self):
		visit = make_visit().name
		with self.assertRaises(engine.VisitOutcomeError):
			self.close(visit, "Left before being seen", outcome=OUTCOME_ADMITTED)
		with self.assertRaises(engine.VisitOutcomeError):
			self.close(visit, "Other", outcome=OUTCOME_DIED)  # death is recorded with the "Died" reason
		self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit, "status"), "Registered")

	def test_only_clinical_roles_record_death(self):
		visit = make_visit().name
		with self.assertRaises(frappe.PermissionError):
			self.close(visit, "Died", user=self.users["reception"])
		self.assertNotIn("Died", engine.closure_reasons_for(self.users["reception"]))
		self.assertIn("Died", engine.closure_reasons_for(self.users["mic"]))
		self.close(visit, "Died")
		self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit, "outcome"), OUTCOME_DIED)

	def test_outcome_only_on_closing_actions(self):
		visit = make_visit().name
		with self.assertRaises(engine.VisitOutcomeError):
			engine.transition(visit, "start_triage", system=True, outcome=OUTCOME_ADMITTED)

	def test_close_keeps_consultation_outcome_unless_death(self):
		doc = frappe._dict(outcome=OUTCOME_ADMITTED)
		self.assertIsNone(engine.resolve_outcome(doc, "close_visit", None, "Left before completion"))
		self.assertEqual(engine.resolve_outcome(doc, "close_visit", None, "Died", system=True), OUTCOME_DIED)

	def test_complete_consultation_outcome(self):
		from onerc_healthcare.clinical.consultation import complete_consultation

		visit = routed_visit().name
		encounter = start(visit, self.users["clinician"])
		with as_user(self.users["clinician"]), self.assertRaises(engine.VisitOutcomeError):
			complete_consultation(encounter, no_diagnosis_reason="T- none", outcome="Referred out")
		self.assertEqual(frappe.db.get_value("Patient Encounter", encounter, "docstatus"), 0)
		with as_user(self.users["clinician"]):
			complete_consultation(encounter, no_diagnosis_reason="T- none", outcome=OUTCOME_ADMITTED)
		self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit, "outcome"), OUTCOME_ADMITTED)

	def test_complete_consultation_defaults_to_discharged_home(self):
		from onerc_healthcare.clinical.consultation import complete_consultation

		visit = routed_visit().name
		encounter = start(visit, self.users["clinician"])
		with as_user(self.users["clinician"]):
			complete_consultation(encounter, no_diagnosis_reason="T- none")
		self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit, "outcome"), OUTCOME_DISCHARGED)

	def test_outcome_choices_hide_death_from_reception(self):
		doc = frappe._dict()
		self.assertNotIn(
			OUTCOME_DIED, engine.outcome_choices(doc, "complete_consultation", user=self.users["reception"])
		)
		self.assertIn(
			OUTCOME_DIED, engine.outcome_choices(doc, "complete_consultation", user=self.users["clinician"])
		)
		self.assertEqual(engine.outcome_choices(doc, "close_visit", "Left before being seen"), [OUTCOME_LEFT])
		self.assertEqual(engine.outcome_choices(doc, "route"), [])


class TestAnnotateAndIntake(EMRTestCase):
	def test_annotate_corrects_without_status_change(self):
		visit = make_visit()
		doc = engine.annotate(
			visit.name,
			"correct_attendance",
			"T- seen in January on paper",
			{"attendance_type": ATTENDANCE_REVISIT, "attendance_basis": ATTENDANCE_BASIS_HIS},
		)
		self.assertEqual(doc.status, "Registered")
		self.assertEqual(doc.attendance_type, ATTENDANCE_REVISIT)
		last = doc.status_log[-1]
		self.assertEqual(
			(last.action, last.from_status, last.to_status),
			("correct_attendance", "Registered", "Registered"),
		)
		self.assertFalse(doc.outcome_at)

	def test_annotate_refusals(self):
		visit = make_visit().name
		with self.assertRaises(engine.IllegalVisitTransitionError):
			engine.annotate(visit, "close_visit", "T- x")
		with self.assertRaises(engine.IllegalVisitTransitionError):
			engine.annotate(visit, "note", "  ")
		with self.assertRaises(engine.IllegalVisitTransitionError):
			engine.annotate(visit, "note", "T- x", {"status": "Discharged"})

	def test_intake_fields_kept_at_registration_then_protected(self):
		from onerc_healthcare.emr_visit.doctype.patient_visit.patient_visit import VisitStatusLockedError

		visit = make_visit(seen_in_smartcare=1, outcome=OUTCOME_DIED, attendance_basis=ATTENDANCE_BASIS_HIS)
		self.assertEqual(visit.seen_in_smartcare, 1)
		self.assertFalse(visit.outcome)  # server-owned: client values ignored
		self.assertNotEqual(visit.attendance_basis, ATTENDANCE_BASIS_HIS)
		visit.reload()
		visit.seen_in_smartcare = 0
		with self.assertRaises(VisitStatusLockedError):
			visit.save(ignore_permissions=True)

	def test_referred_in_from_cleared_without_referred_in(self):
		facility = frappe.db.get_value("Referral Facility", {}, "name")
		if not facility:
			self.skipTest("no Referral Facility seeded")
		visit = make_visit(referred_in=0, referred_in_from=facility)
		self.assertFalse(visit.referred_in_from)


class TestAgeBands(EMRTestCase):
	CASES = (
		("2024-02-29", "2025-02-28"),
		("2024-02-29", "2025-03-01"),
		("2024-01-31", "2024-02-29"),
		("2024-03-31", "2024-04-30"),
		("2021-03-15", "2026-03-14"),
		("2021-03-15", "2026-03-15"),
		("2026-01-01", "2026-01-01"),
		("2026-02-01", "2026-01-01"),
		(None, "2026-01-01"),
	)
	BANDS = (ages.AgeBand("<1", 0, 12), ages.AgeBand("1-4", 12, 60), ages.AgeBand("5+", 60))

	def test_sql_matches_python(self):
		case = ages.band_case_sql("CAST(%(dob)s AS DATE)", "CAST(%(on)s AS DATE)", self.BANDS)
		months = ages.age_months_sql("CAST(%(dob)s AS DATE)", "CAST(%(on)s AS DATE)")
		for dob, on in self.CASES:
			sql_band, sql_months = frappe.db.sql(f"select {case}, {months}", {"dob": dob, "on": on})[0]
			py_months = ages.age_in_months(dob, on)
			self.assertEqual(sql_band, ages.band_for(py_months, self.BANDS), (dob, on))
			if py_months is not None:
				self.assertEqual(sql_months, py_months, (dob, on))

	def test_known_values(self):
		self.assertEqual(ages.age_in_months("2024-02-29", "2025-02-28"), 11)
		self.assertEqual(ages.age_in_months("2024-02-29", "2025-03-01"), 12)
		self.assertEqual(ages.band_for(59, self.BANDS), "1-4")
		self.assertEqual(ages.band_for(60, self.BANDS), "5+")
		self.assertEqual(ages.band_for(None, self.BANDS), ages.UNKNOWN_BAND)


class TestReportingContract(EMRTestCase):
	def ctx(self, **kw):
		today = frappe.utils.getdate()
		return registry.SourceContext(report="T-", from_date=today, to_date=today, **kw)

	def count(self, ctx, visit):
		where, params = filters.visit_conditions(ctx)
		params["v"] = visit
		return frappe.db.sql(
			f"select count(*) from {filters.VISIT_FROM} where {where} and v.name = %(v)s", params
		)[0][0]

	def test_visit_filters_exclude_registered_in_error(self):
		kept, dropped = make_visit().name, make_visit().name
		engine.transition(
			dropped, "close_visit", system=True, closure_reason="Registered in error", comment="T-"
		)
		ctx = self.ctx()
		self.assertEqual(self.count(ctx, kept), 1)
		self.assertEqual(self.count(ctx, dropped), 0)
		other_day = registry.SourceContext(report="T-", from_date=date(2001, 1, 1), to_date=date(2001, 1, 31))
		self.assertEqual(self.count(other_day, kept), 0)

	def test_encounter_filters_compile(self):
		where, params = filters.encounter_conditions(self.ctx())
		frappe.db.sql(f"select count(*) from {filters.ENCOUNTER_FROM} where {where}", params)

	def test_cell_key_blanks_unused_dimensions(self):
		el = registry.ElementSpec("E1", "E1", "Visit Attendance", {}, registry.DISAGGREGATION_SEX)
		self.assertEqual(registry.cell_key(el, "5+", "Female"), ("E1", "", "Female"))

	def test_contract_records(self):
		self.assertEqual(frappe.db.get_value("Role", ROLE_MOH_API, "desk_access"), 0)
		from onerc_healthcare.naming import DEFAULT_RULES

		rules = dict(DEFAULT_RULES)
		for doctype in ("MOH Report Run", "Service Delivered", "Community Activity Record"):
			self.assertIn(doctype, rules)
		self.assertTrue(frappe.get_meta(VISIT_DOCTYPE).get_field("outcome"))
		self.assertEqual(frappe.get_hooks("krcs_report_exclusions"), [])
