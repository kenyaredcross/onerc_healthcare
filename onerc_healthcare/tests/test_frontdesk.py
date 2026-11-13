# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Front desk: entrance screening, visitor records and the daily entry report (FR-41, FR-42, BR-13, C-3,
SDD §5.11)."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager

import frappe
from frappe.utils import add_days, add_to_date, now_datetime, today

from onerc_healthcare.constants import (
	REPORT_DAILY_ENTRY,
	SCREENING_DOCTYPE,
	SCREENING_PARAMETER_DOCTYPE,
	VISITOR_DOCTYPE,
)
from onerc_healthcare.emr_front_desk.doctype.visitor_record.visitor_record import VisitorRecordLockedError
from onerc_healthcare.emr_front_desk.report.daily_facility_entry import daily_facility_entry
from onerc_healthcare.frontdesk import screening, visitor
from onerc_healthcare.frontdesk import setup as frontdesk_setup
from onerc_healthcare.frontdesk.screening import ImplausibleValueError, latest_screening_for_visit
from onerc_healthcare.frontdesk.visitor import IllegalVisitorTransitionError, record_exit
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import get_facility, make_patient
from onerc_healthcare.tests.visit_factories import make_visit, make_visit_users
from onerc_healthcare.visit.patients import InactivePatientError

TEMP = "Temperature"


