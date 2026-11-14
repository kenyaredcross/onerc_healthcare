# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""R4: MOH 717 workload sources, MOH 711 rehabilitation / social work and the blocked definitions
(FR-29, FR-30, BR-9; SDD §5.8, §5.12.7). Never commit.

Each test builds its own records in March 2001 at the site's first facility (nothing else lives there),
calls the evaluator directly with a `SourceContext` and compares with counts worked out by hand in the
comments. Fixture rows are written with ``db_insert`` (no controllers or hooks): the evaluators only read
the tables, so the tests pin the counting rules, not the capture logic (which R3 and the Phase 2 builders
test). R3's DocTypes are used when they exist; otherwise those tests are skipped.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.utils import get_datetime

from onerc_healthcare import constants as c
from onerc_healthcare.reporting import ages, registry, workload
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import get_company_name, get_facility, make_patient

FROM, TO = date(2001, 3, 1), date(2001, 3, 31)
BANDS = (ages.AgeBand("<5", 0, 60), ages.AgeBand("5+", 60))
AS = registry.DISAGGREGATION_AGE_SEX
NONE = registry.DISAGGREGATION_NONE
DATA = Path(workload.__file__).parent / "data"
R4_FILES = (
	"moh_717.json",
	"moh_711_rehab.json",
	"moh_711_rh.json",
	"moh_747a.json",
	"moh_643b.json",
	"mh_clinic.json",
)


def _row(doctype: str, **values) -> str:
	"""Insert one row as it is, without controllers or hooks (fixtures for read-only evaluators)."""
	doc = frappe.get_doc({"doctype": doctype, **values})
	doc.name = values.get("name") or f"T-R4-{frappe.generate_hash(length=10)}"
	doc.db_insert()
	return doc.name


def el(code, source, rule=None, dis=NONE) -> registry.ElementSpec:
	return registry.ElementSpec(code, code, source, rule or {}, dis)


