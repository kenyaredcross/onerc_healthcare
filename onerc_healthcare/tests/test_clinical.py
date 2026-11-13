# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""B1 consultation tests: FR-10, FR-11, FR-12, BR-3, BR-4, SDD §5.3."""

from __future__ import annotations

from unittest.mock import patch

import frappe

from onerc_healthcare.clinical import consultation, diagnosis, events, guards, history, practitioner
from onerc_healthcare.clinical import setup as clinical_setup
from onerc_healthcare.clinical.consultation import ConsultationError
from onerc_healthcare.clinical.diagnosis import DiagnosisError
from onerc_healthcare.clinical.events import EncounterGuardError
from onerc_healthcare.constants import (
	DEFAULT_APPOINTMENT_TYPE,
	DEPT_MENTAL_HEALTH,
	DEPT_OUTPATIENT,
	ENCOUNTER_DOCTYPE,
	VISIT_CLOSURE_REASONS,
	VISIT_DOCTYPE,
	VISIT_STATUS_AWAITING_LAB,
	VISIT_STATUS_AWAITING_PHARMACY,
	VISIT_STATUS_DISCHARGED,
	VISIT_STATUS_IN_CONSULTATION,
)
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import (
	LAB_TEST,
	LAB_TEST_2,
	LEAF_CODE,
	LEAF_CODE_2,
	PARENT_CODE,
	add_diagnosis,
	add_drug_row,
	add_lab_row,
	as_user,
	chapter_x_code,
	routed_visit,
	start,
	users,
	visit_status,
)
from onerc_healthcare.visit.engine import transition

PE = "healthcare.healthcare.doctype.patient_encounter.patient_encounter"


class TestClinicalSetup(EMRTestCase):
	def test_install_idempotent(self):
		"""FR-10: consultation fields and property setters exist; a second install changes nothing."""
		before = frappe.db.count("Property Setter", {"doc_type": ENCOUNTER_DOCTYPE})
		clinical_setup.install()
		clinical_setup.install()
		self.assertEqual(frappe.db.count("Property Setter", {"doc_type": ENCOUNTER_DOCTYPE}), before)
		meta = frappe.get_meta(ENCOUNTER_DOCTYPE)
		for field in (
			"krcs_visit",
			"krcs_department",
			"krcs_presenting_complaint",
			"krcs_history",
			"krcs_examination_findings",
			"krcs_management_plan",
			"krcs_diagnoses",
			"krcs_no_diagnosis_reason",
			"krcs_addendum",
			"krcs_lab_results_html",
			"krcs_history_html",
		):
			self.assertTrue(meta.has_field(field), field)
		self.assertTrue(meta.get_field("krcs_addendum").allow_on_submit)
		self.assertTrue(meta.get_field("krcs_visit").read_only)
		self.assertEqual(meta.get_field("appointment_type").default, DEFAULT_APPOINTMENT_TYPE)
		for field in ("appointment_type", "submit_orders_on_save", "diagnosis", "procedure_prescription"):
			self.assertTrue(meta.get_field(field).hidden, field)
		self.assertTrue(meta.get_field("practitioner").read_only)
		self.assertTrue(frappe.db.exists("Appointment Type", DEFAULT_APPOINTMENT_TYPE))

	def test_install_keeps_admin_change(self):
		"""Property setters are created only when missing: an admin's value survives migrate."""
		ps = frappe.db.get_value(
			"Property Setter",
			{"doc_type": ENCOUNTER_DOCTYPE, "field_name": "practitioner", "property": "read_only"},
			"name",
		)
		frappe.db.set_value("Property Setter", ps, "value", "0")
		clinical_setup.install()
		self.assertEqual(frappe.db.get_value("Property Setter", ps, "value"), "0")