class FrontDeskTestCase(EMRTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users = make_visit_users()

	@contextmanager
	def as_user(self, key: str):
		frappe.set_user(self.users[key])
		try:
			yield
		finally:
			frappe.set_user("Administrator")

	def screen(self, values: dict | None = None, submit=True, user="reception", **kw):
		"""A Screening Record; `values` maps parameter -> value (default: Temperature 36.8)."""
		values = {TEMP: "36.8"} if values is None else values
		fields = {"doctype": SCREENING_DOCTYPE, "facility": get_facility(0), "person_type": "Staff"}
		fields.update(kw)
		if fields["person_type"] in ("Staff", "Other") and not fields.get("person_name"):
			fields["person_name"] = "T- Staff Member"
		fields["results"] = [{"parameter": p, "value": v} for p, v in values.items()]
		with self.as_user(user):
			doc = frappe.get_doc(fields).insert()
			if submit:
				doc.submit()
		return doc

	def new_visitor(self, user="reception", **kw):
		fields = {
			"doctype": VISITOR_DOCTYPE,
			"facility": get_facility(0),
			"visitor_name": "T- Visitor",
			"relationship": "Guardian",
		}
		fields.update(kw)
		with self.as_user(user):
			return frappe.get_doc(fields).insert()

	def parameter(self, label, param_type, **kw):
		return frappe.get_doc(
			{
				"doctype": SCREENING_PARAMETER_DOCTYPE,
				"label": label,
				"param_type": param_type,
				"active": 1,
				**kw,
			}
		).insert()


class TestScreeningParameters(FrontDeskTestCase):
	def test_temperature_seeded(self):
		"""Decision 2026-11-13: temperature in °C, flagged above 37.5 (provisional), plausible 25-45."""
		p = frappe.get_doc(SCREENING_PARAMETER_DOCTYPE, TEMP)
		self.assertEqual(
			(p.param_type, p.unit, p.high_threshold, p.low_threshold, p.plausible_min, p.plausible_max),
			("Numeric", "°C", 37.5, 0, 25, 45),
		)
		self.assertEqual((p.active, p.required), (1, 1))
		self.assertIn("PROVISIONAL", p.description)

	def test_seed_idempotent_and_keeps_admin_edits(self):
		frappe.db.set_value(SCREENING_PARAMETER_DOCTYPE, TEMP, "high_threshold", 38)
		count = frappe.db.count(SCREENING_PARAMETER_DOCTYPE)
		frontdesk_setup.install()
		self.assertEqual(frappe.db.count(SCREENING_PARAMETER_DOCTYPE), count)
		self.assertEqual(frappe.db.get_value(SCREENING_PARAMETER_DOCTYPE, TEMP, "high_threshold"), 38)

	def test_parameter_validation(self):
		with self.assertRaises(frappe.ValidationError):
			self.parameter("T-Bad Range", "Numeric", plausible_min=50, plausible_max=10)
		with self.assertRaises(frappe.ValidationError):
			self.parameter("T-Bad Thresholds", "Numeric", low_threshold=40, high_threshold=30)
		with self.assertRaises(frappe.ValidationError):
			self.parameter("T-Outside", "Numeric", plausible_min=30, plausible_max=40, high_threshold=45)
		with self.assertRaises(frappe.ValidationError):
			self.parameter("T-No Options", "Select")
		with self.assertRaises(frappe.ValidationError):
			self.parameter("T-Bad Abnormal", "Select", options="Yes\nNo", abnormal_options="Maybe")
		p = self.parameter("T-Cough", "Check", unit="x", high_threshold=3)
		self.assertEqual((p.unit, p.high_threshold), (None, 0))


class TestScreening(FrontDeskTestCase):
	def test_normal_and_flagged(self):
		"""FR-42: values above the threshold are outside the expected range."""
		normal = self.screen({TEMP: "36.8"})
		self.assertEqual(normal.outcome, "Normal")
		self.assertEqual(normal.results[0].flag, "Normal")
		self.assertEqual(normal.results[0].numeric_value, 36.8)
		self.assertEqual((normal.screened_by, bool(normal.screened_at)), (self.users["reception"], True))
		fever = self.screen({TEMP: "38,2"}, user="triage")
		self.assertEqual((fever.results[0].flag, fever.outcome), ("High", "Outside Expected Range"))
		self.assertEqual(fever.screened_by, self.users["triage"])
		edge = self.screen({TEMP: "37.5"})
		self.assertEqual(edge.outcome, "Normal")

	def test_low_threshold(self):
		self.parameter("T-SpO2", "Numeric", unit="%", low_threshold=94, plausible_min=50, plausible_max=100)
		doc = self.screen({TEMP: "36.5", "T-SpO2": "90"})
		flags = {r.parameter: r.flag for r in doc.results}
		self.assertEqual(flags, {TEMP: "Normal", "T-SpO2": "Low"})
		self.assertEqual(doc.outcome, "Outside Expected Range")

	def test_implausible_or_invalid_values_refused(self):
		for value in ("375", "12", "abc"):
			with self.assertRaises(ImplausibleValueError):
				self.screen({TEMP: value})

	def test_select_and_check_parameters(self):
		self.parameter("T-Rash", "Select", options="None\nMild\nSevere", abnormal_options="Severe")
		self.parameter("T-Cough", "Check")
		doc = self.screen({TEMP: "36.6", "T-Rash": "Mild", "T-Cough": "no"})
		self.assertEqual(
			{r.parameter: r.flag for r in doc.results},
			{TEMP: "Normal", "T-Rash": "Normal", "T-Cough": "Normal"},
		)
		self.assertEqual(doc.results[2].value, "No")
		doc = self.screen({TEMP: "36.6", "T-Rash": "Severe", "T-Cough": "Yes"})
		self.assertEqual(
			{r.parameter: r.flag for r in doc.results},
			{TEMP: "Normal", "T-Rash": "Abnormal", "T-Cough": "Abnormal"},
		)
		self.assertEqual(doc.outcome, "Outside Expected Range")
		with self.assertRaises(ImplausibleValueError):
			self.screen({TEMP: "36.6", "T-Rash": "Purple"})
		with self.assertRaises(ImplausibleValueError):
			self.screen({TEMP: "36.6", "T-Cough": "perhaps"})

	def test_required_parameter_needs_value(self):
		with self.assertRaises(frappe.ValidationError):
			self.screen({TEMP: ""})
		self.parameter("T-Optional", "Numeric")
		with self.assertRaises(frappe.ValidationError):
			self.screen({"T-Optional": "5"})  # Temperature row missing

	def test_new_record_starts_with_active_parameters(self):
		self.parameter("T-Weight", "Numeric", unit="kg", display_order=2)
		self.parameter("T-Inactive", "Numeric", display_order=3, active=0)
		with self.as_user("reception"):
			doc = frappe.get_doc(
				{
					"doctype": SCREENING_DOCTYPE,
					"facility": get_facility(0),
					"person_type": "Other",
					"person_name": "T- X",
				}
			).insert()
		params = [r.parameter for r in doc.results]
		self.assertIn(TEMP, params)
		self.assertIn("T-Weight", params)
		self.assertNotIn("T-Inactive", params)
		self.assertIn("T-Weight", [p["name"] for p in screening.get_active_parameters()])
		with self.assertRaises(frappe.ValidationError):
			self.screen({TEMP: "36.5", "T-Inactive": "1"})
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": SCREENING_DOCTYPE,
					"facility": get_facility(0),
					"person_type": "Other",
					"person_name": "T- X",
					"results": [{"parameter": TEMP, "value": "36"}, {"parameter": TEMP, "value": "37"}],
				}
			).insert()

	def test_snapshot_frozen_at_submit(self):
		"""Changing a parameter later never alters a submitted record."""
		doc = self.screen({TEMP: "37.8"})
		frappe.db.set_value(SCREENING_PARAMETER_DOCTYPE, TEMP, {"high_threshold": 38.5, "unit": "C"})
		doc.reload()
		row = doc.results[0]
		self.assertEqual((row.label, row.unit, row.high_threshold, row.plausible_max), (TEMP, "°C", 37.5, 45))
		self.assertEqual((row.flag, doc.outcome), ("High", "Outside Expected Range"))
		# A draft takes the parameter's current values until it is submitted.
		draft = self.screen({TEMP: "37.8"}, submit=False)
		self.assertEqual((draft.results[0].high_threshold, draft.results[0].flag), (38.5, "Normal"))

	def test_submitted_record_is_permanent(self):
		doc = self.screen()
		with self.as_user("reception"):
			doc.reload()
			doc.results[0].value = "39"
			with self.assertRaises(frappe.ValidationError):
				doc.save()
			with self.assertRaises(frappe.PermissionError):
				doc.reload()
				doc.cancel()
		with self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
			frappe.delete_doc(SCREENING_DOCTYPE, doc.name, ignore_permissions=True)
		draft = self.screen(submit=False)
		with self.assertRaises(frappe.PermissionError):  # delete guard (NFR-9)
			frappe.delete_doc(SCREENING_DOCTYPE, draft.name, ignore_permissions=True)

	def test_person_types(self):
		patient = make_patient().name
		visit = make_visit(patient=patient)
		doc = self.screen(
			person_type="Patient",
			patient=patient,
			visit=visit.name,
			person_name="T- ignored",
			visitor_record=None,
		)
		self.assertEqual(doc.person_name, frappe.db.get_value("Patient", patient, "patient_name"))
		self.assertEqual(latest_screening_for_visit(visit.name), doc.name)
		# Visit only: the patient is taken from it.
		self.assertEqual(self.screen(person_type="Patient", visit=visit.name).patient, patient)
		with self.assertRaises(frappe.ValidationError):
			self.screen(person_type="Patient", patient=make_patient().name, visit=visit.name)
		with self.assertRaises(frappe.ValidationError):
			self.screen(person_type="Patient", patient=patient, visit=visit.name, facility=get_facility(1))
		with self.assertRaises(frappe.ValidationError):
			self.screen(person_type="Patient")
		# Staff: patient links are cleared, a name is required.
		staff = self.screen(person_type="Staff", person_name="T- Nurse", patient=patient, visit=visit.name)
		self.assertEqual((staff.patient, staff.visit), (None, None))
		with self.assertRaises(frappe.ValidationError):
			self.screen(person_type="Other", person_name=" ")
		# Visitor: from the Visitor Record (name, facility) or by name.
		vr = self.new_visitor(visitor_name="T- Amina Guardian")
		vdoc = self.screen(person_type="Visitor", visitor_record=vr.name, facility=None)
		self.assertEqual((vdoc.person_name, vdoc.facility), ("T- Amina Guardian", get_facility(0)))
		self.assertEqual(screening.latest_screening_for_visitor(vr.name), vdoc.name)
		self.assertEqual(self.screen(person_type="Visitor", person_name="T- Walk-in").visitor_record, None)

	def test_inactive_patient_refused(self):
		patient = make_patient().name
		frappe.db.set_value("Patient", patient, "status", "Disabled")
		with self.assertRaises(InactivePatientError):
			self.screen(person_type="Patient", patient=patient)


