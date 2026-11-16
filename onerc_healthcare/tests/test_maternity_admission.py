# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 5M M1: Maternity Admission (admit, fast path, beds, lifecycle, dating, ward observations). Never commit.

Expectations are worked out by hand in the comments (Naegele: EDD = LMP + 280 calendar days).
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import frappe
from frappe.utils import add_days, getdate, nowdate, random_string, today

from onerc_healthcare import constants as c
from onerc_healthcare.maternity import admission as adm
from onerc_healthcare.maternity.setup import ward_for_facility
from onerc_healthcare.maternity.vital_signs import WardObservationError, validate_ward_observation
from onerc_healthcare.privacy.lifecycle import IllegalTransitionError, LockedFieldError
from onerc_healthcare.reporting import ages, registry, workload
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import (
	LEAF_CODE,
	add_diagnosis,
	as_user,
	chapter_x_code,
	routed_visit,
	start,
)
from onerc_healthcare.tests.clinical_factories import users as visit_users
from onerc_healthcare.tests.factories import get_facility
from onerc_healthcare.tests.maternity_factories import make_bed, make_maternity_patient, maternity_users
from onerc_healthcare.tests.visit_factories import make_visit

MAT = c.MATERNITY_ADMISSION_DOCTYPE
HSU = "Healthcare Service Unit"