class ClinicalTestCase(EMRTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users = users()

	def u(self, key: str) -> str:
		return self.users[key]

	def consulting(self, department: str = DEPT_OUTPATIENT, user: str = "clinician"):
		"""(visit name, encounter name) of a consultation started through the endpoint."""
		visit = routed_visit(department)
		return visit.name, start(visit.name, self.u(user))

	def complete(self, encounter: str, user: str = "clinician", **kw):
		with as_user(self.u(user)):
			return consultation.complete_consultation(encounter, **kw)

	def send_labs(self, encounter: str, user: str = "clinician"):
		with as_user(self.u(user)):
			return consultation.send_lab_orders(encounter)


class TestStartConsultation(ClinicalTestCase):
	def test_start_creates_one_encounter(self):
		"""FR-10, BR-3: Routed → In Consultation, encounter pre-filled from triage; repeat returns it."""
		visit = routed_visit(DEPT_OUTPATIENT)
		with as_user(self.u("clinician")):
			out = consultation.start_consultation(visit.name)
			again = consultation.start_consultation(visit.name)
		self.assertTrue(out["created"])
		self.assertFalse(again["created"])
		self.assertEqual(out["encounter"], again["encounter"])
		enc = frappe.get_doc(ENCOUNTER_DOCTYPE, out["encounter"])
		self.assertEqual(enc.krcs_visit, visit.name)
		self.assertEqual(enc.krcs_department, DEPT_OUTPATIENT)
		self.assertEqual(enc.krcs_presenting_complaint, "T- cough for three days")
		self.assertEqual(enc.appointment_type, DEFAULT_APPOINTMENT_TYPE)
		self.assertEqual(enc.submit_orders_on_save, 0)
		self.assertEqual(
			frappe.db.get_value("Healthcare Practitioner", enc.practitioner, "user_id"), self.u("clinician")
		)
		v = frappe.get_doc(VISIT_DOCTYPE, visit.name)
		self.assertEqual((v.status, v.encounter), (VISIT_STATUS_IN_CONSULTATION, enc.name))

	def test_mho_consults_any_department(self):
		"""Decision 2026-11-13: CO and MHO may consult in any department."""
		_visit, enc = self.consulting(DEPT_OUTPATIENT, user="mho")
		self.assertTrue(enc)
		_visit, enc = self.consulting(DEPT_MENTAL_HEALTH, user="clinician")
		self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "krcs_department"), DEPT_MENTAL_HEALTH)

	def test_start_roles(self):
		visit = routed_visit()
		for key in ("reception", "triage", "lab", "pharmacist", "his", "mic", "hoo", "sysadmin"):
			with as_user(self.u(key)), self.assertRaises(frappe.PermissionError, msg=key):
				consultation.start_consultation(visit.name)

	def test_start_needs_routed_visit(self):
		from onerc_healthcare.tests.visit_factories import make_visit

		visit = make_visit()
		with as_user(self.u("clinician")), self.assertRaises(frappe.ValidationError):
			consultation.start_consultation(visit.name)