class TestScreeningRoles(FrontDeskTestCase):
	def test_docperms(self):
		"""Receptionist and Triage Nurse rwcs; MIC read; parameters MIC rwc; never delete; no SysAdmin."""
		perms = {}
		for name in ("screening_record", "screening_parameter", "visitor_record"):
			path = os.path.join(
				frappe.get_app_path("onerc_healthcare"), "emr_front_desk", "doctype", name, f"{name}.json"
			)
			with open(path) as f:
				perms[name] = {p["role"]: p for p in json.load(f)["permissions"]}
		for name, by_role in perms.items():
			self.assertNotIn("EMR System Administrator", by_role, name)
			self.assertNotIn("Head of Operations", by_role, name)
			for role, p in by_role.items():
				self.assertFalse(p.get("delete") or p.get("cancel") or p.get("amend"), (name, role))
		rec = perms["screening_record"]
		self.assertEqual(set(rec), {"Nurse Receptionist", "Triage Nurse", "Medical In-Charge"})
		for role in ("Nurse Receptionist", "Triage Nurse"):
			self.assertTrue(all(rec[role].get(k) for k in ("read", "write", "create", "submit")), role)
		self.assertFalse(rec["Medical In-Charge"].get("create"))
		par = perms["screening_parameter"]
		self.assertTrue(all(par["Medical In-Charge"].get(k) for k in ("read", "write", "create")))
		self.assertFalse(par["Nurse Receptionist"].get("write"))
		vis = perms["visitor_record"]
		self.assertTrue(all(vis["Nurse Receptionist"].get(k) for k in ("read", "write", "create")))
		self.assertFalse(vis["Triage Nurse"].get("create"))

	def test_role_matrix(self):
		doc = self.screen()
		for key in ("reception", "triage"):
			self.screen(user=key)
		with self.as_user("mic"):
			self.assertTrue(frappe.has_permission(SCREENING_DOCTYPE, "read", doc=doc.name))
		for key in ("mic", "clinician", "pharmacist", "hoo", "sysadmin"):
			with self.assertRaises(frappe.PermissionError):
				self.screen(user=key)
		for key in ("clinician", "lab", "pharmacist", "hoo", "sysadmin"):
			with self.as_user(key):
				self.assertFalse(frappe.has_permission(SCREENING_DOCTYPE, "read", doc=doc.name), key)
		with self.as_user("mic"):
			self.parameter("T-MIC Param", "Check")
		for key in ("reception", "triage"):
			with self.as_user(key), self.assertRaises(frappe.PermissionError):
				self.parameter(f"T-{key}", "Check")
		with self.as_user("sysadmin"), self.assertRaises(frappe.PermissionError):
			screening.get_active_parameters()