class MaternityCase(EMRTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users = maternity_users()
		cls.vusers = visit_users()
		frappe.set_user("Administrator")

	def setUp(self):
		super().setUp()
		self.facility = get_facility(0)
		self.ward = ward_for_facility(self.facility)
		if not self.ward:
			self.skipTest("No Maternity Ward seeded on this site (maternity.setup.seed_maternity_wards).")

	def u(self, key: str) -> str:
		return self.users[key]

	def mother(self, **kw) -> str:
		return make_maternity_patient(**kw).name

	def maternity_visit(self, patient: str | None = None):
		return routed_visit(c.DEPT_MATERNITY, patient=patient or self.mother())

	def admit(self, user: str = "midwife", **kw) -> dict:
		with as_user(self.u(user)):
			return adm.admit(**kw)

	def act(self, name: str, action: str, user: str = "midwife", **values) -> dict:
		with as_user(self.u(user)):
			return adm.apply_action(name, action, values)

	def bed_status(self, bed: str) -> str:
		return frappe.db.get_value(HSU, bed, "occupancy_status")

	def admission(self, name: str):
		return frappe.get_doc(MAT, name)


# ------------------------------------------------------------------------------------------------ admit


class TestAdmit(MaternityCase):
	def test_transition_table(self):
		t = adm.TRANSITIONS
		self.assertEqual(set(t), {"admit", "start_labour", "record_delivery", "discharge"})
		self.assertEqual((t["admit"].from_statuses, t["admit"].to_status), ((), c.ADMISSION_ADMITTED))
		self.assertEqual(t["start_labour"].from_statuses, (c.ADMISSION_ADMITTED,))
		self.assertEqual(t["record_delivery"].from_statuses, (c.ADMISSION_ADMITTED, c.ADMISSION_IN_LABOUR))
		self.assertTrue(t["record_delivery"].is_system)
		self.assertEqual(t["discharge"].from_statuses, tuple(c.ADMISSION_OPEN_STATUSES))
		for action in ("admit", "start_labour", "discharge"):
			self.assertEqual(set(t[action].roles), set(c.MATERNITY_ROLES))

	def test_normal_path_from_routed_visit(self):
		visit = self.maternity_visit()
		out = self.admit(patient=visit.patient, visit=visit.name, values={"remarks": "T- contractions 3/10"})
		doc = self.admission(out["name"])
		self.assertEqual((out["status"], out["visit"], out["bed"]), (c.ADMISSION_ADMITTED, visit.name, None))
		self.assertEqual((doc.patient, doc.visit, doc.facility), (visit.patient, visit.name, self.facility))
		self.assertEqual(doc.ward, self.ward)  # defaults to the facility's Maternity Ward
		self.assertEqual(doc.admitted_by, self.u("midwife"))
		self.assertEqual(doc.remarks, "T- contractions 3/10")
		self.assertEqual(
			[(r.action, r.from_status, r.to_status, r.user) for r in doc.status_log],
			[("admit", None, c.ADMISSION_ADMITTED, self.u("midwife"))],
		)
		v = frappe.get_doc(c.VISIT_DOCTYPE, visit.name)
		self.assertEqual(
			(v.status, v.outcome, v.current_department),
			(c.VISIT_STATUS_DISCHARGED, c.OUTCOME_ADMITTED, c.DEPT_MATERNITY),
		)
		self.assertTrue(v.closed_at and v.outcome_at)
		self.assertEqual(v.outcome_by, self.u("midwife"))
		self.assertEqual(v.status_log[-1].action, "admit")

	def test_admit_from_registered_visit(self):
		visit = make_visit(patient=self.mother())
		out = self.admit(patient=visit.patient, visit=visit.name, user="clinician")
		self.assertEqual(frappe.db.get_value(c.VISIT_DOCTYPE, visit.name, "outcome"), c.OUTCOME_ADMITTED)
		self.assertEqual(out["status"], c.ADMISSION_ADMITTED)

	def test_mic_admits_without_create_right(self):
		visit = self.maternity_visit()
		out = self.admit(patient=visit.patient, visit=visit.name, user="mic")
		self.assertEqual(frappe.db.get_value(MAT, out["name"], "admitted_by"), self.u("mic"))

	def test_refused_roles(self):
		visit = self.maternity_visit()
		for key in ("reception", "his", "mho"):
			with self.subTest(role=key), self.assertRaises(frappe.PermissionError):
				self.admit(key, patient=visit.patient, visit=visit.name)
		self.assertEqual(frappe.db.get_value(c.VISIT_DOCTYPE, visit.name, "status"), c.VISIT_STATUS_ROUTED)
		self.assertFalse(frappe.db.exists(MAT, {"patient": visit.patient}))

	def test_visit_of_another_patient_refused(self):
		visit = self.maternity_visit()
		with self.assertRaises(adm.MaternityAdmissionError):
			self.admit(patient=self.mother(), visit=visit.name)

	def test_visit_in_consultation_refused(self):
		visit = self.maternity_visit()
		start(visit.name, self.vusers["clinician"])
		with self.assertRaises(adm.MaternityAdmissionError):
			self.admit(patient=visit.patient, visit=visit.name)

	def test_male_patient_refused(self):
		visit = make_visit()  # factories.make_patient: a woman by default
		frappe.db.set_value("Patient", visit.patient, "sex", "Male")
		with self.assertRaises(adm.MaternityAdmissionError):
			self.admit(patient=visit.patient, visit=visit.name)

	def test_hand_made_insert_refused(self):
		doc = frappe.get_doc(
			{
				"doctype": MAT,
				"patient": self.mother(),
				"facility": self.facility,
				"status": c.ADMISSION_ADMITTED,
			}
		)
		with as_user(self.u("midwife")), self.assertRaises(frappe.PermissionError):
			doc.insert()

	def test_server_fields_in_values_refused(self):
		visit = self.maternity_visit()
		for bad in ({"status": c.ADMISSION_DISCHARGED}, {"bed": "x"}, {"admitted_by": "Administrator"}):
			with self.subTest(values=bad), self.assertRaises(adm.MaternityAdmissionError):
				self.admit(patient=visit.patient, visit=visit.name, values=bad)

	def test_second_open_admission_refused(self):
		visit = self.maternity_visit()
		first = self.admit(patient=visit.patient, visit=visit.name)["name"]
		with self.assertRaises(adm.OpenAdmissionExistsError):
			self.admit(patient=visit.patient)  # fast path: refused before any visit is opened
		self.assertEqual(frappe.db.count(c.VISIT_DOCTYPE, {"patient": visit.patient}), 1)
		# Discharged: a new admission is allowed again.
		self.act(first, "discharge", discharge_outcome=c.MATERNITY_DISCHARGED_HOME)
		second = self.admit(patient=visit.patient)
		self.assertNotEqual(second["name"], first)

	def test_same_visit_twice_refused(self):
		visit = self.maternity_visit()
		first = self.admit(patient=visit.patient, visit=visit.name)["name"]
		self.act(first, "discharge", discharge_outcome=c.MATERNITY_DISCHARGED_HOME)
		# The visit is now Discharged / Admitted, but it already has its admission.
		with self.assertRaises(adm.OpenAdmissionExistsError):
			self.admit(patient=visit.patient, visit=visit.name)

	def test_diagnoses_icd11(self):
		visit = self.maternity_visit()
		out = self.admit(
			patient=visit.patient,
			visit=visit.name,
			values={"diagnoses": [{"code": LEAF_CODE, "case_type": "New"}]},
		)
		rows = self.admission(out["name"]).diagnoses
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].is_primary, 1)  # a single row becomes primary
		self.assertEqual(rows[0].code_value, frappe.db.get_value("Code Value", LEAF_CODE, "code_value"))
		code_x = chapter_x_code()
		if code_x:
			from onerc_healthcare.clinical.diagnosis import DiagnosisError

			other = self.maternity_visit()
			with self.assertRaises(DiagnosisError):
				self.admit(patient=other.patient, visit=other.name, values={"diagnoses": [{"code": code_x}]})