class TestEncounterGuards(ClinicalTestCase):
	def _new(self, visit: str, **kw):
		values = {
			"doctype": ENCOUNTER_DOCTYPE,
			"patient": frappe.db.get_value(VISIT_DOCTYPE, visit, "patient"),
			"practitioner": practitioner.get_or_create(self.u("clinician")),
			"krcs_visit": visit,
		}
		values.update(kw)
		return frappe.get_doc(values)

	def test_one_encounter_per_visit(self):
		"""SDD §5.3: one active consultation per visit."""
		visit, _enc = self.consulting()
		with self.assertRaises(EncounterGuardError):
			self._new(visit).insert(ignore_permissions=True)

	def test_visit_required_and_patient_must_match(self):
		visit, _enc = self.consulting()
		doc = self._new(visit)
		doc.krcs_visit = None
		with self.assertRaises(EncounterGuardError):
			doc.insert(ignore_permissions=True)
		other = routed_visit()
		transition(other.name, "start_consultation", system=True)
		doc = self._new(other.name, patient=frappe.db.get_value(VISIT_DOCTYPE, visit, "patient"))
		with self.assertRaises(EncounterGuardError):
			doc.insert(ignore_permissions=True)

	def test_new_encounter_needs_visit_in_consultation(self):
		visit = routed_visit()
		with self.assertRaises(EncounterGuardError):
			self._new(visit.name).insert(ignore_permissions=True)

	def test_no_desk_submit(self):
		"""Submit only through Complete Consultation (flags.krcs_via_consultation)."""
		_visit, enc = self.consulting()
		add_diagnosis(enc)
		doc = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
		with as_user(self.u("clinician")), self.assertRaises(EncounterGuardError):
			doc.submit()
		self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "docstatus"), 0)

	def test_no_cancel(self):
		"""Phase 2 decision: no encounter cancel (corrections are addenda)."""
		_visit, enc = self.consulting()
		add_diagnosis(enc)
		self.complete(enc)
		doc = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
		with self.assertRaises(EncounterGuardError):
			doc.cancel()
		with as_user(self.u("clinician")), self.assertRaises((EncounterGuardError, frappe.PermissionError)):
			frappe.get_doc(ENCOUNTER_DOCTYPE, enc).cancel()
		self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "docstatus"), 1)

	def test_no_draft_save_after_close(self):
		visit, enc = self.consulting()
		transition(visit, "close_visit", system=True, closure_reason=VISIT_CLOSURE_REASONS[0], comment="T-")
		doc = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
		doc.krcs_history = "late note"
		with self.assertRaises(EncounterGuardError):
			doc.save(ignore_permissions=True)

	def test_visit_cannot_change(self):
		_visit, enc = self.consulting()
		other = routed_visit()
		doc = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
		doc.krcs_visit = other.name
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	def test_orders_on_save_forced_off(self):
		_visit, enc = self.consulting()
		doc = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
		doc.submit_orders_on_save = 1
		doc.append("lab_test_prescription", {"observation_template": LAB_TEST})
		doc.save(ignore_permissions=True)
		self.assertEqual(doc.submit_orders_on_save, 0)
		self.assertFalse(doc.lab_test_prescription[0].service_request)

	def test_unused_tables_refused(self):
		_visit, enc = self.consulting()
		doc = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
		doc.append("therapies", {"therapy_type": "T-none", "no_of_sessions": 1})
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	def test_addendum(self):
		_visit, enc = self.consulting()
		add_diagnosis(enc)
		with as_user(self.u("clinician")), self.assertRaises(ConsultationError):
			consultation.add_addendum(enc, "too early")
		self.complete(enc)
		with as_user(self.u("clinician")):
			consultation.add_addendum(enc, "First correction")
			out = consultation.add_addendum(enc, "Second correction")
		self.assertIn("First correction", out["krcs_addendum"])
		self.assertTrue(out["krcs_addendum"].rstrip().endswith("Second correction"))
		with as_user(self.u("his")), self.assertRaises(frappe.PermissionError):
			consultation.add_addendum(enc, "not allowed")
		# The append-only check (before_update_after_submit hook, wired by the integrator).
		doc = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
		doc._doc_before_save = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
		doc.krcs_addendum = "rewritten"
		with self.assertRaises(EncounterGuardError):
			events.encounter_before_update_after_submit(doc)
		doc.krcs_addendum = doc._doc_before_save.krcs_addendum
		events.encounter_before_update_after_submit(doc)