class TestVisitorRecord(FrontDeskTestCase):
	def test_entry_and_exit(self):
		"""FR-41: Inside -> Exited through record_exit; the log is appended."""
		vr = self.new_visitor()
		self.assertEqual((vr.status, vr.exit_time), ("Inside", None))
		self.assertEqual(
			[(r.from_status, r.to_status, r.action) for r in vr.status_log],
			[(None, "Inside", "record_entry")],
		)
		with self.as_user("reception"):
			result = record_exit(vr.name, comment="T- left with patient")
		self.assertEqual(result["status"], "Exited")
		vr.reload()
		self.assertTrue(vr.exit_time)
		self.assertEqual(
			[(r.from_status, r.to_status, r.action) for r in vr.status_log],
			[(None, "Inside", "record_entry"), ("Inside", "Exited", "record_exit")],
		)
		self.assertEqual(vr.status_log[1].user, self.users["reception"])
		self.assertEqual(vr.status_log[1].comment, "T- left with patient")

	def test_transition_matrix(self):
		"""Every TRANSITIONS action from every status, allowed and denied, for each role."""
		self.assertEqual(set(visitor.TRANSITIONS), {visitor.ACTION_RECORD_EXIT})
		for action, rule in visitor.TRANSITIONS.items():
			for status in visitor.STATUSES:
				for key in ("reception", "triage", "mic", "clinician", "hoo", "sysadmin"):
					vr = self.new_visitor()
					if status == visitor.STATUS_EXITED:
						visitor.transition(vr.name, visitor.ACTION_RECORD_EXIT)
					allowed_role = frappe.db.get_value(
						"Has Role", {"parent": self.users[key], "role": ["in", rule.roles]}
					)
					with self.as_user(key):
						if not allowed_role:
							with self.assertRaises(frappe.PermissionError, msg=(action, status, key)):
								visitor.transition(vr.name, action)
						elif status not in rule.from_statuses:
							with self.assertRaises(IllegalVisitorTransitionError, msg=(action, status, key)):
								visitor.transition(vr.name, action)
						else:
							self.assertEqual(visitor.transition(vr.name, action).status, rule.to_status)
		with self.assertRaises(IllegalVisitorTransitionError):
			visitor.transition(self.new_visitor().name, "readmit")

	def test_exit_time_rules(self):
		vr = self.new_visitor(entry_time=add_to_date(now_datetime(), hours=-2))
		with self.as_user("reception"):
			with self.assertRaises(IllegalVisitorTransitionError):
				record_exit(vr.name, exit_time=str(add_to_date(now_datetime(), hours=-3)))
			with self.assertRaises(IllegalVisitorTransitionError):
				record_exit(vr.name, exit_time=str(add_to_date(now_datetime(), hours=2)))
			record_exit(vr.name, exit_time=str(add_to_date(now_datetime(), hours=-1)))
		with self.assertRaises(frappe.ValidationError):
			self.new_visitor(entry_time=add_to_date(now_datetime(), hours=3))

	def test_status_and_log_protected(self):
		vr = self.new_visitor()
		with self.as_user("reception"):
			doc = frappe.get_doc(VISITOR_DOCTYPE, vr.name)
			doc.status = "Exited"
			with self.assertRaises(VisitorRecordLockedError):
				doc.save()
			doc.reload()
			doc.status_log[0].action = "edited"
			with self.assertRaises(VisitorRecordLockedError):
				doc.save()
			doc.reload()
			doc.append(
				"status_log",
				{"to_status": "Exited", "action": "x", "user": "Administrator", "timestamp": now_datetime()},
			)
			with self.assertRaises(VisitorRecordLockedError):
				doc.save()
			doc.reload()
			doc.relationship = "Parent"
			doc.save()  # ordinary corrections while Inside are allowed
			record_exit(vr.name)
			doc.reload()
			doc.relationship = "Spouse"
			with self.assertRaises(VisitorRecordLockedError):
				doc.save()
		if frappe.get_meta(VISITOR_DOCTYPE).has_field("krcs_archived"):
			doc.reload()
			doc.krcs_archived = 1
			doc.save(ignore_permissions=True)  # archiving (NFR-9) stays possible after exit

	def test_patient_link_and_minimal_data(self):
		"""C-3: name, relationship, optional identifier, patient accompanied; nothing more."""
		patient = make_patient().name
		visit = make_visit(patient=patient)
		vr = self.new_visitor(visit=visit.name)
		self.assertEqual(vr.patient, patient)
		with self.assertRaises(frappe.ValidationError):
			self.new_visitor(patient=make_patient().name, visit=visit.name)
		with self.assertRaises(frappe.ValidationError):
			self.new_visitor(identifier_value="T-123")
		data_fields = {
			f.fieldname
			for f in frappe.get_meta(VISITOR_DOCTYPE).fields
			if f.fieldtype not in ("Section Break", "Column Break", "Table")
		}
		self.assertEqual(
			data_fields,
			{
				"naming_series",
				"facility",
				"visitor_name",
				"relationship",
				"identifier_type",
				"identifier_value",
				"patient",
				"patient_name",
				"visit",
				"status",
				"entry_time",
				"exit_time",
			}
			| ({"krcs_archived"} if frappe.get_meta(VISITOR_DOCTYPE).has_field("krcs_archived") else set()),
		)

	def test_roles(self):
		for key in ("triage", "mic", "clinician", "hoo", "sysadmin"):
			with self.assertRaises(frappe.PermissionError):
				self.new_visitor(user=key)
		vr = self.new_visitor()
		for key in ("triage", "mic"):
			with self.as_user(key):
				self.assertTrue(frappe.has_permission(VISITOR_DOCTYPE, "read", doc=vr.name))
		for key in ("clinician", "hoo", "sysadmin", "pharmacist"):
			with self.as_user(key):
				self.assertFalse(frappe.has_permission(VISITOR_DOCTYPE, "read", doc=vr.name), key)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc(VISITOR_DOCTYPE, vr.name, ignore_permissions=True)