class TestFastPath(MaternityCase):
	def test_new_patient_registered_by_midwife_then_admitted(self):
		with as_user(self.u("midwife")):
			patient = (
				frappe.get_doc(
					{
						"doctype": "Patient",
						"first_name": f"T-{random_string(6)}",
						"last_name": "T-Labour",
						"sex": "Female",
						"dob": "2000-01-01",
						c.PATIENT_IDENTIFIERS_FIELD: [
							{
								"identifier_type": c.IDENTIFIER_UNHCR,
								"identifier_value": f"T-UNHCR-{random_string(8)}",
							}
						],
					}
				)
				.insert()
				.name
			)
			out = adm.admit(patient=patient, values={"lmp": add_days(today(), -266)})
		visits = frappe.get_all(
			c.VISIT_DOCTYPE,
			filters={"patient": patient},
			fields=[
				"name",
				"status",
				"outcome",
				"attendance_type",
				"facility",
				"queue_number",
				"current_department",
			],
		)
		self.assertEqual(len(visits), 1)  # the attendance counts once
		v = visits[0]
		self.assertEqual(out["visit"], v.name)
		self.assertEqual(
			(v.status, v.outcome, v.current_department, v.attendance_type),
			(c.VISIT_STATUS_DISCHARGED, c.OUTCOME_ADMITTED, c.DEPT_MATERNITY, c.ATTENDANCE_NEW),
		)
		self.assertTrue(v.queue_number)
		registration = frappe.db.get_value("Patient", patient, "krcs_registration_facility")
		self.assertEqual(v.facility, registration)
		self.assertEqual(frappe.db.get_value(MAT, out["name"], "facility"), registration)
		# 266 days = 38 weeks exactly
		self.assertEqual(frappe.db.get_value(MAT, out["name"], "gestation_weeks"), 38)
		log = frappe.get_doc(c.VISIT_DOCTYPE, v.name).status_log
		self.assertEqual([r.action for r in log], ["register", "admit"])
		self.assertEqual(log[0].user, self.u("midwife"))

	def test_fast_path_uses_the_open_visit(self):
		visit = make_visit(patient=self.mother())
		out = self.admit(patient=visit.patient)
		self.assertEqual(out["visit"], visit.name)
		self.assertEqual(frappe.db.count(c.VISIT_DOCTYPE, {"patient": visit.patient}), 1)

	def test_fast_path_facility_choice(self):
		patient = self.mother()
		other = get_facility(1)
		out = self.admit(patient=patient, values={"facility": other})
		self.assertEqual(frappe.db.get_value(c.VISIT_DOCTYPE, out["visit"], "facility"), other)
		self.assertEqual(frappe.db.get_value(MAT, out["name"], "ward"), ward_for_facility(other))

	def test_attendance_counts_once_with_outcome_admitted(self):
		"""MOH 717-E02 "Patients sent for admission": +1 for one admission (the visit, once)."""
		ctx = registry.SourceContext(
			report="T-MAT",
			from_date=getdate(today()),
			to_date=getdate(today()),
			facility=self.facility,
			age_bands=(ages.AgeBand("all", 0),),
		)
		spec = registry.ElementSpec(
			"E02", "E02", "Visit Outcome", {"outcomes": [c.OUTCOME_ADMITTED]}, registry.DISAGGREGATION_NONE
		)

		def e02() -> int:
			return sum(workload.evaluate_outcomes(ctx, [spec]).values())

		before = e02()
		visit = self.maternity_visit()
		self.admit(patient=visit.patient, visit=visit.name)
		self.assertEqual(e02(), before + 1)