class TestConsultationFlow(ClinicalTestCase):
	def test_full_flow_with_lab(self):
		"""FR-12/FR-15: start → send lab orders → complete with diagnosis while results pend → visit."""
		visit, enc = self.consulting()
		add_lab_row(enc)
		out = self.send_labs(enc)
		self.assertEqual(len(out["service_requests"]), 1)
		sr = frappe.get_doc("Service Request", out["service_requests"][0])
		self.assertEqual((sr.docstatus, sr.order_group), (1, enc))
		self.assertEqual(visit_status(visit), VISIT_STATUS_AWAITING_LAB)
		with self.assertRaises(ConsultationError):  # nothing new to send
			self.send_labs(enc)

		add_diagnosis(enc)
		with self.assertRaises(ConsultationError):  # results pending: reason required
			self.complete(enc)
		self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "docstatus"), 0)
		res = self.complete(enc, reason="Malaria RDT pending; treated empirically")
		self.assertEqual(res["visit_status"], VISIT_STATUS_AWAITING_LAB)
		doc = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual([r.code for r in doc.codification_table], ["1A00"])
		log = frappe.get_doc(VISIT_DOCTYPE, visit).status_log[-1]
		self.assertEqual(
			(log.action, log.comment), ("complete_consultation", "Malaria RDT pending; treated empirically")
		)

	def test_second_lab_order_ensure_called(self):
		"""Marley's make_observation defect: every new Service Request goes to B2's ensure_observations."""
		_visit, enc = self.consulting()
		add_lab_row(enc, LAB_TEST)
		add_lab_row(enc, LAB_TEST_2)
		calls = []
		with patch.object(consultation, "_ensure_observations_fn", return_value=calls.append):
			out = self.send_labs(enc)
		self.assertEqual(sorted(calls), sorted(out["service_requests"]))
		self.assertEqual(len(calls), 2)
		self.assertTrue(out["observations_ensured"])

	def test_missing_ensure_tolerated(self):
		with patch.object(consultation, "ENSURE_OBSERVATIONS", "onerc_healthcare.lab.no_such_module.fn"):
			self.assertIsNone(consultation._ensure_observations_fn())
		with patch.object(
			consultation, "ENSURE_OBSERVATIONS", "onerc_healthcare.clinical.consultation.no_fn"
		):
			self.assertIsNone(consultation._ensure_observations_fn())

	def test_complete_with_prescription_goes_to_pharmacy(self):
		"""Prescriptions are released on completion (decision 2026-11-13) → prescribe → Awaiting Pharmacy."""
		visit, enc = self.consulting()
		add_diagnosis(enc)
		add_drug_row(enc)
		res = self.complete(enc)
		self.assertEqual(len(res["medication_requests"]), 1)
		mr = frappe.get_doc("Medication Request", res["medication_requests"][0])
		self.assertEqual((mr.docstatus, mr.order_group), (1, enc))
		v = frappe.get_doc(VISIT_DOCTYPE, visit)
		self.assertEqual((v.status, v.pharmacy_open), (VISIT_STATUS_AWAITING_PHARMACY, 1))

	def test_complete_without_diagnosis_needs_reason(self):
		"""FR-11: a coded diagnosis or a reason."""
		visit, enc = self.consulting()
		with self.assertRaises(DiagnosisError):
			self.complete(enc)
		res = self.complete(enc, no_diagnosis_reason="Health education visit only")
		self.assertEqual(res["visit_status"], VISIT_STATUS_DISCHARGED)
		self.assertEqual(
			frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "krcs_no_diagnosis_reason"),
			"Health education visit only",
		)
		self.assertEqual(visit_status(visit), VISIT_STATUS_DISCHARGED)

	def test_complete_refuses_unsent_lab(self):
		_visit, enc = self.consulting()
		add_diagnosis(enc)
		add_lab_row(enc)
		with self.assertRaises(ConsultationError):
			self.complete(enc)
		self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "docstatus"), 0)

	def test_complete_twice_refused(self):
		_visit, enc = self.consulting()
		add_diagnosis(enc)
		self.complete(enc)
		with self.assertRaises(ConsultationError):
			self.complete(enc)

	def test_endpoint_roles(self):
		_visit, enc = self.consulting()
		add_lab_row(enc)
		for key in ("reception", "triage", "lab", "pharmacist", "his", "mic", "sysadmin"):
			with as_user(self.u(key)):
				with self.assertRaises(frappe.PermissionError, msg=key):
					consultation.send_lab_orders(enc)
				with self.assertRaises(frappe.PermissionError, msg=key):
					consultation.complete_consultation(enc)

	def test_cancel_lab_order(self):
		visit, enc = self.consulting()
		add_lab_row(enc)
		sr = self.send_labs(enc)["service_requests"][0]
		with as_user(self.u("clinician")):
			with self.assertRaises(ConsultationError):
				consultation.cancel_lab_order(sr, "")
			with patch.object(consultation, "lab_work_started", return_value=True):
				with self.assertRaises(ConsultationError):
					consultation.cancel_lab_order(sr, "Wrong test")
		with as_user(self.u("lab")), self.assertRaises(frappe.PermissionError):
			consultation.cancel_lab_order(sr, "Wrong test")
		with as_user(self.u("clinician")):
			out = consultation.cancel_lab_order(sr, "Wrong test")
			self.assertEqual(out["docstatus"], 2)
			with self.assertRaises(ConsultationError):  # already cancelled
				consultation.cancel_lab_order(sr, "again")
		self.assertTrue(visit)

	def test_lab_work_started(self):
		_visit, enc = self.consulting()
		add_lab_row(enc)
		sr = self.send_labs(enc)["service_requests"][0]
		self.assertFalse(consultation.lab_work_started(sr))
		obs = frappe.get_doc(
			{
				"doctype": "Observation",
				"patient": frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "patient"),
				"observation_template": LAB_TEST,
				"service_request": sr,
			}
		)
		obs.flags.ignore_mandatory = True
		obs.db_insert()
		self.assertFalse(consultation.lab_work_started(sr))
		frappe.db.set_value("Observation", obs.name, "docstatus", 1)
		self.assertTrue(consultation.lab_work_started(sr))

	def test_lab_work_started_by_collected_sample_row(self):
		"""B2 note: a sample row marked Collected on a draft Sample Collection counts as started."""
		_visit, enc = self.consulting()
		add_lab_row(enc)
		sr = self.send_labs(enc)["service_requests"][0]
		sc = frappe.get_doc(
			{
				"doctype": "Sample Collection",
				"patient": frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "patient"),
				"observation_sample_collection": [
					{"observation_template": LAB_TEST, "service_request": sr, "status": "Open"}
				],
			}
		)
		sc.flags.ignore_mandatory = True
		sc.db_insert()
		for row in sc.observation_sample_collection:
			row.parent, row.parenttype, row.parentfield = (
				sc.name,
				"Sample Collection",
				"observation_sample_collection",
			)
			row.db_insert()
		self.assertFalse(consultation.lab_work_started(sr))
		frappe.db.set_value(
			"Observation Sample Collection", sc.observation_sample_collection[0].name, "status", "Collected"
		)
		self.assertTrue(consultation.lab_work_started(sr))

	def test_visit_closed_revokes_unsent_labs(self):
		"""Close Visit handler: unsent lab rows withdrawn; the draft encounter is left alone."""
		visit, enc = self.consulting()
		add_lab_row(enc, LAB_TEST)
		self.send_labs(enc)
		add_lab_row(enc, LAB_TEST_2)
		doc = transition(
			visit, "close_visit", system=True, closure_reason=VISIT_CLOSURE_REASONS[1], comment="T-"
		)
		consultation.on_visit_closed(doc)
		enc_doc = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
		self.assertEqual(enc_doc.docstatus, 0)
		self.assertEqual([r.observation_template for r in enc_doc.lab_test_prescription], [LAB_TEST])
		with as_user(self.u("clinician")), self.assertRaises(ConsultationError):
			consultation.complete_consultation(enc)


