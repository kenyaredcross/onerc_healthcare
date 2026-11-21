# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MCH-B: Immunisation, the KEPI schedule and the Due Immunisations report (docs/MCH_CONTRACT.md §7, CON-16).
Never commit.

Every expected date and status is worked out by hand from the seeded schedule (mch/data/kepi_schedule.json):
6/10/14 weeks = 42/70/98 days, a dose is Due for 28 days after its due date, then Overdue; a dose may be given
from 14 days before its due age; Penta 2 and 3 need 28 days after the previous Penta.
"""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import add_days, getdate, today

from onerc_healthcare import constants as c
from onerc_healthcare.emr_mch.report.due_immunisations import due_immunisations as report
from onerc_healthcare.mch import common, immunisation
from onerc_healthcare.mch.common import MCHRecordError
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import get_facilities, get_facility
from onerc_healthcare.tests.mch_factories import make_child, make_mch_visit, make_mother, mch_users

IMM = c.IMMUNISATION_DOCTYPE
KEPI = c.KEPI_SCHEDULE_DOCTYPE
GIVEN, NOT_GIVEN = c.DOSE_GIVEN, c.DOSE_NOT_GIVEN
BIRTH = ["BCG", "OPV 0"]
WEEK6 = ["OPV 1", "Penta 1", "PCV 1", "Rota 1"]
WEEK10 = ["OPV 2", "Penta 2", "PCV 2", "Rota 2"]


def dose(vaccine: str, status: str = GIVEN, **kw) -> dict:
	return {"vaccine": vaccine, "status": status, **kw}


def close_visit(name: str) -> None:
	"""Discharge a visit directly (so the child can start another one: one open visit per patient)."""
	frappe.db.set_value(c.VISIT_DOCTYPE, name, "status", c.VISIT_STATUS_DISCHARGED, update_modified=False)


def dob_of(patient: str):
	return getdate(frappe.db.get_value("Patient", patient, "dob"))


class ImmunisationCase(EMRTestCase):
	def setUp(self):
		super().setUp()
		immunisation.seed_schedule()
		self.users = mch_users()

	def new(self, child: str, doses: list[dict], given_on=None, visit: str | None = None, **kw):
		values = {
			"doctype": IMM,
			"patient": child,
			"visit": visit or make_mch_visit(child).name,
			"given_on": given_on or today(),
			"doses": doses,
		}
		values.update(kw)
		return frappe.get_doc(values)

	def give(self, child: str, doses, given_on=None, user: str | None = None, submit=True, close=True, **kw):
		"""Insert (and submit) an Immunisation as `user` (a key of mch_users; default Administrator) on a new
		visit, then close that visit."""
		doc = self.new(child, [dose(d) if isinstance(d, str) else d for d in doses], given_on, **kw)
		if user:
			frappe.set_user(self.users[user])
		try:
			doc.insert()
			if submit:
				doc.submit()
		finally:
			frappe.set_user("Administrator")
			if close:  # also after a refusal, so the child can start another visit
				close_visit(doc.visit)
		return doc

	def on_time_child(self, age_days: int, upto_week10: bool = True) -> str:
		"""A child of `age_days` given the birth doses at birth, the 6-week doses at 42 days and (unless
		`upto_week10` is False) the 10-week doses at 70 days."""
		child = make_child(age_days=age_days).name
		dob = dob_of(child)
		self.give(child, BIRTH, given_on=dob)
		self.give(child, WEEK6, given_on=dob + timedelta(days=42))
		if upto_week10:
			self.give(child, WEEK10, given_on=dob + timedelta(days=70))
		return child

	def by_vaccine(self, child: str, on=None, user: str = "clinician") -> dict:
		frappe.set_user(self.users[user])
		try:
			rows = immunisation.schedule(child, on=str(on) if on else None)
		finally:
			frappe.set_user("Administrator")
		return {r["vaccine"]: r for r in rows}


# =====================================================================================================
# KEPI schedule
# =====================================================================================================
class TestKEPISchedule(ImmunisationCase):
	def test_seed_has_every_contract_dose(self):
		names = set(frappe.get_all(KEPI, pluck="name"))
		expected = {
			"BCG",
			"OPV 0",
			*(f"{a} {n}" for a in ("OPV", "Penta", "PCV") for n in (1, 2, 3)),
			"Rota 1",
			"Rota 2",
			"IPV",
			"MR 1",
			"MR 2",
			*(f"Vitamin A {n}" for n in range(1, 10)),
		}
		self.assertLessEqual(expected, names)
		row = frappe.db.get_value(
			KEPI,
			"Penta 3",
			["antigen", "dose_number", "due_age_days", "min_interval_days", "route"],
			as_dict=True,
		)
		self.assertEqual(
			(row.antigen, row.dose_number, row.due_age_days, row.min_interval_days, row.route),
			("Penta", 3, 98, 28, "Intramuscular"),
		)
		self.assertEqual(frappe.db.get_value(KEPI, "MR 1", "due_age_days"), 274)  # 9 months
		self.assertEqual(frappe.db.get_value(KEPI, "MR 2", "due_age_days"), 548)  # 18 months
		# Vitamin A every 6 months to 59 months: the last dose is due before 59 months (1795 days).
		self.assertLessEqual(frappe.db.get_value(KEPI, "Vitamin A 9", "latest_age_days"), 1795)

	def test_seed_is_idempotent_and_never_edits(self):
		frappe.db.set_value(KEPI, "IPV", "due_age_days", 105)
		self.assertEqual(immunisation.seed_schedule(), 0)
		self.assertEqual(frappe.db.get_value(KEPI, "IPV", "due_age_days"), 105)
		# A deleted (missing) row is inserted again, the others untouched.
		frappe.db.delete(KEPI, {"name": "MR 2"})
		self.assertEqual(immunisation.seed_schedule(), 1)
		self.assertTrue(frappe.db.exists(KEPI, "MR 2"))
		self.assertEqual(frappe.db.get_value(KEPI, "IPV", "due_age_days"), 105)

	def test_row_rules(self):
		def row(**kw):
			values = {"doctype": KEPI, "dose_label": "T-Dose", "antigen": "T-Antigen", "due_age_days": 42}
			values.update(kw)
			return frappe.get_doc(values)

		with self.assertRaises(MCHRecordError):
			row(due_age_days=-1).insert()
		with self.assertRaises(MCHRecordError):
			row(latest_age_days=30).insert()  # before the due age
		row(latest_age_days=0, dose_number=1).insert()  # 0 = no upper limit
		with self.assertRaises(MCHRecordError):  # same antigen and dose number
			row(dose_label="T-Dose bis", dose_number=1).insert()

	def test_master_permissions(self):
		for key, write in (("his", True), ("mic", True), ("clinician", False), ("triage", False)):
			self.assertTrue(frappe.has_permission(KEPI, "read", user=self.users[key]), key)
			self.assertEqual(bool(frappe.has_permission(KEPI, "write", user=self.users[key])), write, key)
		self.assertFalse(frappe.has_permission(KEPI, "read", user=self.users["reception"]))


# =====================================================================================================
# Immunisation record
# =====================================================================================================
class TestImmunisationRecord(ImmunisationCase):
	def test_triage_nurse_records_and_submits(self):
		child = make_child(age_days=45).name
		doc = self.give(child, ["BCG", "OPV 1", "Penta 1"], user="triage", close=False)
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(doc.facility, get_facility(0))
		self.assertEqual(doc.recorded_by, self.users["triage"])
		self.assertEqual(doc.age_at_visit, "6 weeks")  # 45 days
		self.assertEqual([d.given_by for d in doc.doses], [self.users["triage"]] * 3)
		self.assertEqual([d.antigen for d in doc.doses], ["BCG", "OPV", "Penta"])

	def test_given_by_is_server_set(self):
		child = make_child(age_days=45).name
		doc = self.new(child, [dose("BCG", given_by=self.users["mic"])])
		frappe.set_user(self.users["clinician"])
		try:
			doc.insert()
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(doc.doses[0].given_by, self.users["clinician"])

	def test_not_given_needs_a_reason(self):
		child = make_child(age_days=45).name
		with self.assertRaisesRegex(MCHRecordError, "reason"):
			self.give(child, [dose("BCG", NOT_GIVEN)])
		doc = self.give(child, [dose("BCG", NOT_GIVEN, reason_not_given="Out of stock")])
		self.assertIsNone(doc.doses[0].given_by)
		# Not given is not Given: BCG can still be given later.
		self.give(child, ["BCG"])

	def test_at_least_one_dose_and_no_duplicate(self):
		child = make_child(age_days=45).name
		with self.assertRaises(frappe.ValidationError):
			self.give(child, [])
		with self.assertRaisesRegex(MCHRecordError, "twice"):
			self.give(child, ["BCG", "BCG"])

	def test_inactive_vaccine_refused(self):
		frappe.db.set_value(KEPI, "IPV", "is_active", 0)
		child = make_child(age_days=100).name
		with self.assertRaisesRegex(MCHRecordError, "no longer active"):
			self.give(child, ["IPV"])

	def test_dose_already_given_refused(self):
		child = make_child(age_days=60).name
		first = self.give(child, ["Penta 1"], given_on=add_days(today(), -10))
		with self.assertRaisesRegex(MCHRecordError, f"already given.*{first.name}"):
			self.give(child, ["Penta 1"])
		# A draft does not count: two drafts are allowed, the second submit is refused.
		child2 = make_child(age_days=60).name
		draft1 = self.give(child2, ["BCG"], submit=False)
		draft2 = self.give(child2, ["BCG"], submit=False)
		draft1.submit()
		with self.assertRaisesRegex(MCHRecordError, "already given"):
			draft2.reload()
			draft2.submit()

	def test_archived_record_frees_the_dose(self):
		child = make_child(age_days=60).name
		first = self.give(child, ["BCG"])
		frappe.set_user(self.users["mic"])
		try:
			common.archive(IMM, first.name, "Wrong child")
		finally:
			frappe.set_user("Administrator")
		self.give(child, ["BCG"])  # no longer refused

	def test_too_early_refused_with_the_date(self):
		child = make_child(age_days=20).name
		dob = dob_of(child)
		# Penta 1 is due at 42 days and can be given from 28 days.
		with self.assertRaises(MCHRecordError) as ctx:
			self.give(child, ["Penta 1"])
		message = str(ctx.exception)
		self.assertIn("too early", message)
		self.assertIn(frappe.utils.formatdate(dob + timedelta(days=28)), message)
		child2 = make_child(age_days=28).name
		self.give(child2, ["Penta 1"])  # exactly 14 days before the due age

	def test_minimum_interval_refused(self):
		child = make_child(age_days=100).name
		dob = dob_of(child)
		self.give(child, ["Penta 1"], given_on=dob + timedelta(days=50))
		# Penta 2: due 70 days (from 56 days) but at least 28 days after Penta 1 (day 50) = day 78.
		with self.assertRaises(MCHRecordError) as ctx:
			self.give(child, ["Penta 2"], given_on=dob + timedelta(days=70))
		self.assertIn(frappe.utils.formatdate(dob + timedelta(days=78)), str(ctx.exception))
		self.give(child, ["Penta 2"], given_on=dob + timedelta(days=78))

	def test_dates_and_age(self):
		child = make_child(age_days=45).name
		with self.assertRaisesRegex(MCHRecordError, "future"):
			self.give(child, ["BCG"], given_on=add_days(today(), 1))
		with self.assertRaisesRegex(MCHRecordError, "before the child's date of birth"):
			self.give(child, ["BCG"], given_on=add_days(today(), -46))
		old = make_child(age_days=c.GROWTH_MAX_AGE_DAYS).name  # 5 years and over
		with self.assertRaisesRegex(MCHRecordError, "under 5"):
			self.give(old, ["BCG"])
		young = make_child(age_days=c.GROWTH_MAX_AGE_DAYS - 1).name
		self.assertEqual(self.give(young, ["BCG"]).docstatus, 1)


# =====================================================================================================
# Lifecycle (mch.common) and permissions
# =====================================================================================================
class TestImmunisationLifecycle(ImmunisationCase):
	def test_one_per_visit(self):
		child = make_child(age_days=45).name
		first = self.give(child, ["BCG"], close=False)
		with self.assertRaisesRegex(MCHRecordError, "already recorded"):
			self.give(child, ["OPV 1"], visit=first.visit)

	def test_closed_visit_refused_on_insert(self):
		child = make_child(age_days=45).name
		visit = make_mch_visit(child).name
		close_visit(visit)
		with self.assertRaisesRegex(MCHRecordError, "open visit"):
			self.give(child, ["BCG"], visit=visit)

	def test_draft_submitted_after_visit_closes(self):
		child = make_child(age_days=45).name
		doc = self.give(child, ["BCG"], submit=False)  # the visit is closed after insert
		doc.reload()
		doc.submit()
		self.assertEqual(doc.docstatus, 1)

	def test_cancel_and_edit_refused_addendum_allowed(self):
		child = make_child(age_days=45).name
		doc = self.give(child, ["BCG"], user="triage")
		doc.reload()
		with self.assertRaises(frappe.PermissionError):
			doc.cancel()
		doc.reload()
		doc.notes = "changed"
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		frappe.set_user(self.users["triage"])
		try:
			out = common.add_addendum(IMM, doc.name, "Batch number corrected: AB123")
		finally:
			frappe.set_user("Administrator")
		self.assertIn("AB123", out["addendum"])
		self.assertIn("AB123", frappe.db.get_value(IMM, doc.name, "addendum"))

	def test_archive_only_by_medical_in_charge(self):
		child = make_child(age_days=45).name
		doc = self.give(child, ["BCG"])
		for key in ("clinician", "triage", "midwife"):
			frappe.set_user(self.users[key])
			try:
				with self.assertRaises(frappe.PermissionError):
					common.archive(IMM, doc.name, "Wrong child")
			finally:
				frappe.set_user("Administrator")
		frappe.set_user(self.users["mic"])
		try:
			common.archive(IMM, doc.name, "Wrong child")
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(IMM, doc.name, "krcs_archived"), 1)

	def test_open_writes_access_log(self):
		from frappe.desk.form.load import getdoc

		child = make_child(age_days=45).name
		doc = self.give(child, ["BCG"])
		frappe.set_user(self.users["clinician"])
		try:
			getdoc(IMM, doc.name)
		finally:
			frappe.set_user("Administrator")
		rows = frappe.get_all(
			c.ACCESS_LOG_DOCTYPE,
			filters={"reference_doctype": IMM, "reference_name": doc.name, "user": self.users["clinician"]},
			fields=["action", "patient"],
		)
		self.assertEqual([(r.action, r.patient) for r in rows], [("Open", child)])

	def test_roles(self):
		child = make_child(age_days=45).name
		doc = self.give(child, ["BCG"])
		for key in ("clinician", "midwife", "mic", "triage"):
			for ptype in ("read", "create", "write", "submit"):
				self.assertTrue(frappe.has_permission(IMM, ptype, user=self.users[key]), (key, ptype))
		for key in ("his", "mho"):
			self.assertTrue(frappe.has_permission(IMM, "read", doc=doc.name, user=self.users[key]), key)
			self.assertFalse(frappe.has_permission(IMM, "create", user=self.users[key]), key)
			self.assertFalse(frappe.has_permission(IMM, "write", user=self.users[key]), key)
		self.assertFalse(frappe.has_permission(IMM, "read", user=self.users["reception"]))
		for key in self.users:
			self.assertFalse(frappe.has_permission(IMM, "delete", user=self.users[key]), key)
			self.assertFalse(frappe.has_permission(IMM, "cancel", user=self.users[key]), key)
		# Inserting as HIS or Receptionist is refused.
		for key in ("his", "reception"):
			child2 = make_child(age_days=45).name
			with self.assertRaises(frappe.PermissionError):
				self.give(child2, ["BCG"], user=key)
		# The schedule: the clinical roles and the readers, not the Receptionist.
		self.assertEqual(self.by_vaccine(child, user="his")["BCG"]["status"], c.SCHEDULE_GIVEN)
		self.assertEqual(self.by_vaccine(child, user="triage")["BCG"]["status"], c.SCHEDULE_GIVEN)
		with self.assertRaises(frappe.PermissionError):
			self.by_vaccine(child, user="reception")


# =====================================================================================================
# Schedule statuses
# =====================================================================================================
class TestSchedule(ImmunisationCase):
	def test_15_week_old_missing_penta_3(self):
		"""Birth, 6- and 10-week doses on time; the 14-week doses (due day 98) not given."""
		child = self.on_time_child(105)
		dob = dob_of(child)
		s = self.by_vaccine(child)
		self.assertEqual(s["Penta 1"]["status"], c.SCHEDULE_GIVEN)
		self.assertEqual(s["Penta 1"]["given_on"], dob + timedelta(days=42))
		self.assertTrue(s["Penta 1"]["immunisation"])
		# Penta 3 due at max(day 98, Penta 2 on day 70 + 28) = day 98: 7 days late, within the 28-day grace.
		self.assertEqual(s["Penta 3"]["due_date"], dob + timedelta(days=98))
		self.assertEqual(s["Penta 3"]["status"], c.SCHEDULE_DUE)
		self.assertEqual(s["IPV"]["status"], c.SCHEDULE_DUE)
		self.assertEqual(s["MR 1"]["status"], c.SCHEDULE_UPCOMING)
		self.assertEqual(s["MR 1"]["due_date"], dob + timedelta(days=274))
		self.assertEqual(s["OPV 0"]["status"], c.SCHEDULE_GIVEN)
		self.assertEqual(s["Penta 3"]["dob_estimated"], 0)
		# Past the grace (day 98 + 28 = 126): Overdue on day 127.
		self.assertEqual(
			self.by_vaccine(child, on=dob + timedelta(days=126))["Penta 3"]["status"], c.SCHEDULE_DUE
		)
		self.assertEqual(
			self.by_vaccine(child, on=dob + timedelta(days=127))["Penta 3"]["status"], c.SCHEDULE_OVERDUE
		)
		# Before day 98 it is Upcoming.
		self.assertEqual(
			self.by_vaccine(child, on=dob + timedelta(days=97))["Penta 3"]["status"], c.SCHEDULE_UPCOMING
		)

	def test_15_week_old_with_no_dose_after_birth(self):
		child = make_child(age_days=105).name
		dob = dob_of(child)
		on = getdate(today())
		s = self.by_vaccine(child)
		self.assertEqual(s["BCG"]["status"], c.SCHEDULE_OVERDUE)  # due at birth, no upper limit
		self.assertEqual(s["OPV 0"]["status"], c.SCHEDULE_MISSED)  # birth dose only within 14 days
		self.assertEqual(s["OPV 0"]["latest_date"], dob + timedelta(days=14))
		self.assertEqual(s["Penta 1"]["status"], c.SCHEDULE_OVERDUE)  # due day 42, overdue from day 71
		# Penta 2 waits 28 days after Penta 1, which can be given today at the earliest.
		self.assertEqual(s["Penta 2"]["due_date"], on + timedelta(days=28))
		self.assertEqual(s["Penta 2"]["status"], c.SCHEDULE_UPCOMING)
		self.assertEqual(s["Penta 3"]["due_date"], on + timedelta(days=56))
		# OPV 1 is not held back by the missed OPV 0.
		self.assertEqual(s["OPV 1"]["due_date"], dob + timedelta(days=42))
		self.assertEqual(s["OPV 1"]["status"], c.SCHEDULE_OVERDUE)

	def test_on_time_child_due_and_upcoming(self):
		child = self.on_time_child(70, upto_week10=False)
		dob = dob_of(child)
		s = self.by_vaccine(child)
		for v in WEEK10:
			self.assertEqual(s[v]["status"], c.SCHEDULE_DUE, v)  # due today
			self.assertEqual(s[v]["due_date"], dob + timedelta(days=70), v)
		for v in ("OPV 3", "Penta 3", "PCV 3", "IPV"):
			self.assertEqual(s[v]["status"], c.SCHEDULE_UPCOMING, v)
		self.assertEqual(s["Penta 3"]["due_date"], dob + timedelta(days=98))
		self.assertEqual(s["Vitamin A 1"]["status"], c.SCHEDULE_UPCOMING)
		keys = {
			"vaccine",
			"antigen",
			"dose_number",
			"due_date",
			"latest_date",
			"status",
			"given_on",
			"immunisation",
			"dob_estimated",
		}
		self.assertEqual(set(s["BCG"]), keys)

	def test_rota_missed_after_24_months_and_estimated_dob(self):
		child = make_child(age_days=800, dob_estimated=1).name
		s = self.by_vaccine(child)
		self.assertEqual(s["Rota 1"]["status"], c.SCHEDULE_MISSED)
		self.assertEqual(s["Rota 1"]["dob_estimated"], 1)

	def test_draft_and_archived_doses_do_not_count(self):
		child = make_child(age_days=50).name
		self.give(child, ["Penta 1"], submit=False)
		self.assertNotEqual(self.by_vaccine(child)["Penta 1"]["status"], c.SCHEDULE_GIVEN)


# =====================================================================================================
# Due Immunisations report
# =====================================================================================================
class TestDueImmunisationsReport(ImmunisationCase):
	def run_report(self, user: str = "clinician", **filters):
		frappe.set_user(self.users[user])
		try:
			columns, data = report.execute(filters)
		finally:
			frappe.set_user("Administrator")
		return columns, data

	def rows_for(self, child: str, **filters) -> dict:
		filters.setdefault("facility", get_facility(0))
		_cols, data = self.run_report(**filters)
		return {r["vaccine"]: r for r in data if r["patient"] == child}

	def test_report_definition(self):
		doc = frappe.get_doc("Report", c.REPORT_DUE_IMMUNISATIONS)
		self.assertEqual(doc.module, "EMR MCH")
		self.assertEqual(doc.ref_doctype, IMM)
		self.assertEqual(doc.report_type, "Script Report")
		self.assertEqual({r.role for r in doc.roles}, {*c.CHILD_MCH_ROLES, c.ROLE_HIS})

	def test_rows_statuses_and_columns(self):
		mother = make_mother(mobile="0712345678").name
		child = make_child(age_days=105).name
		frappe.db.set_value("Patient", child, c.PATIENT_MOTHER_FIELD, mother, update_modified=False)
		dob = dob_of(child)
		self.give(child, BIRTH, given_on=dob)
		last = self.give(child, WEEK6, given_on=dob + timedelta(days=50))
		rows = self.rows_for(child)
		# Penta 2: due max(70, 50 + 28 = 78) = day 78, 27 days ago: Due. Penta 3 waits for Penta 2.
		self.assertEqual(rows["Penta 2"]["status"], c.SCHEDULE_DUE)
		self.assertEqual(rows["Penta 2"]["due_date"], dob + timedelta(days=78))
		self.assertNotIn("Penta 3", rows)
		self.assertNotIn("BCG", rows)  # Given
		self.assertEqual(rows["IPV"]["status"], c.SCHEDULE_DUE)  # day 98
		r = rows["Penta 2"]
		self.assertEqual(r["mother"], mother)
		self.assertEqual(r["phone"], "0712345678")
		self.assertEqual(r["last_given_on"], getdate(last.given_on))
		self.assertEqual(r["age"], "3 months")  # 105 days
		self.assertEqual(r["sex"], "Male")
		# Overdue only: none of these
		self.assertFalse(self.rows_for(child, status=c.SCHEDULE_OVERDUE))
		# 30 days later Penta 2 is Overdue (78 + 28 = 106 < 135).
		later = add_days(today(), 30)
		rows = self.rows_for(child, from_date=later, to_date=add_days(later, 7), status=c.SCHEDULE_OVERDUE)
		self.assertEqual(rows["Penta 2"]["status"], c.SCHEDULE_OVERDUE)

	def test_upcoming_in_window_is_listed_as_due(self):
		child = make_child(age_days=38).name
		dob = dob_of(child)
		self.give(child, BIRTH, given_on=dob)
		rows = self.rows_for(child)  # today .. today + 7: the 6-week doses fall due on day 42
		self.assertEqual(rows["Penta 1"]["status"], c.SCHEDULE_DUE)
		self.assertEqual(rows["Penta 1"]["due_date"], dob + timedelta(days=42))
		rows = self.rows_for(child, to_date=add_days(today(), 2))
		self.assertNotIn("Penta 1", rows)

	def test_children_counted(self):
		# No immunisation: not listed. Archived only: not listed. Over 5 on the first day: not listed.
		never = make_child(age_days=105).name
		archived = make_child(age_days=105).name
		doc = self.give(archived, ["BCG"])
		frappe.db.set_value(IMM, doc.name, "krcs_archived", 1, update_modified=False)
		old = make_child(age_days=105).name
		self.give(old, ["BCG"])
		frappe.db.set_value("Patient", old, "dob", add_days(today(), -c.GROWTH_MAX_AGE_DAYS))
		_cols, data = self.run_report(facility=get_facility(0))
		listed = {r["patient"] for r in data}
		self.assertFalse({never, archived, old} & listed)
		# Another facility's child is not listed under this facility.
		facilities = get_facilities()
		if len(facilities) > 1:
			other = make_child(age_days=105).name
			self.give(other, ["BCG"], visit=make_mch_visit(other, facility=facilities[1]).name)
			self.assertFalse(self.rows_for(other))
			self.assertTrue(self.rows_for(other, facility=facilities[1]))

	def test_roles_and_access_log(self):
		for key in ("clinician", "midwife", "mic", "triage", "his"):
			self.run_report(user=key)
		for key in ("reception", "mho"):
			with self.assertRaises(frappe.PermissionError):
				self.run_report(user=key)
		rows = frappe.get_all(
			c.ACCESS_LOG_DOCTYPE,
			filters={
				"user": self.users["his"],
				"reference_doctype": "Report",
				"reference_name": c.REPORT_DUE_IMMUNISATIONS,
			},
			fields=["action"],
		)
		self.assertEqual([r.action for r in rows], ["Report"])

	def test_window_checks(self):
		with self.assertRaises(frappe.ValidationError):
			self.run_report(from_date=today(), to_date=add_days(today(), -1))

	def test_query_count_does_not_grow_with_children(self):
		"""No N+1: the same number of queries for 2 and for 40 children."""

		def count(n_before: int) -> tuple[int, int, float]:
			calls = []
			real = frappe.db.sql

			def spy(*args, **kw):
				calls.append(1)
				return real(*args, **kw)

			report.get_data(frappe._dict(facility=get_facility(0)))  # warm the meta cache
			start = time.monotonic()
			with patch.object(frappe.db, "sql", side_effect=spy):
				data = report.get_data(frappe._dict(facility=get_facility(0)))
			return len(calls), len(data), time.monotonic() - start

		for _i in range(2):
			self.give(make_child(age_days=105).name, ["BCG"])
		few, rows_few, _t = count(2)
		for _i in range(38):
			self.give(make_child(age_days=105).name, ["BCG"])
		many, rows_many, seconds = count(40)
		self.assertGreater(rows_many, rows_few)
		self.assertEqual(few, many)
		self.assertLess(seconds, 5)