class TestFromClinician(MaternityCase):
	def test_visit_closed_admitted_by_clinician_is_linked(self):
		from onerc_healthcare.clinical.consultation import complete_consultation

		visit = routed_visit(c.DEPT_OUTPATIENT, patient=self.mother())
		enc = start(visit.name, self.vusers["clinician"])
		add_diagnosis(enc)
		with as_user(self.vusers["clinician"]):
			complete_consultation(enc, outcome=c.OUTCOME_ADMITTED)
		v = frappe.get_doc(c.VISIT_DOCTYPE, visit.name)
		self.assertEqual((v.status, v.outcome), (c.VISIT_STATUS_DISCHARGED, c.OUTCOME_ADMITTED))
		log_before = len(v.status_log)
		out = self.admit(patient=visit.patient, visit=visit.name)
		self.assertEqual(out["visit"], visit.name)
		self.assertEqual(
			len(frappe.get_doc(c.VISIT_DOCTYPE, visit.name).status_log), log_before
		)  # no transition

	def test_closed_visit_with_other_outcome_refused(self):
		from onerc_healthcare.clinical.consultation import complete_consultation

		visit = routed_visit(c.DEPT_OUTPATIENT, patient=self.mother())
		enc = start(visit.name, self.vusers["clinician"])
		add_diagnosis(enc)
		with as_user(self.vusers["clinician"]):
			complete_consultation(enc)  # Discharged home
		with self.assertRaises(adm.MaternityAdmissionError):
			self.admit(patient=visit.patient, visit=visit.name)

	def test_internal_referral_received(self):
		from onerc_healthcare.clinical import referral as ref

		visit = routed_visit(c.DEPT_OUTPATIENT, patient=self.mother())
		enc = start(visit.name, self.vusers["clinician"])
		add_diagnosis(enc)
		with as_user(self.vusers["clinician"]):
			sent = ref.create_referral(
				enc, "Internal", "T- in labour", to_department=c.DEPT_MATERNITY, send=True
			)
		target = sent["target_visit"]
		self.assertEqual(
			frappe.db.get_value(c.VISIT_DOCTYPE, target, ["status", "current_department"]),
			(c.VISIT_STATUS_ROUTED, c.DEPT_MATERNITY),
		)
		self.assertEqual(frappe.db.get_value(c.REFERRAL_DOCTYPE, sent["name"], "status"), ref.STATUS_SENT)
		out = self.admit(patient=visit.patient, visit=target)
		self.assertEqual(frappe.db.get_value(c.REFERRAL_DOCTYPE, sent["name"], "status"), ref.STATUS_RECEIVED)
		log = frappe.get_doc(c.REFERRAL_DOCTYPE, sent["name"]).status_log
		self.assertEqual([r.action for r in log], ["save", "send", "receive"])
		self.assertIn(sent["name"], self.admission(out["name"]).status_log[0].comment)


# ------------------------------------------------------------------------------------------------- beds


class TestBeds(MaternityCase):
	def test_bed_occupied_then_freed_on_discharge(self):
		bed = make_bed(self.ward)
		visit = self.maternity_visit()
		out = self.admit(patient=visit.patient, visit=visit.name, bed=bed)
		self.assertEqual(out["bed"], bed)
		self.assertEqual(self.bed_status(bed), "Occupied")
		# Another mother cannot take the bed.
		other = self.maternity_visit()
		with self.assertRaises(adm.BedUnavailableError):
			self.admit(patient=other.patient, visit=other.name, bed=bed)
		self.assertEqual(frappe.db.get_value(c.VISIT_DOCTYPE, other.name, "status"), c.VISIT_STATUS_ROUTED)
		self.act(out["name"], "discharge", discharge_outcome=c.MATERNITY_DISCHARGED_HOME)
		self.assertEqual(self.bed_status(bed), "Vacant")
		self.admit(patient=other.patient, visit=other.name, bed=bed)
		self.assertEqual(self.bed_status(bed), "Occupied")

	def test_bed_marked_occupied_refused(self):
		bed = make_bed(self.ward)
		frappe.db.set_value(HSU, bed, "occupancy_status", "Occupied")
		visit = self.maternity_visit()
		with self.assertRaises(adm.BedUnavailableError):
			self.admit(patient=visit.patient, visit=visit.name, bed=bed)

	def test_not_a_bed_of_the_ward_refused(self):
		other_ward = ward_for_facility(get_facility(1))
		if not other_ward:
			self.skipTest("Second facility has no Maternity Ward.")
		visit = self.maternity_visit()
		for bad in (make_bed(other_ward), self.ward):  # a bed of another ward; the ward group itself
			with self.subTest(bed=bad), self.assertRaises(adm.BedUnavailableError):
				self.admit(patient=visit.patient, visit=visit.name, bed=bad)
		self.assertEqual(frappe.db.get_value(c.VISIT_DOCTYPE, visit.name, "status"), c.VISIT_STATUS_ROUTED)

	def test_ward_of_another_facility_refused(self):
		other_ward = ward_for_facility(get_facility(1))
		if not other_ward:
			self.skipTest("Second facility has no Maternity Ward.")
		visit = self.maternity_visit()
		with self.assertRaises(adm.MaternityAdmissionError):
			self.admit(patient=visit.patient, visit=visit.name, ward=other_ward)

	def test_free_bed_keeps_a_bed_held_by_another_open_admission(self):
		bed = make_bed(self.ward)
		visit = self.maternity_visit()
		self.admit(patient=visit.patient, visit=visit.name, bed=bed)
		adm.free_bed(bed)
		self.assertEqual(self.bed_status(bed), "Occupied")
		adm.free_bed(None)  # no-op

	def test_archive_frees_the_bed_and_closes_the_admission(self):
		bed = make_bed(self.ward)
		visit = self.maternity_visit()
		name = self.admit(patient=visit.patient, visit=visit.name, bed=bed)["name"]
		with as_user(self.u("midwife")), self.assertRaises(LockedFieldError):
			doc = self.admission(name)
			doc.krcs_archived = 1
			doc.save()
		with as_user(self.u("mic")):
			doc = self.admission(name)
			doc.krcs_archived = 1
			doc.save()
		self.assertEqual(self.bed_status(bed), "Vacant")
		self.assertIsNone(adm.open_admission(visit.patient))
		with self.assertRaises(IllegalTransitionError):
			self.act(name, "start_labour")