class TestDiagnosis(ClinicalTestCase):
	def _doc(self, rows):
		_visit, enc = self.consulting()
		doc = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
		for row in rows:
			doc.append("krcs_diagnoses", row)
		return doc

	def test_single_row_becomes_primary_and_fetches_title(self):
		doc = self._doc([{"code": LEAF_CODE, "case_type": "New"}])
		doc.save(ignore_permissions=True)
		row = doc.krcs_diagnoses[0]
		self.assertEqual((row.is_primary, row.code_value, row.title), (1, "1A00", "Cholera"))

	def test_exactly_one_primary(self):
		two = [{"code": LEAF_CODE, "case_type": "New"}, {"code": LEAF_CODE_2, "case_type": "New"}]
		with self.assertRaises(DiagnosisError):  # none marked
			self._doc(two).save(ignore_permissions=True)
		with self.assertRaises(DiagnosisError):  # two marked
			self._doc([dict(r, is_primary=1) for r in two]).save(ignore_permissions=True)
		doc = self._doc([dict(two[0], is_primary=1), two[1]])
		doc.save(ignore_permissions=True)
		self.assertEqual([r.is_primary for r in doc.krcs_diagnoses], [1, 0])

	def test_duplicate_refused(self):
		doc = self._doc([{"code": LEAF_CODE, "is_primary": 1}, {"code": LEAF_CODE}])
		with self.assertRaises(DiagnosisError):
			doc.save(ignore_permissions=True)

	def test_chapter_x_refused(self):
		"""BR-4: extension codes (chapter X) cannot stand alone."""
		doc = self._doc([{"code": chapter_x_code(), "is_primary": 1}])
		with self.assertRaises(DiagnosisError):
			doc.save(ignore_permissions=True)

	def test_non_icd11_refused(self):
		other = frappe.db.get_value("Code Value", {"code_system": ["!=", "ICD-11"]}, "name")
		doc = self._doc([{"code": other, "is_primary": 1}])
		with self.assertRaises(DiagnosisError):
			doc.save(ignore_permissions=True)

	def test_parent_code_warns(self):
		doc = self._doc([{"code": PARENT_CODE, "is_primary": 1}])
		frappe.clear_messages()
		doc.save(ignore_permissions=True)
		self.assertTrue(any("more specific" in str(m) for m in frappe.message_log))
		self.assertEqual(doc.krcs_diagnoses[0].code_value, "1A03")

	def test_codification_mirrors_rows(self):
		doc = self._doc([{"code": LEAF_CODE, "is_primary": 1}, {"code": LEAF_CODE_2}])
		doc.save(ignore_permissions=True)
		self.assertEqual([r.code for r in doc.codification_table], ["1A00", "1A01"])
		doc.krcs_diagnoses = doc.krcs_diagnoses[:1]
		doc.save(ignore_permissions=True)
		self.assertEqual([r.code for r in doc.codification_table], ["1A00"])
		self.assertEqual(doc.codification_table[0].code_system, "ICD-11")

	def _search(self, txt, page_len=200):
		return [
			r[0]
			for r in diagnosis.search_icd11(
				doctype="Code Value", txt=txt, searchfield="name", start=0, page_len=page_len, filters={}
			)
		]

	def test_picker_ranking(self):
		"""Exact code, then code prefix (leaf first), then title prefix, then contains; no chapter X."""
		self.assertEqual(self._search("1A03")[0], "1A03-ICD-11")  # exact, although a parent code
		results = self._search("1A0")
		self.assertLess(results.index("1A03.0-ICD-11"), results.index("1A03-ICD-11"))  # leaf first
		self.assertLess(results.index("1A00-ICD-11"), results.index("1A03.0-ICD-11"))  # shorter first
		self.assertEqual(self._search("cholera")[0], "1A00-ICD-11")  # title prefix before contains
		x_code = frappe.db.get_value("Code Value", chapter_x_code(), "code_value")
		self.assertNotIn(chapter_x_code(), self._search(x_code))

	def test_picker_title_prefix_before_contains(self):
		results = self._search("cholera")
		titles = [frappe.db.get_value("Code Value", n, "display") for n in results]
		prefix_done = False
		for title in titles:
			starts = title.lower().startswith("cholera")
			if not starts:
				prefix_done = True
			self.assertFalse(prefix_done and starts, titles)