class R4Case(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.f0, self.f1 = get_facility(0), get_facility(1)
		self.company = get_company_name()
		self.child_f = make_patient(sex="Female", dob="1999-06-15").name  # 20-21 months in March 2001
		self.child_m = make_patient(sex="Male", dob="2000-01-10").name
		self.adult_f = make_patient(sex="Female", dob="1970-05-05").name
		self.adult_m = make_patient(sex="Male", dob="1980-02-02").name

	def ctx(self, facility=None) -> registry.SourceContext:
		return registry.SourceContext(
			report="T-R4", from_date=FROM, to_date=TO, facility=facility or self.f0, age_bands=BANDS
		)

	def visit(self, patient, on, facility=None, attendance=c.ATTENDANCE_NEW, routes=(), **fields) -> str:
		"""A Patient Visit row; `routes` are (action, department) status-log rows in order."""
		name = _row(
			c.VISIT_DOCTYPE,
			patient=patient,
			facility=facility or self.f0,
			company=self.company,
			visit_date=on,
			status=fields.pop("status", c.VISIT_STATUS_DISCHARGED),
			attendance_type=attendance,
			current_department=fields.pop("current_department", routes[-1][1] if routes else None),
			**fields,
		)
		for idx, (action, department) in enumerate(routes, 1):
			_row(
				"Patient Visit Status Log",
				parent=name,
				parenttype=c.VISIT_DOCTYPE,
				parentfield="status_log",
				idx=idx,
				action=action,
				department=department,
				from_status="",
				to_status="",
			)
		return name

	def encounter(self, visit, on, docstatus=1) -> str:
		patient = frappe.db.get_value(c.VISIT_DOCTYPE, visit, "patient")
		return _row(
			c.ENCOUNTER_DOCTYPE, patient=patient, krcs_visit=visit, encounter_date=on, docstatus=docstatus
		)

	def kinds(self, ctx) -> list[tuple[str, str | None, str | None]]:
		return [(e["kind"], e["reference_name"], e["element"]) for e in ctx.exceptions]


class TestAttendance(R4Case):
	def test_new_and_reattendance_by_department_age_and_sex(self):
		"""FR-29 / MOH 717 attendance: department = last routed department; base filters; period edges."""
		opd = [("route", c.DEPT_OUTPATIENT)]
		no_dob = make_patient(sex="Female", dob="1990-01-01").name
		frappe.db.set_value("Patient", no_dob, "dob", None, update_modified=False)
		v1 = self.visit(self.child_f, "2001-03-01", routes=opd)  # first day
		self.visit(self.adult_m, "2001-03-31", routes=opd)  # last day
		self.visit(self.adult_m, "2001-03-15", attendance=c.ATTENDANCE_REVISIT, routes=opd)
		self.visit(self.adult_f, "2001-02-28", routes=opd)  # before the period
		self.visit(self.adult_f, "2001-04-01", routes=opd)  # after the period
		self.visit(self.child_m, "2001-03-10", routes=opd, closure_reason="Registered in error")
		self.visit(self.child_m, "2001-03-10", routes=opd, closure_reason="Superseded by a new visit")
		self.visit(self.child_m, "2001-03-10", routes=opd, facility=self.f1)  # another facility
		# Routed to OPD, re-routed to Mental Health, sent back to triage and closed there: Mental Health.
		self.visit(
			self.adult_f,
			"2001-03-12",
			routes=[
				("route", c.DEPT_OUTPATIENT),
				("reroute", c.DEPT_MENTAL_HEALTH),
				("return_to_triage", "Triage"),
			],
		)
		# Never routed, left before being seen.
		self.visit(
			self.child_f, "2001-03-20", current_department="Triage", closure_reason="Left before being seen"
		)
		self.visit(self.adult_f, "2001-03-21", routes=opd, is_outreach=1)
		# Internal referral target visit (Re-attendance by the calendar rule).
		self.visit(
			self.adult_m,
			"2001-03-22",
			attendance=c.ATTENDANCE_REVISIT,
			routes=[("receive_referral", c.DEPT_MENTAL_HEALTH)],
			referred_from="T-R4-REF-X",
		)
		self.visit(no_dob, "2001-03-05", routes=opd)
		untyped = self.visit(self.adult_m, "2001-03-06", attendance=None, routes=opd)

		not_seen = ["Left before being seen"]
		elements = [
			el("OPD-N", "Visit Attendance", {"attendance_type": "New", "departments": ["Outpatient"]}, AS),
			el(
				"OPD-R",
				"Visit Attendance",
				{"attendance_type": "Re-attendance", "departments": ["Outpatient"]},
				AS,
			),
			el("MH-N", "Visit Attendance", {"attendance_type": "New", "departments": ["Mental Health"]}, AS),
			el(
				"MH-R",
				"Visit Attendance",
				{"attendance_type": "Re-attendance", "departments": ["Mental Health"]},
				AS,
			),
			el("OUT-N", "Visit Attendance", {"attendance_type": "New", "outreach": "only"}),
			el(
				"TOT-N",
				"Visit Attendance",
				{
					"attendance_type": "New",
					"internal_referrals": "exclude",
					"exclude_closure_reasons": not_seen,
				},
			),
			el(
				"TOT-R",
				"Visit Attendance",
				{"attendance_type": "Re-attendance", "internal_referrals": "exclude"},
			),
			el("ALL", "Visit Attendance"),
			el("ALL-P", "Visit Attendance", {"unit": "patients"}),
			el("ALL-SEX", "Visit Attendance", {"outreach": "include"}, registry.DISAGGREGATION_SEX),
		]
		ctx = self.ctx()
		got = workload.evaluate_attendance(ctx, elements)
		expected = {
			# OPD new: child_f on the 1st, adult_m on the 31st, the patient without a DOB.
			("OPD-N", "<5", "Female"): 1,
			("OPD-N", "5+", "Male"): 1,
			("OPD-N", ages.UNKNOWN_BAND, "Female"): 1,
			("OPD-R", "5+", "Male"): 1,
			("MH-N", "5+", "Female"): 1,  # re-routed visit
			("MH-R", "5+", "Male"): 1,  # internal referral target
			("OUT-N", "", ""): 1,
			# New, not outreach, not left-before-seen: 1st, 31st, re-routed, no-DOB.
			("TOT-N", "", ""): 4,
			("TOT-R", "", ""): 1,  # the internal referral target is left out
			# Every in-scope visit except outreach: 1st, 31st, 15th, re-routed, left, referral, no-DOB, untyped.
			("ALL", "", ""): 8,
			("ALL-P", "", ""): 4,  # child_f, adult_m, adult_f, no-DOB patient
			# ALL plus outreach (adult_f): Female = child_f x2, adult_f x2, no-DOB; Male = adult_m x4.
			("ALL-SEX", "", "Female"): 5,
			("ALL-SEX", "", "Male"): 4,
		}
		self.assertEqual(got, expected)
		kinds = self.kinds(ctx)
		self.assertIn(("Attendance type missing", untyped, None), kinds)
		self.assertIn(("Age unknown", None, "OPD-N"), kinds)
		self.assertNotIn(v1, [k[1] for k in kinds])

	def test_other_facility_context(self):
		self.visit(self.child_m, "2001-03-10", routes=[("route", c.DEPT_OUTPATIENT)], facility=self.f1)
		self.visit(self.child_m, "2001-03-10", routes=[("route", c.DEPT_OUTPATIENT)])
		got = workload.evaluate_attendance(self.ctx(self.f1), [el("ALL", "Visit Attendance")])
		self.assertEqual(got, {("ALL", "", ""): 1})

	def test_invalid_rule_is_skipped_and_reported(self):
		self.visit(self.adult_m, "2001-03-10")
		ctx = self.ctx()
		got = workload.evaluate_attendance(
			ctx, [el("BAD", "Visit Attendance", {"attendance_type": "First"}), el("OK", "Visit Attendance")]
		)
		self.assertEqual(got, {("OK", "", ""): 1})
		self.assertEqual(ctx.exceptions[0]["kind"], "Invalid rule")
		self.assertEqual(ctx.exceptions[0]["element"], "BAD")


class TestLaboratory(R4Case):
	def setUp(self):
		super().setUp()
		for template in (
			"Haemoglobin",
			"Full Haemogram (CBC)",
			"Malaria Rapid Diagnostic Test",
			"Stool Microscopy",
		):
			if not frappe.db.exists("Observation Template", template):
				self.skipTest(f"lab catalogue template {template} missing")

	def obs(self, template, docstatus, approved=None, **kw) -> str:
		values = {"observation_template": template, "docstatus": docstatus, "time_of_approval": approved}
		values.update(kw)
		return _row("Observation", **values)

	def test_tests_done_counts_approved_tests_once(self):
		"""Approved vs draft; a panel is one test; the three encounter links; rejected and out of scope."""
		visit = self.visit(self.adult_f, "2001-03-10", routes=[("route", c.DEPT_OUTPATIENT)])
		enc = self.encounter(visit, "2001-03-10", docstatus=0)  # results arrive before completion
		ref = {"reference_doctype": c.ENCOUNTER_DOCTYPE, "reference_docname": enc}
		self.obs("Haemoglobin", 1, "2001-03-10 10:00:00", **ref)  # counted
		self.obs("Malaria Rapid Diagnostic Test", 0, None, **ref)  # draft: not counted
		panel = self.obs("Full Haemogram (CBC)", 0, None, has_component=1, **ref)
		for day in (11, 12):  # two approved components: one test, dated the 11th
			self.obs("Haemoglobin", 1, f"2001-03-{day} 09:00:00", parent_observation=panel)
		sr = _row(
			"Service Request",
			source_doc=c.ENCOUNTER_DOCTYPE,
			order_group=enc,
			template_dt="Observation Template",
			template_dn="Malaria Rapid Diagnostic Test",
			docstatus=1,
		)
		self.obs("Malaria Rapid Diagnostic Test", 1, "2001-03-12 08:00:00", service_request=sr)  # counted
		sc = _row("Sample Collection", reference_doc=c.ENCOUNTER_DOCTYPE, reference_name=enc, docstatus=1)
		# No approval time: the modified date stands in.
		self.obs(
			"Stool Microscopy",
			1,
			None,
			reference_doctype="Sample Collection",
			reference_docname=sc,
			creation="2001-03-13 09:00:00",
			modified="2001-03-13 09:00:00",
		)
		self.obs("Haemoglobin", 2, "2001-03-14 10:00:00", **ref)  # rejected
		self.obs("Haemoglobin", 1, "2001-04-02 10:00:00", **ref)  # approved after the period
		other = self.visit(self.adult_m, "2001-03-10", facility=self.f1)
		other_enc = self.encounter(other, "2001-03-10")
		self.obs(
			"Haemoglobin",
			1,
			"2001-03-15 10:00:00",
			reference_doctype=c.ENCOUNTER_DOCTYPE,
			reference_docname=other_enc,
		)

		haem = ["Haemoglobin", "Full Haemogram (CBC)", "Erythrocyte Sedimentation Rate"]
		elements = [
			el("LAB", "Laboratory Tests"),
			el("HAEM", "Laboratory Tests", {"templates": haem}),
			el("MAL", "Laboratory Tests", {"templates": ["Malaria Rapid Diagnostic Test"]}),
			el("PAT", "Laboratory Tests", {"unit": "patients"}),
			el("LAB-AS", "Laboratory Tests", {}, AS),
			el("MH", "Laboratory Tests", {"departments": ["Mental Health"]}),
		]
		got = workload.evaluate_lab_tests(self.ctx(), elements)
		self.assertEqual(
			got,
			{
				("LAB", "", ""): 4,  # Hb, CBC panel, MRDT via the request, stool via the sample
				("HAEM", "", ""): 2,
				("MAL", "", ""): 1,
				("PAT", "", ""): 1,
				("LAB-AS", "5+", "Female"): 4,
			},
		)

	def test_late_note_does_not_move_the_test_to_the_next_month(self):
		"""Review L1: approved ("Approved", not "Final") on the 31st through a real submit, then a note on the
		2nd written the way Marley's ``add_note`` does (``db.set_value``, which bumps ``modified``)."""
		visit = self.visit(self.adult_f, "2001-03-30")
		enc = self.encounter(visit, "2001-03-30")
		obs = frappe.get_doc(
			{
				"doctype": "Observation",
				"observation_template": "Haemoglobin",
				"patient": self.adult_f,
				"reference_doctype": c.ENCOUNTER_DOCTYPE,
				"reference_docname": enc,
				"result": "12.5",
				"status": "Approved",
			}
		)
		approved_at = get_datetime("2001-03-31 16:00:00")  # the approval clock (freezegun is not installed)
		with patch("onerc_healthcare.lab.events.now_datetime", return_value=approved_at):
			obs.insert(ignore_permissions=True)
			obs.submit()
		# The note as Marley's add_note writes it, two days later: set_value bumps `modified`.
		frappe.db.set_value(
			"Observation", obs.name, "note", "<p>T- repeat advised</p>", modified="2001-04-02 09:00:00"
		)
		row = frappe.db.get_value("Observation", obs.name, ["time_of_approval", "modified"], as_dict=True)
		self.assertEqual(str(row.time_of_approval)[:10], "2001-03-31")  # stamped at submit
		self.assertEqual(str(row.modified)[:10], "2001-04-02")
		march = workload.evaluate_lab_tests(self.ctx(), [el("LAB", "Laboratory Tests")])
		april = registry.SourceContext(
			report="T-R4", from_date=date(2001, 4, 1), to_date=date(2001, 4, 30), facility=self.f0
		)
		self.assertEqual(march, {("LAB", "", ""): 1})
		self.assertEqual(workload.evaluate_lab_tests(april, [el("LAB", "Laboratory Tests")]), {})

	def test_approval_backfill_patch(self):
		"""The patch fills empty approval times from the submit Version, else ``modified``; idempotent."""
		from onerc_healthcare.patches import backfill_observation_approval_time as backfill

		with_version = self.obs("Haemoglobin", 1, None, modified="2001-04-05 10:00:00", creation="2001-03-01")
		_row(
			"Version",
			ref_doctype="Observation",
			docname=with_version,
			data=json.dumps({"changed": [["status", "Preliminary", "Approved"], ["docstatus", 0, 1]]}),
			creation="2001-03-31 16:00:00",
			modified="2001-03-31 16:00:00",
		)
		without = self.obs("Haemoglobin", 1, None, modified="2001-03-20 08:00:00", creation="2001-03-01")
		stamped = self.obs("Haemoglobin", 1, "2001-03-02 08:00:00")
		draft = self.obs("Haemoglobin", 0, None)
		backfill.execute()
		backfill.execute()  # idempotent
		got = {
			n: str(frappe.db.get_value("Observation", n, "time_of_approval"))
			for n in (with_version, without, stamped, draft)
		}
		self.assertEqual(
			got,
			{
				with_version: "2001-03-31 16:00:00",
				without: "2001-03-20 08:00:00",
				stamped: "2001-03-02 08:00:00",
				draft: "None",
			},
		)


class TestPharmacy(R4Case):
	def mr(self, encounter, docstatus=1) -> str:
		return _row("Medication Request", order_group=encounter, docstatus=docstatus)

	def dispense(self, visit, encounter, mr, at, outcome="Dispensed", docstatus=1) -> str:
		return _row(
			c.DISPENSING_DOCTYPE,
			visit=visit,
			encounter=encounter,
			medication_request=mr,
			outcome=outcome,
			dispensed_at=at,
			docstatus=docstatus,
		)

	def test_prescriptions_vs_dispensing(self):
		v1 = self.visit(self.adult_m, "2001-03-10")
		e1 = self.encounter(v1, "2001-03-10")
		mr1, mr2 = self.mr(e1), self.mr(e1)
		self.mr(e1, docstatus=0)  # draft line
		v2 = self.visit(self.child_f, "2001-03-31")
		e2 = self.encounter(v2, "2001-03-31")
		mr4 = self.mr(e2)
		v3 = self.visit(self.adult_f, "2001-03-05")
		self.mr(self.encounter(v3, "2001-03-05", docstatus=0))  # encounter not submitted
		v4 = self.visit(self.adult_f, "2001-03-31")
		self.mr(self.encounter(v4, "2001-04-01"))  # encounter after the period

		self.dispense(v1, e1, mr1, "2001-03-10 10:00:00")
		self.dispense(v1, e1, mr1, "2001-03-11 10:00:00")  # second partial issue of the same line
		self.dispense(v1, e1, mr2, "2001-03-10 10:05:00", outcome="Not Dispensed")
		self.dispense(v2, e2, mr4, "2001-03-31 12:00:00", docstatus=2)  # cancelled
		self.dispense(v2, e2, mr4, "2001-04-01 08:00:00")  # after the period

		got = workload.evaluate_prescriptions(
			self.ctx(),
			[
				el("RX", "Prescriptions"),
				el("RX-I", "Prescriptions", {"unit": "items"}),
				el("RX-P", "Prescriptions", {"unit": "patients"}, AS),
			],
		)
		self.assertEqual(
			got,
			{("RX", "", ""): 2, ("RX-I", "", ""): 3, ("RX-P", "5+", "Male"): 1, ("RX-P", "<5", "Female"): 1},
		)
		got = workload.evaluate_dispensing(
			self.ctx(),
			[
				el("DSP", "Dispensing"),
				el("DSP-R", "Dispensing", {"unit": "records"}),
				el("DSP-RX", "Dispensing", {"unit": "prescriptions"}),
				el("NOT", "Dispensing", {"outcome": "Not Dispensed"}),
			],
		)
		self.assertEqual(
			got, {("DSP", "", ""): 1, ("DSP-R", "", ""): 2, ("DSP-RX", "", ""): 1, ("NOT", "", ""): 1}
		)


class TestReferrals(R4Case):
	def referral(self, visit, rtype, status, sent_at=None) -> str:
		return _row(
			c.REFERRAL_DOCTYPE, source_visit=visit, referral_type=rtype, status=status, sent_at=sent_at
		)

	def test_referrals_in_out_and_internal(self):
		v1 = self.visit(self.adult_f, "2001-03-08")
		v2 = self.visit(self.adult_m, "2001-03-20")
		self.referral(v1, "External", "Sent", "2001-03-08 11:00:00")
		self.referral(v2, "External", "Completed", "2001-03-20 09:00:00")
		self.referral(v2, "External", "Lost to Follow-up", "2001-03-02 09:00:00")
		self.referral(v1, "External", "Draft")
		self.referral(v1, "External", "Cancelled")
		self.referral(v1, "Internal", "Received", "2001-03-09 10:00:00")
		self.referral(v1, "External", "Sent", "2001-04-01 07:00:00")  # after the period
		self.referral(
			self.visit(self.adult_m, "2001-03-20", facility=self.f1), "External", "Sent", "2001-03-20"
		)
		self.visit(self.child_m, "2001-03-12", referred_in=1)
		self.visit(self.child_m, "2001-03-12", referred_in=1, facility=self.f1)
		self.visit(self.child_m, "2001-03-13")

		ctx = self.ctx()
		got = workload.evaluate_referrals(
			ctx,
			[
				el("OUT-X", "Referrals", {"direction": "out", "referral_type": "External"}),
				el("OUT-I", "Referrals", {"direction": "out", "referral_type": "Internal"}),
				el("OUT", "Referrals", {"direction": "out"}, registry.DISAGGREGATION_SEX),
				el("OUT-SENT", "Referrals", {"direction": "out", "statuses": ["Sent"]}),
				el("IN", "Referrals", {"direction": "in"}, AS),
				el("BAD", "Referrals", {"direction": "in", "referral_type": "External"}),
			],
		)
		self.assertEqual(
			got,
			{
				("OUT-X", "", ""): 3,  # Sent, Completed, Lost to Follow-up
				("OUT-I", "", ""): 1,
				("OUT", "", "Female"): 2,  # v1: external Sent + internal
				("OUT", "", "Male"): 2,  # v2: Completed + Lost
				("OUT-SENT", "", ""): 1,
				("IN", "<5", "Male"): 1,
			},
		)
		self.assertEqual([(e["kind"], e["element"]) for e in ctx.exceptions], [("Invalid rule", "BAD")])


class TestOutcomes(R4Case):
	def test_deaths_and_left_without_completing(self):
		died, left = c.OUTCOME_DIED, c.OUTCOME_LEFT
		self.visit(self.adult_m, "2001-03-10", outcome=died, outcome_at="2001-03-10 15:00:00")
		self.visit(
			self.adult_m, "2001-03-31", outcome=died, outcome_at="2001-04-01 01:00:00"
		)  # died in April
		self.visit(
			self.child_f, "2001-02-28", outcome=died, outcome_at="2001-03-01 02:00:00"
		)  # died in March
		self.visit(self.adult_f, "2001-03-15", outcome=left, outcome_at="2001-03-15 12:00:00")
		self.visit(self.adult_f, "2001-03-16", outcome=left, closed_at="2001-03-16 18:00:00")  # backfilled
		self.visit(
			self.adult_f, "2001-03-15", outcome=left, outcome_at="2001-03-15 12:00:00", facility=self.f1
		)
		self.visit(
			self.adult_f,
			"2001-03-15",
			outcome=c.OUTCOME_NOT_APPLICABLE,
			closure_reason="Registered in error",
			outcome_at="2001-03-15 12:00:00",
		)
		self.visit(self.adult_f, "2001-03-15", outcome=c.OUTCOME_DISCHARGED, outcome_at="2001-03-15 12:00:00")
		got = workload.evaluate_outcomes(
			self.ctx(),
			[
				el("DIED", "Visit Outcome", {"outcomes": [died]}, AS),
				el("LEFT", "Visit Outcome", {"outcomes": [left]}),
			],
		)
		self.assertEqual(got, {("DIED", "5+", "Male"): 1, ("DIED", "<5", "Female"): 1, ("LEFT", "", ""): 2})


class TestServicesAndCommunity(R4Case):
	def require(self, *doctypes):
		missing = [d for d in doctypes if not frappe.db.table_exists(d)]
		if missing:
			self.skipTest(f"R3 DocType(s) not built yet: {', '.join(missing)}")

	def test_source_missing_is_reported_not_raised(self):
		ctx = self.ctx()
		with_table = frappe.db.table_exists(c.SERVICE_DELIVERED_DOCTYPE) and frappe.db.table_exists(
			c.SERVICE_TYPE_DOCTYPE
		)
		got = workload.evaluate_services(ctx, [el("S", "Service Delivered")])
		if with_table:
			self.assertNotIn("Source not available", [e["kind"] for e in ctx.exceptions])
		else:
			self.assertEqual(got, {})
			self.assertEqual(ctx.exceptions[0]["kind"], "Source not available")

	def test_services_active_only(self):
		self.require(c.SERVICE_DELIVERED_DOCTYPE, c.SERVICE_TYPE_DOCTYPE)
		types = {}
		for key, category in (
			("counsel", "Psychosocial"),
			("social", "Social Work"),
			("physio", "Rehabilitation"),
		):
			name = f"T-R4 {key} {frappe.generate_hash(length=6)}"
			types[key] = _row(
				c.SERVICE_TYPE_DOCTYPE, name=name, service_type_name=name, category=category, enabled=1
			)
		s1 = self.visit(self.adult_f, "2001-03-05")
		s2 = self.visit(self.child_m, "2001-03-06")
		s3 = self.visit(self.adult_m, "2001-03-07", facility=self.f1)

		def service(visit, key, on, status="Active"):
			patient = frappe.db.get_value(c.VISIT_DOCTYPE, visit, "patient") if visit else self.adult_m
			facility = frappe.db.get_value(c.VISIT_DOCTYPE, visit, "facility") if visit else self.f0
			return _row(
				c.SERVICE_DELIVERED_DOCTYPE,
				patient=patient,
				visit=visit,
				facility=facility,
				service_type=types[key],
				service_date=on,
				status=status,
			)

		service(s1, "counsel", "2001-03-05")
		service(s1, "social", "2001-03-05")
		service(s2, "counsel", "2001-03-06", status="Voided")
		service(s2, "counsel", "2001-04-01")  # after the period
		service(s3, "counsel", "2001-03-07")  # another facility
		service(s2, "physio", "2001-03-06")
		orphan = service(None, "counsel", "2001-03-08")  # no visit: reported, not counted

		social = ["Social Work", "Psychosocial"]
		ctx = self.ctx()
		got = workload.evaluate_services(
			ctx,
			[
				el("SW", "Service Delivered", {"categories": social}),
				el(
					"SW-P",
					"Service Delivered",
					{"categories": social, "unit": "patients"},
					registry.DISAGGREGATION_SEX,
				),
				el("COUNSEL", "Service Delivered", {"service_types": [types["counsel"]]}),
				el("REHAB", "Service Delivered", {"categories": ["Rehabilitation"]}),
				el("ALL-V", "Service Delivered", {"unit": "visits"}),
			],
		)
		self.assertEqual(
			got,
			{
				("SW", "", ""): 2,
				("SW-P", "", "Female"): 1,
				("COUNSEL", "", ""): 1,
				("REHAB", "", ""): 1,
				("ALL-V", "", ""): 2,
			},
		)
		self.assertIn(("Service without visit", orphan, None), self.kinds(ctx))

	def test_community_sessions_and_participants(self):
		self.require(c.COMMUNITY_ACTIVITY_DOCTYPE, c.COMMUNITY_ACTIVITY_TYPE_DOCTYPE)
		talk = f"T-R4 Talk {frappe.generate_hash(length=6)}"
		home = f"T-R4 Home {frappe.generate_hash(length=6)}"
		_row(
			c.COMMUNITY_ACTIVITY_TYPE_DOCTYPE,
			name=talk,
			activity_type_name=talk,
			category="Health Talk",
			enabled=1,
		)
		_row(
			c.COMMUNITY_ACTIVITY_TYPE_DOCTYPE,
			name=home,
			activity_type_name=home,
			category="Home Visit",
			enabled=1,
		)

		def record(kind, on, male, female, total, docstatus=1, facility=None):
			return _row(
				c.COMMUNITY_ACTIVITY_DOCTYPE,
				activity_type=kind,
				activity_date=on,
				facility=facility or self.f0,
				participants_male=male,
				participants_female=female,
				participants_total=total,
				docstatus=docstatus,
			)

		record(talk, "2001-03-03", 10, 15, 30)
		record(talk, "2001-03-31", 5, 5, 10)
		record(talk, "2001-03-10", 100, 100, 200, docstatus=2)  # cancelled
		record(talk, "2001-03-11", 100, 100, 200, docstatus=0)  # draft
		record(home, "2001-03-04", 0, 1, 1)
		record(talk, "2001-03-05", 7, 7, 14, facility=self.f1)
		record(talk, "2001-04-01", 7, 7, 14)
		child_unit = frappe.db.get_value(
			"Healthcare Service Unit", {"parent_healthcare_service_unit": self.f0}, "name"
		)
		if child_unit:
			record(talk, "2001-03-15", 0, 0, 0, facility=child_unit)  # a unit under the facility counts

		got = workload.evaluate_community(
			self.ctx(),
			[
				el("TALK", "Community Activity", {"categories": ["Health Talk"]}),
				el(
					"TALK-P", "Community Activity", {"categories": ["Health Talk"], "measure": "participants"}
				),
				el(
					"TALK-PS",
					"Community Activity",
					{"activity_types": [talk], "measure": "participants"},
					registry.DISAGGREGATION_SEX,
				),
				el("HOME", "Community Activity", {"categories": ["Home Visit"]}),
			],
		)
		self.assertEqual(
			got,
			{
				("TALK", "", ""): 2 + (1 if child_unit else 0),
				("TALK-P", "", ""): 40,
				("TALK-PS", "", "Male"): 15,
				("TALK-PS", "", "Female"): 20,
				("TALK-PS", "", "Other"): 5,
				("HOME", "", ""): 1,
			},
		)


class TestRules(EMRTestCase):
	def test_validate_rule(self):
		v = workload.validate_rule
		self.assertEqual(v("Visit Attendance", {}), [])
		self.assertEqual(v("Visit Attendance", '{"attendance_type": "New"}'), [])
		self.assertTrue(v("Visit Attendance", {"colour": "red"}))
		self.assertTrue(v("Visit Attendance", "{not json"))
		self.assertTrue(v("Visit Attendance", {"departments": "Outpatient"}))
		self.assertTrue(v("Referrals", {}))  # direction required
		self.assertTrue(v("Visit Outcome", {"outcomes": ["Cured"]}))
		self.assertTrue(v("Community Activity", {}, registry.DISAGGREGATION_AGE))
		self.assertTrue(v("Community Activity", {}, registry.DISAGGREGATION_SEX))
		self.assertEqual(
			v("Community Activity", {"measure": "participants"}, registry.DISAGGREGATION_SEX), []
		)
		self.assertTrue(v("Diagnosis Category", {}))

	def test_registry_points_at_these_evaluators(self):
		for source, path in registry.SOURCE_TYPES.items():
			if source == "Diagnosis Category":
				continue
			self.assertIn(source, workload.RULE_SCHEMAS)
			self.assertIs(registry.get_evaluator(source), getattr(workload, path.rsplit(".", 1)[1]))


class TestDefinitions(EMRTestCase):
	"""Every R4 definition file is a valid seed (FR-29; SDD §5.8 data element inventory)."""

	NAMES = frozenset({c.MOH_717, c.MOH_711_REHAB, c.MOH_711_RH, c.MOH_747A, c.MOH_643B, c.MH_CLINIC})
	BLOCKED = frozenset({c.MOH_711_RH, c.MOH_747A, c.MOH_643B, c.MH_CLINIC})

	def load(self) -> dict[str, dict]:
		return {f: json.loads((DATA / f).read_text(encoding="utf-8")) for f in R4_FILES}

	def test_definitions_are_valid(self):
		defs = self.load()
		self.assertEqual({d["name"] for d in defs.values()}, self.NAMES)
		lab = json.loads((Path(c.__file__).parent / "setup" / "data" / "lab_catalogue.json").read_text())
		templates = {t["name"] for t in lab["single_tests"] + lab["panels"]}
		codes = []
		for fname, d in defs.items():
			with self.subTest(fname):
				self.assertIn(d["status"], c.MOH_REPORT_STATUSES)
				self.assertEqual(d["note"], "PROVISIONAL — HIS Officer to confirm (OI-5)")
				self.assertTrue(d["version"] and d["period_type"] == "Monthly")
				self.assertIsInstance(d["dimensions"], list)
				self.check_bands(d["age_bands"])
				if d["name"] in self.BLOCKED:
					self.assertEqual(d["status"], "Blocked")
					self.assertEqual(d["elements"], [])
					self.assertRegex(d["blocked_reason"], r"OI-(5|24)")
				else:
					self.assertEqual(d["status"], "Active - Provisional")
					self.assertIsNone(d["blocked_reason"])
					self.assertTrue(d["elements"])
				for e in d["elements"]:
					codes.append(e["code"])
					self.check_element(e, templates)
		self.assertEqual(len(codes), len(set(codes)), "element codes must be unique")

	def check_bands(self, bands):
		previous = 0
		for band in bands:
			self.assertEqual(band["min_months"], previous, "bands must be contiguous and ordered")
			previous = band["max_months"]
		if bands:
			self.assertIsNone(previous, "the last band is open-ended")

	def check_element(self, e, templates):
		self.assertTrue(e["code"] and e["label"] and e["section"] and isinstance(e["sort_order"], int))
		self.assertIn(e["disaggregation"], registry.DISAGGREGATIONS)
		self.assertIn("dhis2_uid", e)
		if e["availability"] == registry.AVAILABILITY_GAP:
			self.assertTrue(e["gap_note"].startswith("Gap — Wave 2 / OI-5"), e["code"])
			return
		self.assertEqual(e["availability"], registry.AVAILABILITY_AVAILABLE)
		self.assertIsNone(e["gap_note"])
		self.assertIn(e["source_type"], registry.SOURCE_TYPES)
		self.assertNotEqual(e["source_type"], "Diagnosis Category")
		self.assertEqual(
			workload.validate_rule(e["source_type"], e["rule"], e["disaggregation"]), [], e["code"]
		)
		for dept in e["rule"].get("departments", []):
			self.assertIn(dept, c.DEPARTMENTS, e["code"])
		for template in e["rule"].get("templates", []):
			self.assertIn(template, templates, e["code"])

	def test_mental_health_population_groups(self):
		from onerc_healthcare.registration.setup import POPULATION_GROUPS

		mh = self.load()["mh_clinic.json"]
		self.assertEqual([b["label"] for b in mh["age_bands"]], ["<18", "18-59", "60+"])
		group = next(d for d in mh["dimensions"] if d["dimension"] == "Population group")
		self.assertEqual(group["field"], "krcs_population_group")
		self.assertEqual([v["value"] for v in group["values"]], POPULATION_GROUPS.strip().split("\n"))

	def test_every_available_element_evaluates(self):
		"""Each Available element's SQL runs (empty period) and parses without an "Invalid rule"."""
		ctx = registry.SourceContext(
			report="T-R4", from_date=FROM, to_date=TO, facility=get_facility(0), age_bands=BANDS
		)
		by_source: dict[str, list] = {}
		for d in self.load().values():
			for e in d["elements"]:
				if e["availability"] == registry.AVAILABILITY_AVAILABLE:
					spec = registry.ElementSpec(
						e["code"], e["label"], e["source_type"], e["rule"], e["disaggregation"]
					)
					by_source.setdefault(e["source_type"], []).append(spec)
		for source, specs in by_source.items():
			registry.get_evaluator(source)(ctx, specs)
		self.assertNotIn("Invalid rule", [x["kind"] for x in ctx.exceptions])

	def test_rules_match_r3_masters(self):
		"""Service and community rules name MOH lines and categories that R3's masters actually carry."""
		try:
			from onerc_healthcare.community.setup import ACTIVITY_TYPES, SERVICE_TYPES
		except ImportError:
			self.skipTest("R3 community seeds not built yet")
		pkg = Path(c.__file__).parent / "emr_community" / "doctype"

		def options(doctype: str) -> set[str]:
			meta = json.loads((pkg / doctype / f"{doctype}.json").read_text(encoding="utf-8"))
			field = next(f for f in meta["fields"] if f["fieldname"] == "category")
			return set(field["options"].split("\n"))

		allowed = {
			"Service Delivered": {
				"moh_lines": {row[2] for row in SERVICE_TYPES},
				"categories": options("visit_service_type"),
			},
			"Community Activity": {
				"moh_lines": {row[2] for row in ACTIVITY_TYPES},
				"categories": options("community_activity_type"),
			},
		}
		for d in self.load().values():
			for e in d["elements"]:
				for key, values in allowed.get(e["source_type"], {}).items():
					for value in (e["rule"] or {}).get(key, []):
						self.assertIn(value, values, f"{e['code']} {key}")