# -------------------------------------------------------------------------------------------- lifecycle


class TestLifecycle(MaternityCase):
	def admitted(self, **kw) -> str:
		visit = self.maternity_visit()
		return self.admit(patient=visit.patient, visit=visit.name, **kw)["name"]

	def test_full_path(self):
		name = self.admitted()
		self.assertEqual(self.act(name, "start_labour")["status"], c.ADMISSION_IN_LABOUR)
		adm.record_delivery(name, "T-DEL-1")
		self.assertEqual(frappe.db.get_value(MAT, name, "status"), c.ADMISSION_DELIVERED)
		out = self.act(
			name,
			"discharge",
			discharge_outcome=c.MATERNITY_DISCHARGED_HOME,
			discharge_condition="T- well",
			discharge_notes="T- review in 6 weeks",
		)
		self.assertEqual(out["status"], c.ADMISSION_DISCHARGED)
		doc = self.admission(name)
		self.assertEqual(
			(
				doc.discharge_outcome,
				doc.discharge_condition,
				doc.discharge_notes,
				doc.discharged_by,
				doc.death_reason,
			),
			(c.MATERNITY_DISCHARGED_HOME, "T- well", "T- review in 6 weeks", self.u("midwife"), None),
		)
		self.assertTrue(doc.discharged_at)
		self.assertEqual(
			[(r.action, r.from_status, r.to_status) for r in doc.status_log],
			[
				("admit", None, c.ADMISSION_ADMITTED),
				("start_labour", c.ADMISSION_ADMITTED, c.ADMISSION_IN_LABOUR),
				("record_delivery", c.ADMISSION_IN_LABOUR, c.ADMISSION_DELIVERED),
				("discharge", c.ADMISSION_DELIVERED, c.ADMISSION_DISCHARGED),
			],
		)
		self.assertIn("T-DEL-1", doc.status_log[2].comment)

	def test_record_delivery_rules(self):
		name = self.admitted()
		adm.record_delivery(name, "T-DEL-1")  # straight from Admitted
		with self.assertRaises(IllegalTransitionError):
			adm.record_delivery(name, "T-DEL-2")  # second delivery
		with self.assertRaises(IllegalTransitionError):
			self.act(name, "record_delivery")  # never a user action
		with self.assertRaises(IllegalTransitionError):
			self.act(name, "admit")
		with self.assertRaises(IllegalTransitionError):
			self.act(name, "start_labour")  # Delivered -> In Labour is not a transition
		self.act(name, "discharge", discharge_outcome=c.MATERNITY_ABSCONDED)
		with self.assertRaises(IllegalTransitionError):
			adm.record_delivery(name, "T-DEL-3")  # not open

	def test_discharged_is_final(self):
		name = self.admitted()
		self.act(name, "discharge", discharge_outcome=c.MATERNITY_REFERRED_OUT)
		for action, values in (
			("discharge", {"discharge_outcome": c.MATERNITY_DISCHARGED_HOME}),
			("start_labour", {}),
		):
			with self.subTest(action=action), self.assertRaises(IllegalTransitionError):
				self.act(name, action, **values)

	def test_discharge_outcomes(self):
		name = self.admitted()
		with self.assertRaises(IllegalTransitionError):
			self.act(name, "discharge")  # outcome required
		with self.assertRaises(IllegalTransitionError):
			self.act(name, "discharge", discharge_outcome="Transferred")
		with self.assertRaises(IllegalTransitionError):
			self.act(name, "discharge", discharge_outcome=c.MATERNITY_DIED)  # cause required
		with self.assertRaises(IllegalTransitionError):
			self.act(name, "discharge", discharge_outcome=c.MATERNITY_DISCHARGED_HOME, status="Admitted")
		self.assertEqual(frappe.db.get_value(MAT, name, "status"), c.ADMISSION_ADMITTED)

	def test_died_by_role(self):
		# Every maternity role may record a maternal death (DIED_ROLES includes the Midwife).
		self.assertTrue(set(c.MATERNITY_ROLES) <= set(c.DIED_ROLES))
		name = self.admitted()
		with patch.object(adm, "DIED_ROLES", (c.ROLE_MIC,)), self.assertRaises(frappe.PermissionError):
			self.act(name, "discharge", discharge_outcome=c.MATERNITY_DIED, death_reason="T- PPH")
		self.act(name, "discharge", discharge_outcome=c.MATERNITY_DIED, death_reason="T- PPH")
		doc = self.admission(name)
		self.assertEqual((doc.discharge_outcome, doc.death_reason), (c.MATERNITY_DIED, "T- PPH"))

	def test_death_reason_dropped_for_other_outcomes(self):
		name = self.admitted()
		self.act(name, "discharge", discharge_outcome=c.MATERNITY_DISCHARGED_HOME, death_reason="T- typo")
		self.assertIsNone(frappe.db.get_value(MAT, name, "death_reason"))

	def test_action_roles(self):
		name = self.admitted()
		for key in ("reception", "his", "mho"):
			with self.subTest(role=key), self.assertRaises(frappe.PermissionError):
				self.act(name, "start_labour", user=key)
		self.act(name, "start_labour", user="clinician")
		self.act(name, "discharge", user="mic", discharge_outcome=c.MATERNITY_DISCHARGED_HOME)

	def test_allowed_actions(self):
		name = self.admitted()
		doc = self.admission(name)
		self.assertEqual(
			[a["action"] for a in adm.allowed_actions(doc, self.u("midwife"))], ["start_labour", "discharge"]
		)
		self.assertEqual(adm.allowed_actions(doc, self.u("his")), [])
		adm.record_delivery(name, "T-DEL-1")
		doc.reload()
		self.assertEqual([a["action"] for a in adm.allowed_actions(doc, self.u("midwife"))], ["discharge"])

	def test_status_and_server_fields_not_editable(self):
		name = self.admitted()
		for field, value in (("status", c.ADMISSION_DISCHARGED), ("discharge_outcome", c.MATERNITY_DIED)):
			with self.subTest(field=field), as_user(self.u("midwife")), self.assertRaises(LockedFieldError):
				doc = self.admission(name)
				doc.set(field, value)
				doc.save()

	def test_log_append_only(self):
		name = self.admitted()
		self.act(name, "start_labour")
		with as_user(self.u("midwife")):
			doc = self.admission(name)
			doc.status_log[0].comment = "T- rewritten"
			with self.assertRaises(LockedFieldError):
				doc.save()
			doc = self.admission(name)
			doc.status_log = doc.status_log[1:]
			with self.assertRaises(LockedFieldError):
				doc.save()
			doc = self.admission(name)
			doc.append(
				"status_log",
				{"to_status": "X", "action": "x", "user": "Administrator", "timestamp": frappe.utils.now()},
			)
			with self.assertRaises(LockedFieldError):
				doc.save()
		self.assertEqual(len(self.admission(name).status_log), 2)

	def test_clinical_fields_editable_while_open_read_only_after_discharge(self):
		name = self.admitted()
		with as_user(self.u("midwife")):
			doc = self.admission(name)
			doc.remarks = "T- progressing"
			doc.save()
		self.act(name, "discharge", discharge_outcome=c.MATERNITY_DISCHARGED_HOME)
		with as_user(self.u("clinician")), self.assertRaises(LockedFieldError):
			doc = self.admission(name)
			doc.remarks = "T- late edit"
			doc.save()
		with as_user(self.u("mic")):
			doc = self.admission(name)
			doc.krcs_archived = 1
			doc.save()  # archive allowed on a discharged admission
		self.assertEqual(frappe.db.get_value(MAT, name, "krcs_archived"), 1)

	def test_never_deleted(self):
		name = self.admitted()
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc(MAT, name, ignore_permissions=True)