class TestPractitioner(ClinicalTestCase):
	def test_get_or_create_once_and_strip_user_permission(self):
		"""D22: no User Permission from Marley's practitioner on_update."""
		user = self.u("mho")
		name = practitioner.get_or_create(user)
		self.assertEqual(practitioner.get_or_create(user), name)
		self.assertEqual(frappe.db.count("Healthcare Practitioner", {"user_id": user}), 1)
		self.assertFalse(
			frappe.db.exists("User Permission", {"user": user, "allow": "Healthcare Practitioner"})
		)
		doc = frappe.get_doc("Healthcare Practitioner", name)
		doc.save(ignore_permissions=True)
		self.assertFalse(
			frappe.db.exists("User Permission", {"user": user, "allow": "Healthcare Practitioner"})
		)

	def test_only_consulting_roles(self):
		with self.assertRaises(frappe.PermissionError):
			practitioner.get_or_create(self.u("triage"))

	def test_install_strips_existing(self):
		user = self.u("clinician")
		name = practitioner.get_or_create(user)
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user,
				"allow": "Healthcare Practitioner",
				"for_value": name,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		self.assertGreaterEqual(practitioner.strip_all_user_permissions(), 1)
		self.assertFalse(
			frappe.db.exists("User Permission", {"user": user, "allow": "Healthcare Practitioner"})
		)


