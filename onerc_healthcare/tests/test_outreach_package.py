# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 4 O1: the outreach package (OUT-06, FR-23, D58; protocol §3).

Catchment subtree inclusion and exclusion, merged / disabled / archived patients left out, Mental Health and
sensitive services never sent, one access-log row per patient sent, stable paging and the patient cap, the
reference data (no chapter X, no Mental Health department, the point-of-care flag) and access by role."""

from __future__ import annotations

from contextlib import contextmanager

import frappe
from frappe.utils import add_days, getdate, random_string, today

from onerc_healthcare.constants import (
	ACCESS_ACTION_OUTREACH_PACKAGE,
	ACCESS_LOG_DOCTYPE,
	ENCOUNTER_DOCTYPE,
	OUTREACH_DEPARTMENT_DOCTYPE,
	OUTREACH_SESSION_DOCTYPE,
	OUTREACH_SETTINGS_DOCTYPE,
	SESSION_ACTIVE,
	SESSION_PLANNED,
	VISIT_DOCTYPE,
)
from onerc_healthcare.outreach import auth, package
from onerc_healthcare.outreach import session as sessions
from onerc_healthcare.privacy.access_log import is_sensitive
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import (
	LEAF_CODE,
	LEAF_CODE_2,
	add_diagnosis,
	chapter_x_code,
	get_test_item,
	routed_visit,
	start,
)
from onerc_healthcare.tests.factories import make_patient
from onerc_healthcare.tests.outreach_factories import (
	make_catchment,
	make_residence,
	make_session,
	outreach_users,
)
from onerc_healthcare.tests.visit_factories import make_visit, make_visit_users
from onerc_healthcare.visit.engine import transition


@contextmanager
def as_user(user: str):
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user("Administrator")


def set_setting(key: str, value) -> None:
	frappe.db.set_single_value(OUTREACH_SETTINGS_DOCTYPE, key, value)


def close(visit: str, reason: str = "Left before completion") -> None:
	if frappe.db.get_value(VISIT_DOCTYPE, visit, "status") not in ("Discharged", "Referred"):
		transition(visit, "close_visit", system=True, closure_reason=reason, comment="T- test")


def add_drug(encounter: str, period: str = "5 Day"):
	doc = frappe.get_doc(ENCOUNTER_DOCTYPE, encounter)
	doc.append(
		"drug_prescription",
		{"drug_code": get_test_item(), "dosage": "TID", "period": period, "dosage_form": "Tablet"},
	)
	doc.save(ignore_permissions=True)


class PackageCase(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.users = outreach_users()
		self.visit_users = make_visit_users()
		self.area, self.leaf = make_catchment()
		self.top = frappe.db.get_value("Catchment Area Location", {"parent": self.area}, "residence_location")
		self.session = make_session(members=[self.users["member"]], catchment_area=self.area)

	def patients(self, page: int = 0, page_size: int = 500, user: str | None = None, session=None):
		with as_user(user or self.users["member"]):
			return package.get_patients(session or self.session.name, page=page, page_size=page_size)

	def consulted(
		self, patient: str, department: str = "Outpatient", code: str = LEAF_CODE, drug: str | None = None
	):
		"""A real consultation (route, start, diagnose, prescribe, complete), then the visit is closed."""
		from onerc_healthcare.clinical.consultation import complete_consultation

		user = self.visit_users["mho" if department == "Mental Health" else "clinician"]
		visit = routed_visit(department, patient=patient)
		enc = start(visit.name, user)
		add_diagnosis(enc, code)
		if drug:
			add_drug(enc, drug)
		with as_user(user):
			complete_consultation(enc)
		close(visit.name)
		return visit.name, enc


class TestCatchmentSelection(PackageCase):
	def test_subtree_inclusion_and_exclusion(self):
		inside_top = make_patient(krcs_residence=self.top).name
		inside_leaf = make_patient(krcs_residence=self.leaf).name
		deeper = (
			make_residence(parent=self.leaf)
			if frappe.db.get_value("Residence Location", self.leaf, "is_group")
			else None
		)
		inside_deep = make_patient(krcs_residence=deeper).name if deeper else None
		outside = make_patient(krcs_residence=make_residence()).name
		no_residence = make_patient().name
		merged = make_patient(krcs_residence=self.leaf).name
		frappe.db.set_value("Patient", merged, "krcs_merged_into", inside_leaf)
		disabled = make_patient(krcs_residence=self.leaf).name
		frappe.db.set_value("Patient", disabled, "status", "Disabled")
		archived = make_patient(krcs_residence=self.leaf).name
		frappe.db.set_value("Patient", archived, "krcs_archived", 1)

		res = self.patients()
		names = {p["name"] for p in res["patients"]}
		expected = {inside_top, inside_leaf} | ({inside_deep} if inside_deep else set())
		self.assertEqual(names, expected)
		self.assertEqual(res["total"], len(expected))
		self.assertFalse(res["has_more"])
		for name in (outside, no_residence, merged, disabled, archived):
			self.assertNotIn(name, names)

	def test_patient_shape(self):
		p = make_patient(
			krcs_residence=self.leaf, first_name="T-Amina", mobile="0712000000", allergies="T- penicillin"
		)
		row = self.patients()["patients"][0]
		self.assertEqual(
			set(row),
			{
				"name",
				"modified",
				"first_name",
				"middle_name",
				"last_name",
				"sex",
				"dob",
				"dob_estimated",
				"mobile",
				"residence",
				"allergies",
				"identifiers",
				"recent_visits",
				"active_prescriptions",
			},
		)
		self.assertEqual(
			(row["name"], row["first_name"], row["residence"], row["allergies"], row["dob"]),
			(p.name, "T-Amina", self.leaf, "T- penicillin", str(p.dob)),
		)
		self.assertEqual(row["modified"], str(frappe.db.get_value("Patient", p.name, "modified")))
		self.assertEqual(
			row["identifiers"],
			[
				{"identifier_type": r.identifier_type, "identifier_value": r.identifier_value}
				for r in p.krcs_identifiers
			],
		)
		self.assertEqual((row["recent_visits"], row["active_prescriptions"]), ([], []))

	def test_paging_is_stable_and_capped(self):
		names = sorted(make_patient(krcs_residence=self.leaf).name for _i in range(5))
		pages = [self.patients(page=i, page_size=2) for i in range(3)]
		self.assertEqual(
			[[p["name"] for p in pg["patients"]] for pg in pages], [names[0:2], names[2:4], names[4:5]]
		)
		self.assertEqual([pg["has_more"] for pg in pages], [True, True, False])
		self.assertEqual({pg["total"] for pg in pages}, {5})
		self.assertEqual(self.patients(page=3, page_size=2)["patients"], [])
		set_setting("package_max_patients", 3)
		capped = [self.patients(page=i, page_size=2) for i in range(2)]
		self.assertEqual([p["name"] for pg in capped for p in pg["patients"]], names[:3])
		self.assertEqual((capped[0]["total"], capped[0]["has_more"], capped[1]["has_more"]), (3, True, False))

	def test_access_log_one_row_per_patient_sent(self):
		names = sorted(make_patient(krcs_residence=self.leaf).name for _i in range(3))
		self.patients(page=0, page_size=2)
		rows = frappe.get_all(
			ACCESS_LOG_DOCTYPE,
			filters={"action": ACCESS_ACTION_OUTREACH_PACKAGE, "patient": ["in", names]},
			fields=["patient", "user", "sensitive", "reference_doctype", "reference_name", "details"],
		)
		self.assertEqual(sorted(r.patient for r in rows), names[:2])
		for r in rows:
			self.assertEqual(
				(r.user, r.sensitive, r.reference_doctype, r.reference_name),
				(self.users["member"], 0, "Patient", r.patient),
			)
			self.assertIn(self.session.name, r.details)
		self.patients(page=1, page_size=2)
		self.assertEqual(
			frappe.db.count(
				ACCESS_LOG_DOCTYPE, {"action": ACCESS_ACTION_OUTREACH_PACKAGE, "patient": ["in", names]}
			),
			3,
		)

	def test_first_download_activates_session(self):
		self.assertEqual(self.session.status, SESSION_PLANNED)
		self.patients()
		doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, self.session.name)
		self.assertEqual(doc.status, SESSION_ACTIVE)
		self.assertEqual([r.action for r in doc.status_log], ["create", "activate"])
		self.patients()
		self.assertEqual(len(frappe.get_doc(OUTREACH_SESSION_DOCTYPE, self.session.name).status_log), 2)

	def test_access_by_role(self):
		make_patient(krcs_residence=self.leaf)
		for key in ("outsider", "his", "mic", "reception", "clinician"):
			with self.assertRaises(frappe.PermissionError, msg=key):
				self.patients(user=self.users[key])
			with as_user(self.users[key]), self.assertRaises(frappe.PermissionError, msg=key):
				package.get_reference(self.session.name)
		with as_user("Guest"), self.assertRaises(frappe.AuthenticationError):
			package.get_patients(self.session.name)
		self.assertEqual(self.patients()["total"], 1)

	def test_closed_session_refused(self):
		sessions.transition(self.session.name, sessions.ACTION_CANCEL, reason="T-", system=True)
		with self.assertRaisesRegex(frappe.ValidationError, "no longer accepts"):
			self.patients()

	def test_device_grant_must_match_session(self):
		from onerc_healthcare.outreach import devices

		other = make_session(members=[self.users["member"]], catchment_area=self.area)
		with as_user(self.users["member"]):
			import base64
			import os

			res = devices.enrol(
				other.name, f"t-dev-{random_string(10).lower()}", base64.b64encode(os.urandom(32)).decode()
			)
		setattr(frappe.local, auth.GRANT_LOCAL_KEY, res["grant"])
		try:
			with self.assertRaisesRegex(frappe.PermissionError, "another outreach session"):
				self.patients()
			self.assertEqual(self.patients(session=other.name)["total"], 0)
		finally:
			setattr(frappe.local, auth.GRANT_LOCAL_KEY, None)


class TestPatientSummary(PackageCase):
	"""Recent visits and active prescriptions; Mental Health never (OI-7, D58)."""

	def summary(self, patient: str) -> dict:
		return next(p for p in self.patients()["patients"] if p["name"] == patient)

	def test_recent_visits_diagnoses_and_mental_health_excluded(self):
		patient = make_patient(krcs_residence=self.leaf).name
		_mh_visit, mh_enc = self.consulted(patient, "Mental Health", code=LEAF_CODE_2, drug="5 Day")
		opd_visit, opd_enc = self.consulted(patient, "Outpatient", code=LEAF_CODE, drug="5 Day")
		self.assertTrue(is_sensitive(ENCOUNTER_DOCTYPE, mh_enc))
		self.assertFalse(is_sensitive(ENCOUNTER_DOCTYPE, opd_enc))
		row = self.summary(patient)
		self.assertEqual(len(row["recent_visits"]), 1)
		visit = row["recent_visits"][0]
		self.assertEqual(
			(visit["department"], visit["date"], visit["facility"]),
			("Outpatient", str(getdate(today())), frappe.db.get_value(VISIT_DOCTYPE, opd_visit, "facility")),
		)
		self.assertEqual(
			visit["diagnoses"],
			[
				{
					"code": "1A00",
					"title": frappe.db.get_value("Code Value", LEAF_CODE, "display"),
					"case_type": "New",
				}
			],
		)
		self.assertEqual(len(row["active_prescriptions"]), 1)
		rx = row["active_prescriptions"][0]
		self.assertEqual((rx["dosage"], rx["period"], rx["date"]), ("TID", "5 Day", str(getdate(today()))))
		self.assertEqual(
			rx["medication"],
			frappe.db.get_value("Drug Prescription", {"parent": opd_enc}, "drug_name") or get_test_item(),
		)
		# nothing of the Mental Health consultation anywhere in the payload
		import json

		blob = json.dumps(row)
		self.assertNotIn("1A01", blob)
		self.assertNotIn("Mental Health", blob)

	def test_encounter_department_alone_excludes_visit(self):
		"""A visit shown as Outpatient whose encounter is Mental Health is still never sent."""
		patient = make_patient(krcs_residence=self.leaf).name
		visit, _enc = self.consulted(patient, "Mental Health", drug="5 Day")
		frappe.db.set_value(VISIT_DOCTYPE, visit, "current_department", "Outpatient")
		row = self.summary(patient)
		self.assertEqual((row["recent_visits"], row["active_prescriptions"]), ([], []))

	def test_sensitive_service_excludes_visit(self):
		patient = make_patient(krcs_residence=self.leaf).name
		visit, _enc = self.consulted(patient, "Outpatient")
		self.assertEqual(len(self.summary(patient)["recent_visits"]), 1)
		service_type = frappe.get_doc(
			{
				"doctype": "Visit Service Type",
				"service_type_name": f"T-Psy {random_string(5)}",
				"category": "Psychosocial",
				"enabled": 1,
				"sensitive": 1,
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Service Delivered",
				"visit": visit,
				"service_type": service_type.name,
				"service_date": today(),
			}
		).insert(ignore_permissions=True)
		self.assertEqual(self.summary(patient)["recent_visits"], [])

	def test_last_n_visits_only(self):
		patient = make_patient(krcs_residence=self.leaf).name
		visits = []
		for days_ago in (40, 30, 20, 10):
			day = add_days(today(), -days_ago)
			v = make_visit(patient=patient, system=True, visit_date=day, arrived_at=f"{day} 08:00:00")
			close(v.name, "Left before being seen")
			visits.append(day)
		erroneous = make_visit(
			patient=patient,
			system=True,
			visit_date=add_days(today(), -5),
			arrived_at=f"{add_days(today(), -5)} 08:00:00",
		)
		close(erroneous.name, "Registered in error")
		self.assertEqual(
			[v["date"] for v in self.summary(patient)["recent_visits"]],
			[str(getdate(d)) for d in reversed(visits)][:3],
		)
		set_setting("summary_visits", 1)
		self.assertEqual(
			[v["date"] for v in self.summary(patient)["recent_visits"]], [str(getdate(visits[-1]))]
		)
		set_setting("summary_visits", 0)
		self.assertEqual(self.summary(patient)["recent_visits"], [])

	def test_finished_prescription_not_active(self):
		patient = make_patient(krcs_residence=self.leaf).name
		_visit, enc = self.consulted(patient, "Outpatient", drug="5 Day")
		frappe.db.set_value(
			ENCOUNTER_DOCTYPE, enc, "encounter_date", add_days(today(), -5)
		)  # days 1-5 are over
		self.assertEqual(self.summary(patient)["active_prescriptions"], [])
		frappe.db.set_value(ENCOUNTER_DOCTYPE, enc, "encounter_date", add_days(today(), -4))  # today is day 5
		self.assertEqual(len(self.summary(patient)["active_prescriptions"]), 1)
		mr = frappe.db.get_value("Drug Prescription", {"parent": enc}, "medication_request")
		frappe.db.set_value("Medication Request", mr, "status", "stopped-Medication Request Status")
		self.assertEqual(self.summary(patient)["active_prescriptions"], [])

	def test_prescription_end(self):
		durations = {
			r.name: r for r in frappe.get_all("Prescription Duration", fields=["name", "number", "period"])
		}
		start_day = getdate("2026-01-30")
		cases = {
			"5 Day": "2026-02-04",
			"1 Week": "2026-02-06",
			"1 Month": "2026-02-28",
			"12 Hour": "2026-01-31",
			None: "2026-03-01",
		}
		for period, end in cases.items():
			self.assertEqual(package._prescription_end(start_day, period, durations), getdate(end), period)


class TestReference(PackageCase):
	def reference(self, version=None, session=None):
		with as_user(self.users["member"]):
			return package.get_reference(session or self.session.name, version=version)

	def test_contents(self):
		res = self.reference()
		self.assertFalse(res["unchanged"])
		data = res["data"]
		self.assertEqual(
			set(data),
			{
				"icd11",
				"medications",
				"dosages",
				"periods",
				"dosage_forms",
				"lab_templates",
				"departments",
				"identifier_types",
				"residences",
				"triage_levels",
				"community_activity_types",
				"outcomes",
				"genders",
			},
		)
		icd = {row[0]: row for row in data["icd11"]}
		self.assertNotIn(chapter_x_code(), icd)
		self.assertEqual(
			icd[LEAF_CODE][1:], ["1A00", frappe.db.get_value("Code Value", LEAF_CODE, "display"), 1]
		)
		self.assertEqual(data["departments"], ["Outpatient"])
		labs = {t["name"]: t for t in data["lab_templates"]}
		self.assertEqual(labs["Malaria Rapid Diagnostic Test"]["point_of_care"], 1)
		self.assertEqual(labs["Malaria Rapid Diagnostic Test"]["abbr"], "MRDT")
		self.assertIn("Positive - P. falciparum", labs["Malaria Rapid Diagnostic Test"]["options"])
		self.assertEqual(labs["Fasting Blood Sugar"]["point_of_care"], 0)
		self.assertEqual(labs["Fasting Blood Sugar"]["unit"], "mmol/L")
		self.assertIn("Urinalysis", labs)
		self.assertIn("Haemoglobin", labs)
		for not_orderable in ("BP Systolic", "Temperature", "Lymphocytes", "Urine pH"):
			self.assertNotIn(not_orderable, labs)
		self.assertEqual(data["outcomes"], ["Discharged home", "Died"])
		self.assertEqual(data["triage_levels"][0], "Emergency")
		self.assertIn("5 Day", data["periods"])
		self.assertIn(self.leaf, {r["name"] for r in data["residences"]})
		ids = {t["name"]: t for t in data["identifier_types"]}
		self.assertEqual(
			(ids["UNHCR ID"]["is_primary"], ids["Facility-generated"]["facility_generated"]), (1, 1)
		)
		meds = data["medications"]
		self.assertTrue(
			meds
			and {"name", "generic_name", "strength", "dosage_form", "default_dosage", "default_period"}
			== set(meds[0])
		)

	def test_unchanged_short_circuit_and_new_version(self):
		first = self.reference()
		self.assertEqual(
			self.reference(version=first["version"]), {"version": first["version"], "unchanged": True}
		)
		self.assertFalse(self.reference(version="stale")["unchanged"])
		doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, self.session.name)
		doc.append("departments", {"department": "Maternal and Child Health"})
		doc.save(ignore_permissions=True)
		second = self.reference(version=first["version"])
		self.assertFalse(second["unchanged"])
		self.assertNotEqual(second["version"], first["version"])
		self.assertEqual(second["data"]["departments"], ["Outpatient", "Maternal and Child Health"])

	def test_mental_health_never_offered_even_if_stored(self):
		"""A Mental Health row written past validation (e.g. a direct DB edit) is still filtered."""
		frappe.get_doc(
			{
				"doctype": OUTREACH_DEPARTMENT_DOCTYPE,
				"parent": self.session.name,
				"parenttype": OUTREACH_SESSION_DOCTYPE,
				"parentfield": "departments",
				"department": "Mental Health",
				"idx": 9,
			}
		).db_insert()
		self.assertEqual(self.reference()["data"]["departments"], ["Outpatient"])