# ---------------------------------------------------------------------------------- dating and ranges


class TestDating(MaternityCase):
	def test_naegele_pure(self):
		# Non-leap span: 2023-02-01 + 280 = Nov 8 (Feb 28 d -> Mar 1 = 28, ..., Nov 1 = 273, + 7).
		self.assertEqual(adm.edd_from_lmp("2023-02-01"), date(2023, 11, 8))
		# Leap span (29 Feb 2024): one day earlier, Nov 7 (Mar 1 = 29, ..., Nov 1 = 274, + 6).
		self.assertEqual(adm.edd_from_lmp("2024-02-01"), date(2024, 11, 7))
		# Across the year end and 29 Feb 2024: 2023-12-31 + 280 = 2024-10-06.
		self.assertEqual(adm.edd_from_lmp("2023-12-31"), date(2024, 10, 6))
		# Gestation on 2024-11-01 from LMP 2024-02-01: 274 days = 39 weeks 1 day.
		self.assertEqual(adm.gestation("2024-02-01", None, "2024-11-01"), (39, 1))
		# From the EDD only: EDD 2024-11-07 on 2024-11-01 -> 280 - 6 = 274 days, the same 39+1.
		self.assertEqual(adm.gestation(None, "2024-11-07", "2024-11-01"), (39, 1))
		self.assertIsNone(adm.gestation(None, None, "2024-11-01"))

	def test_controller_sets_edd_and_gestation(self):
		visit = self.maternity_visit()
		lmp = add_days(today(), -275)  # 275 days = 39 weeks 2 days
		out = self.admit(patient=visit.patient, visit=visit.name, values={"lmp": lmp})
		doc = self.admission(out["name"])
		self.assertEqual(getdate(doc.edd), getdate(add_days(lmp, 280)))  # 5 days from today
		self.assertEqual(doc.gestation_weeks, 39)
		self.assertEqual(adm.gestation_label(doc), "39+2 weeks")

	def test_typed_edd_kept_and_gestation_from_edd(self):
		visit = self.maternity_visit()
		edd = add_days(today(), 14)  # 280 - 14 = 266 days = 38 weeks
		out = self.admit(patient=visit.patient, visit=visit.name, values={"edd": edd})
		doc = self.admission(out["name"])
		self.assertEqual((getdate(doc.edd), doc.gestation_weeks), (getdate(edd), 38))

	def test_implausible_dates_refused(self):
		visit = self.maternity_visit()
		cases = (
			{"lmp": add_days(today(), 1)},  # after admission
			{"lmp": add_days(today(), -320)},  # 45 weeks 5 days > 45 weeks
			{"lmp": add_days(today(), -200), "edd": add_days(today(), -210)},  # EDD before LMP
			{"edd": add_days(today(), 300)},  # gestation -20 days
		)
		for values in cases:
			with self.subTest(values=values), self.assertRaises(adm.ImplausibleAdmissionValueError):
				self.admit(patient=visit.patient, visit=visit.name, values=values)

	def test_gravida_para(self):
		self.assertEqual(adm.parse_para("2 + 1"), (2, 1))
		self.assertEqual(adm.parse_para("3"), (3, 0))
		self.assertIsNone(adm.parse_para(""))
		visit = self.maternity_visit()
		for values in ({"gravida": 2, "para": "2+1"}, {"gravida": -1}, {"para": "two"}, {"para": "1+1+1"}):
			with self.subTest(values=values), self.assertRaises(adm.ImplausibleAdmissionValueError):
				self.admit(patient=visit.patient, visit=visit.name, values=values)
		out = self.admit(patient=visit.patient, visit=visit.name, values={"gravida": 4, "para": " 2+1 "})
		self.assertEqual(frappe.db.get_value(MAT, out["name"], "para"), "2+1")

	def test_examination_ranges(self):
		visit = self.maternity_visit()
		bad = (
			{"foetal_heart_rate": 300},
			{"temperature": 98.6},  # Fahrenheit
			{"pulse": 400},
			{"bp_systolic": 80, "bp_diastolic": 90},
			{"bp_systolic": 120},
			{"haemoglobin": 80},
			{"fundal_height_cm": 95},
			{"weight_kg": 500},
			{"obstetric_history": [{"year": 1900}]},
			{"obstetric_history": [{"birth_weight_kg": 35}]},
		)
		for values in bad:
			with self.subTest(values=values), self.assertRaises(adm.ImplausibleAdmissionValueError):
				self.admit(patient=visit.patient, visit=visit.name, values=values)
		good = {
			"foetal_heart_rate": 140,
			"temperature": 36.8,
			"pulse": 88,
			"respiratory_rate": 18,
			"bp_systolic": 120,
			"bp_diastolic": 80,
			"haemoglobin": 11.2,
			"fundal_height_cm": 36,
			"weight_kg": 68,
			"presentation": "Cephalic",
			"obstetric_history": [{"year": 2022, "birth_weight_kg": 3.1, "alive_or_dead": "Alive"}],
		}
		out = self.admit(patient=visit.patient, visit=visit.name, values=good)
		doc = self.admission(out["name"])
		self.assertEqual((doc.foetal_heart_rate, doc.bp_systolic, len(doc.obstetric_history)), (140, 120, 1))

	def test_lab_result_must_be_the_mothers(self):
		def observation(patient: str) -> str:
			# A bare result row (no controller): only its patient matters to the admission check.
			doc = frappe.get_doc({"doctype": "Observation", "patient": patient})
			doc.name = f"T-OBS-{random_string(8)}"
			doc.db_insert()
			return doc.name

		visit = self.maternity_visit()
		with self.assertRaises(adm.ImplausibleAdmissionValueError):
			self.admit(
				patient=visit.patient,
				visit=visit.name,
				values={"blood_group_result": observation(self.mother())},
			)
		own = observation(visit.patient)
		out = self.admit(
			patient=visit.patient, visit=visit.name, values={"blood_group": "O", "blood_group_result": own}
		)
		self.assertEqual(
			frappe.db.get_value(MAT, out["name"], ["blood_group", "blood_group_result"]), ("O", own)
		)