class TestDailyEntryReport(FrontDeskTestCase):
	def setUp(self):
		super().setUp()
		self.patient = make_patient().name
		self.visit = make_visit(patient=self.patient).name
		self.visit_screening = self.screen({TEMP: "38.4"}, person_type="Patient", visit=self.visit)
		self.visitor = self.new_visitor(patient=self.patient, visit=self.visit, visitor_name="T- Companion")
		self.visitor_screening = self.screen(person_type="Visitor", visitor_record=self.visitor.name)
		self.staff = self.screen(person_type="Staff", person_name="T- Cleaner")
		self.elsewhere = self.screen(
			person_type="Other", person_name="T- Elsewhere", facility=get_facility(1)
		)

	def run_report(self, user: str, **filters):
		from frappe.desk.query_report import run

		filters = {"date": today(), "facility": get_facility(0), **filters}
		with self.as_user(user):
			return run(REPORT_DAILY_ENTRY, filters=filters)

	def test_full_list(self):
		"""BR-13: visits, visitors and other screenings with times and screening outcome."""
		result = self.run_report("reception")
		rows = {r["reference_name"]: r for r in result["result"]}
		visit_row = rows[self.visit]
		self.assertEqual((visit_row["person_type"], visit_row["patient"]), ("Patient", self.patient))
		self.assertEqual(visit_row["screening_outcome"], "Outside Expected Range")
		self.assertTrue(visit_row["time_in"])
		visitor_row = rows[self.visitor.name]
		self.assertEqual(
			(visitor_row["person_type"], visitor_row["person_name"]), ("Visitor", "T- Companion")
		)
		self.assertEqual((visitor_row["status"], visitor_row["screening_outcome"]), ("Inside", "Normal"))
		self.assertEqual(rows[self.staff.name]["person_type"], "Staff")
		# Screenings linked to a listed visit / visitor are not repeated as their own rows.
		self.assertNotIn(self.visit_screening.name, rows)
		self.assertNotIn(self.visitor_screening.name, rows)
		self.assertNotIn(self.elsewhere.name, rows)
		self.assertIn(
			self.elsewhere.name,
			[r["reference_name"] for r in self.run_report("mic", facility=get_facility(1))["result"]],
		)
		self.assertNotIn(
			self.visit,
			[r["reference_name"] for r in self.run_report("triage", date=add_days(today(), -1))["result"]],
		)

	def test_head_of_operations_sees_counts_only(self):
		full = self.run_report("mic")["result"]
		result = self.run_report("hoo")
		self.assertEqual([c["fieldname"] for c in result["columns"]], ["category", "count"])
		counts = {r["category"]: r["count"] for r in result["result"]}
		for person in ("Patient", "Visitor", "Staff", "Other"):
			self.assertEqual(counts[person], sum(1 for r in full if r["person_type"] == person), person)
		self.assertEqual(counts["Total present"], len(full))
		self.assertGreaterEqual(counts["Screened outside expected range"], 1)
		dump = json.dumps(result["result"], default=str)
		for secret in ("T- Companion", "T- Cleaner", self.patient, self.visit):
			self.assertNotIn(secret, dump)

	def test_report_roles(self):
		for key in ("clinician", "pharmacist", "sysadmin", "lab"):
			with self.assertRaises(frappe.PermissionError):
				self.run_report(key)
		self.assertFalse(daily_facility_entry.counts_only("Administrator"))
		self.assertTrue(daily_facility_entry.counts_only(self.users["hoo"]))
		self.assertFalse(daily_facility_entry.counts_only(self.users["reception"]))