class TestGuards(ClinicalTestCase):
	def test_marley_endpoints_blocked(self):
		"""Phase 2 audit A3: Marley's unchecked order/referral/cancel endpoints are closed."""
		_visit, enc = self.consulting()
		calls = (
			lambda: guards.create_service_request(enc),
			lambda: guards.create_service_request_from_widget(enc, "{}"),
			lambda: guards.create_patient_referral(enc, "[]"),
			lambda: guards.cancel_request("Service Request", "x"),
			lambda: guards.create_medication_request(enc),
		)
		for user in ("Administrator", self.u("clinician")):
			with as_user(user):
				for call in calls:
					with self.assertRaises(frappe.PermissionError):
						call()

	def test_overrides_wired(self):
		overrides = frappe.get_hooks("override_whitelisted_methods")
		for method in (
			"create_service_request",
			"create_service_request_from_widget",
			"create_patient_referral",
			"cancel_request",
		):
			self.assertEqual(overrides[f"{PE}.{method}"][-1], f"onerc_healthcare.clinical.guards.{method}")


class TestHistory(ClinicalTestCase):
	def test_history_lists_completed_consultations(self):
		visit, enc = self.consulting()
		add_diagnosis(enc)
		add_drug_row(enc)
		self.complete(enc)
		patient = frappe.db.get_value(VISIT_DOCTYPE, visit, "patient")
		with as_user(self.u("clinician")):
			data = history.get_patient_history(patient)
		self.assertEqual([e.name for e in data["encounters"]], [enc])
		item = data["encounters"][0]
		self.assertEqual(item.diagnoses[0].code_value, "1A00")
		self.assertEqual(len(item.prescriptions), 1)
		self.assertTrue(item.prescriptions[0].dispense_status)
		with as_user(self.u("clinician")):
			self.assertEqual(history.get_patient_history(patient, exclude=enc)["encounters"], [])
			lab = history.get_encounter_lab_results(enc)
		self.assertIn("orders", lab)

	def test_history_permission(self):
		visit, _enc = self.consulting()
		patient = frappe.db.get_value(VISIT_DOCTYPE, visit, "patient")
		for key in ("lab", "pharmacist", "sysadmin", "triage"):
			with as_user(self.u(key)), self.assertRaises(frappe.PermissionError, msg=key):
				history.get_patient_history(patient)