# --------------------------------------------------------------------------------- ward observations


class TestWardObservations(MaternityCase):
	def test_midwife_records_observations(self):
		visit = self.maternity_visit()
		name = self.admit(patient=visit.patient, visit=visit.name)["name"]
		with as_user(self.u("midwife")):
			vs = adm.record_observation(
				name, {"temperature": "37.1", "pulse": "92", "bp_systolic": "118", "bp_diastolic": "76"}
			)
		doc = frappe.get_doc("Vital Signs", vs["name"])
		self.assertEqual((doc.docstatus, doc.patient, doc.get("krcs_visit")), (1, visit.patient, None))
		self.assertEqual(doc.get(c.VITAL_SIGNS_ADMISSION_FIELD), name)
		self.assertEqual(doc.owner, self.u("midwife"))
		# The closed visit is untouched (vital_signs_on_submit ignores ward observations).
		self.assertEqual(
			frappe.db.get_value(c.VISIT_DOCTYPE, visit.name, "triage_vital_signs"), visit.triage_vital_signs
		)
		with as_user(self.u("midwife")):
			rows = adm.ward_observations(name)
		self.assertEqual([r.name for r in rows], [vs["name"]])

	def test_observation_rules(self):
		visit = self.maternity_visit()
		name = self.admit(patient=visit.patient, visit=visit.name)["name"]
		with as_user(self.u("midwife")):
			with self.assertRaises(adm.MaternityAdmissionError):
				adm.record_observation(name, {"vital_signs_note": "T- only a note"})
			with self.assertRaises(adm.MaternityAdmissionError):
				adm.record_observation(name, {"temperature": "37", "krcs_visit": visit.name})
			from onerc_healthcare.visit.events import ImplausibleVitalSignsError

			with self.assertRaises(ImplausibleVitalSignsError):
				adm.record_observation(name, {"pulse": "900"})
		with as_user(self.u("his")), self.assertRaises(frappe.PermissionError):
			adm.record_observation(name, {"temperature": "37"})
		self.act(name, "discharge", discharge_outcome=c.MATERNITY_DISCHARGED_HOME)
		with as_user(self.u("midwife")), self.assertRaises(adm.MaternityAdmissionError):
			adm.record_observation(name, {"temperature": "37"})

	def test_link_validation(self):
		visit = self.maternity_visit()
		name = self.admit(patient=visit.patient, visit=visit.name)["name"]

		def vs(**kw):
			doc = frappe.get_doc(
				{
					"doctype": "Vital Signs",
					"patient": visit.patient,
					"signs_date": nowdate(),
					c.VITAL_SIGNS_ADMISSION_FIELD: name,
					**kw,
				}
			)
			doc._action = "save"
			return doc

		validate_ward_observation(vs())  # same patient, open admission
		with self.assertRaises(WardObservationError):
			validate_ward_observation(vs(patient=self.mother()))
		with self.assertRaises(WardObservationError):
			validate_ward_observation(vs(krcs_visit=visit.name))
		self.act(name, "discharge", discharge_outcome=c.MATERNITY_DISCHARGED_HOME)
		with self.assertRaises(WardObservationError):
			validate_ward_observation(vs())
		closed = vs()
		closed._action = None  # an existing submitted observation is still valid after discharge
		validate_ward_observation(closed)

	def test_onload(self):
		visit = self.maternity_visit()
		name = self.admit(patient=visit.patient, visit=visit.name, values={"lmp": add_days(today(), -260)})[
			"name"
		]
		with as_user(self.u("midwife")):
			doc = self.admission(name)
			doc.run_method("onload")
			data = doc.get_onload()
		self.assertEqual([a["action"] for a in data.allowed_actions], ["start_labour", "discharge"])
		self.assertEqual(data.gestation_label, "37+1 weeks")  # 260 = 37 * 7 + 1
		self.assertEqual(data.observations, [])
		self.assertTrue(data.can_open_partograph)


# ---------------------------------------------------------------------------------- visit / patient state


class TestFormState(MaternityCase):
	def test_visit_and_patient_state(self):
		visit = self.maternity_visit()
		with as_user(self.u("midwife")):
			state = adm.get_visit_state(visit.name)
			self.assertEqual((state["can_admit"], state["maternity"], state["admission"]), (True, True, None))
			self.assertEqual(adm.get_patient_state(visit.patient)["admission"], None)
			self.assertEqual(adm.get_admit_defaults(visit=visit.name)["ward"], self.ward)
		name = self.admit(patient=visit.patient, visit=visit.name)["name"]
		with as_user(self.u("midwife")):
			self.assertEqual(adm.get_visit_state(visit.name)["admission"], name)
			self.assertFalse(adm.get_visit_state(visit.name)["can_admit"])
			self.assertEqual(adm.get_patient_state(visit.patient)["admission"], name)
		with as_user(self.vusers["reception"]):
			other = make_visit(patient=self.mother(), ignore_permissions=False)
			self.assertFalse(adm.get_visit_state(other.name)["can_admit"])
