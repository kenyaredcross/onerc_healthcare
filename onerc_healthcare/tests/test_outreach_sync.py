# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Outreach sync (Phase 4, O2): `outreach.sync.push` / `status` and `outreach.apply` (OUT-14..17, FR-24/25,
TST-05/06/07/11; protocol docs/OUTREACH_PROTOCOL.md §4). Never commit.

Every expectation is worked out by hand in the test's docstring or comments. Records are sent as the team member
through `push`, exactly as the device does (a cookie sign-in; the device-grant hook is O1's)."""

from __future__ import annotations

import random
import string
import time
import uuid
from datetime import date, datetime, timedelta

import frappe
from frappe.utils import add_days, add_years, get_datetime, getdate, now_datetime, random_string, today

from onerc_healthcare.constants import (
	COMMUNITY_ACTIVITY_DOCTYPE,
	DISPENSING_DOCTYPE,
	ENCOUNTER_DOCTYPE,
	IDENTIFIER_UNHCR,
	OUTCOME_DISCHARGED,
	OUTREACH_BATCH_DOCTYPE,
	OUTREACH_LAB_PENDING_REASON,
	OUTREACH_NO_VITALS_REASON,
	OUTREACH_RECORD_DOCTYPE,
	OUTREACH_SESSION_DOCTYPE,
	RECORD_COMMUNITY_ACTIVITY,
	RECORD_PATIENT_REGISTER,
	RECORD_PATIENT_UPDATE,
	RECORD_STATUS_ACCEPTED,
	RECORD_STATUS_FAILED,
	RECORD_VISIT_CAPTURE,
	SESSION_CANCELLED,
	SESSION_CLOSED,
	VISIT_DOCTYPE,
	VISIT_STATUS_AWAITING_PHARMACY,
	VISIT_STATUS_DISCHARGED,
)
from onerc_healthcare.outreach import sync
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import LAB_TEST, as_user
from onerc_healthcare.tests.factories import get_facility, make_patient
from onerc_healthcare.tests.outreach_factories import make_catchment, make_session, outreach_users
from onerc_healthcare.tests.visit_factories import get_triage_level

MRDT = "Malaria Rapid Diagnostic Test"
MRDT_POSITIVE = "Positive - P. falciparum"
PARACETAMOL = "Paracetamol 500 mg Tablet"
AMOXICILLIN = "Amoxicillin 500 mg Capsule"
MALARIA = "1F40.Z-ICD-11"
PNEUMONIA = "CA40.Z-ICD-11"
DEVICE = "t-device-o2-0001"
FMT = "%Y-%m-%d %H:%M:%S"


def new_uuid() -> str:
	return str(uuid.uuid4())


def random_name() -> str:
	"""A name with a random first letter, so phonetic keys (Soundex) of test patients rarely collide."""
	return random.choice(string.ascii_uppercase) + "".join(random.choices(string.ascii_lowercase, k=9))


def stamp(value: datetime) -> str:
	return value.strftime(FMT)


class SyncCase(EMRTestCase):
	"""A Planned session (started 3 days ago, Outpatient and MCH offered) with two members."""

	def setUp(self):
		super().setUp()
		self.users = outreach_users()
		self.area, self.leaf = make_catchment()
		self.session = make_session(
			members=[self.users["member"], self.users["member2"]],
			catchment_area=self.area,
			departments=("Outpatient", "Maternal and Child Health"),
			start_date=add_days(today(), -3),
			days=10,
		).name
		self.facility = frappe.db.get_value(OUTREACH_SESSION_DOCTYPE, self.session, "facility")
		# Two hours ago, at a whole second (the wire format has no fractions).
		self.captured = (now_datetime() - timedelta(hours=2)).replace(microsecond=0)

	# ------------------------------------------------------------------------------ envelopes

	def env(self, record_type, payload, captured=None, depends_on=(), client_uuid=None, **kw) -> dict:
		return {
			"client_uuid": client_uuid or new_uuid(),
			"type": record_type,
			"session": kw.get("session", self.session),
			"device_id": kw.get("device_id", DEVICE),
			"captured_at": kw.get("captured_at") or stamp(captured or self.captured),
			"depends_on": list(depends_on),
			"payload": payload,
		}

	def register_payload(self, **kw) -> dict:
		payload = {
			"first_name": random_name(),
			"middle_name": None,
			"last_name": f"T-{random_name()}",
			"sex": "Female",
			"dob": None,
			"estimated_age_years": random.randint(18, 80),
			"estimated_age_months": None,
			"mobile": None,
			"residence": self.leaf,
			"allergies": None,
			"is_walk_in": 0,
			"identifiers": [
				{"identifier_type": IDENTIFIER_UNHCR, "identifier_value": f"T-UN-{random_string(10)}"}
			],
		}
		payload.update(kw)
		return payload

	def visit_payload(
		self,
		patient=None,
		patient_ref=None,
		department="Outpatient",
		vitals=True,
		dx=((MALARIA, "New", 1),),
		labs=(),
		drugs=(),
		outcome=None,
		no_diagnosis_reason=None,
	) -> dict:
		return {
			"patient": patient,
			"patient_ref": patient_ref,
			"department": department,
			"vitals": {
				"triage_level": get_triage_level(),
				"chief_complaint": "T- fever for two days",
				"temperature": 38.4,
				"pulse": 96,
				"respiratory_rate": 20,
				"bp_systolic": 118,
				"bp_diastolic": 76,
				"weight": 61.5,
				"height": 1.64,
				"spo2": 98,
			}
			if vitals
			else None,
			"consultation": {
				"presenting_complaint": "T- fever and headache",
				"history": "T- two days of fever",
				"examination_findings": "T- warm, alert",
				"management_plan": "T- treat and review",
				"no_diagnosis_reason": no_diagnosis_reason,
				"diagnoses": [{"code": c, "case_type": t, "is_primary": p} for c, t, p in dx],
			},
			"lab_requests": list(labs),
			"prescriptions": list(drugs),
			"outcome": outcome or OUTCOME_DISCHARGED,
		}

	def drug(self, medication=PARACETAMOL, dosage="BID", period="5 Day", dispensed=None) -> dict:
		return {
			"line_id": random_string(4),
			"medication": medication,
			"dosage": dosage,
			"period": period,
			"dosage_form": frappe.db.get_value("Medication", medication, "dosage_form"),
			"comment": None,
			"dispensed": dispensed,
		}

	# ------------------------------------------------------------------------------ calls

	def push(self, records, user="member", batch_id=None, session=None, device_id=DEVICE) -> dict:
		with as_user(self.users[user] if user in self.users else user):
			return sync.push(session or self.session, device_id, batch_id or new_uuid(), records)

	def results(self, response) -> list[str]:
		return [r["result"] for r in response["results"]]

	def record(self, client_uuid):
		return frappe.get_doc(OUTREACH_RECORD_DOCTYPE, client_uuid)

	def register(self, **kw) -> tuple[dict, dict]:
		"""Sync one patient.register; returns (envelope, result)."""
		env = self.env(RECORD_PATIENT_REGISTER, self.register_payload(**kw))
		return env, self.push([env])["results"][0]


class TestPushAccess(SyncCase):
	def test_member_registers_patient(self):
		"""FR-24: a team member's offline registration becomes a Patient: owner = the member, registration facility =
		the session facility, age 30 estimated as of the capture date (dob = capture date - 30 years)."""
		env, result = self.register(estimated_age_years=30)
		self.assertEqual(result["result"], "accepted", result)
		self.assertEqual(result["doctype"], "Patient")
		patient = frappe.get_doc("Patient", result["name"])
		self.assertEqual(patient.owner, self.users["member"])
		self.assertEqual(patient.krcs_registration_facility, self.facility)
		self.assertEqual(patient.krcs_dob_estimated, 1)
		self.assertEqual(getdate(patient.krcs_dob_estimated_on), self.captured.date())
		self.assertEqual(getdate(patient.dob), getdate(add_years(self.captured.date(), -30)))
		rec = self.record(env["client_uuid"])
		self.assertEqual(
			(rec.status, rec.target_name, rec.user),
			(RECORD_STATUS_ACCEPTED, patient.name, self.users["member"]),
		)

	def test_outsider_receptionist_and_his_not_on_team_refused(self):
		"""Only a team member may sync into the session (outsider holds the outreach role but is not on the team)."""
		env = self.env(RECORD_PATIENT_REGISTER, self.register_payload())
		for user in ("outsider", "reception", "his", "clinician"):
			with self.assertRaises(frappe.PermissionError, msg=user):
				self.push([env], user=user)
		self.assertFalse(frappe.db.exists(OUTREACH_RECORD_DOCTYPE, env["client_uuid"]))
		self.assertEqual(self.push([env], user="member2")["results"][0]["result"], "accepted")

	def test_closed_or_cancelled_session_refused(self):
		env = self.env(RECORD_PATIENT_REGISTER, self.register_payload())
		for status in (SESSION_CLOSED, SESSION_CANCELLED):
			frappe.db.set_value(OUTREACH_SESSION_DOCTYPE, self.session, "status", status)
			with self.assertRaises(frappe.ValidationError, msg=status):
				self.push([env])
		self.assertFalse(frappe.db.exists(OUTREACH_RECORD_DOCTYPE, env["client_uuid"]))

	def test_batch_too_large_and_bad_batch_id(self):
		"""max_batch (default 50): 51 records are refused before anything is stored."""
		envs = [self.env(RECORD_PATIENT_REGISTER, self.register_payload()) for _ in range(51)]
		with self.assertRaises(frappe.ValidationError):
			self.push(envs)
		with self.assertRaises(frappe.ValidationError):
			self.push(envs[:1], batch_id="not-a-uuid")
		self.assertFalse(frappe.db.exists(OUTREACH_RECORD_DOCTYPE, envs[0]["client_uuid"]))


class TestEnvelope(SyncCase):
	def test_captured_at_bounds(self):
		"""captured_at: > now + 10 min refused; now + 5 min allowed; before the session start date refused; a
		badly formatted value refused. Failed records are stored (except unattributable ones)."""
		now = now_datetime().replace(microsecond=0)
		start = getdate(frappe.db.get_value(OUTREACH_SESSION_DOCTYPE, self.session, "start_date"))
		future = self.env(
			RECORD_PATIENT_REGISTER, self.register_payload(), captured=now + timedelta(minutes=11)
		)
		skew = self.env(RECORD_PATIENT_REGISTER, self.register_payload(), captured=now + timedelta(minutes=5))
		before = self.env(
			RECORD_PATIENT_REGISTER,
			self.register_payload(),
			captured=datetime.combine(start - timedelta(days=1), datetime.min.time()) + timedelta(hours=23),
		)
		on_start = self.env(
			RECORD_PATIENT_REGISTER,
			self.register_payload(),
			captured=datetime.combine(start, datetime.min.time()) + timedelta(hours=7),
		)
		bad = self.env(RECORD_PATIENT_REGISTER, self.register_payload(), captured_at="20/11/2026 10:42")
		out = self.push([future, skew, before, on_start, bad])
		self.assertEqual(self.results(out), ["failed", "accepted", "failed", "accepted", "failed"])
		self.assertIn("future", out["results"][0]["error"])
		self.assertIn("before the outreach session started", out["results"][2]["error"])
		self.assertIn("YYYY-MM-DD HH:mm:ss", out["results"][4]["error"])
		self.assertEqual(self.record(future["client_uuid"]).status, RECORD_STATUS_FAILED)

	def test_unattributable_records_not_stored(self):
		"""Bad UUID, unknown type, another session or device: failed and not stored."""
		other = make_session(members=[self.users["member"]], catchment_area=self.area).name
		bad_uuid = self.env(RECORD_PATIENT_REGISTER, self.register_payload(), client_uuid="1234")
		unknown = self.env("patient.delete", {})
		wrong_session = self.env(RECORD_PATIENT_REGISTER, self.register_payload(), session=other)
		wrong_device = self.env(RECORD_PATIENT_REGISTER, self.register_payload(), device_id="another")
		out = self.push([bad_uuid, unknown, wrong_session, wrong_device, "not a record"])
		self.assertEqual(self.results(out), ["failed"] * 5)
		for env in (unknown, wrong_session, wrong_device):
			self.assertFalse(frappe.db.exists(OUTREACH_RECORD_DOCTYPE, env["client_uuid"]))
		self.assertEqual(out["results"][0]["client_uuid"], "1234")

	def test_failed_record_does_not_block_others(self):
		"""[good, bad (no DOB or age), good]: 2 Patients, 1 failed with a plain-text message; the batch counts
		received 3, accepted 2, failed 1."""
		bad_payload = self.register_payload(estimated_age_years=None)
		envs = [
			self.env(RECORD_PATIENT_REGISTER, self.register_payload()),
			self.env(RECORD_PATIENT_REGISTER, bad_payload),
			self.env(RECORD_PATIENT_REGISTER, self.register_payload()),
		]
		out = self.push(envs)
		self.assertEqual(self.results(out), ["accepted", "failed", "accepted"])
		error = out["results"][1]["error"]
		self.assertIn("date of birth", error)
		self.assertNotIn("<", error)
		self.assertFalse(frappe.db.exists("Patient", {"first_name": bad_payload["first_name"]}))
		batch = frappe.get_doc(OUTREACH_BATCH_DOCTYPE, out["batch"])
		self.assertEqual(
			(batch.received, batch.accepted, batch.failed, batch.duplicate, batch.calls), (3, 2, 1, 0, 1)
		)

	def test_dependency_not_received_then_received(self):
		"""A visit whose patient.register has not arrived fails ("dependency not received"); after the registration
		syncs, the same visit re-sent is applied (Failed is retried)."""
		reg = self.env(RECORD_PATIENT_REGISTER, self.register_payload())
		visit = self.env(
			RECORD_VISIT_CAPTURE,
			self.visit_payload(patient_ref=reg["client_uuid"]),
			depends_on=[reg["client_uuid"]],
		)
		first = self.push([visit])["results"][0]
		self.assertEqual(first["result"], "failed")
		self.assertIn("Dependency not received", first["error"])
		out = self.push([reg, visit])
		self.assertEqual(self.results(out), ["accepted", "accepted"])
		self.assertEqual(
			frappe.db.get_value(VISIT_DOCTYPE, out["results"][1]["name"], "patient"),
			out["results"][0]["name"],
		)
		self.assertEqual(self.record(visit["client_uuid"]).attempts, 2)

	def test_mental_health_and_unoffered_department_refused(self):
		"""Mental Health is never offered offline; Nutrition is not offered by this session."""
		patient = make_patient(krcs_residence=self.leaf).name
		mh = self.env(RECORD_VISIT_CAPTURE, self.visit_payload(patient=patient, department="Mental Health"))
		nutrition = self.env(
			RECORD_VISIT_CAPTURE, self.visit_payload(patient=patient, department="Nutrition")
		)
		out = self.push([mh, nutrition])
		self.assertEqual(self.results(out), ["failed", "failed"])
		self.assertIn("never offered", out["results"][0]["error"])
		self.assertIn("not offered", out["results"][1]["error"])
		self.assertFalse(frappe.db.exists(VISIT_DOCTYPE, {"patient": patient}))

	def test_status_endpoint(self):
		"""status: the user's own records by UUID; unknown and other users' records come back as not received."""
		env, _result = self.register()
		other_env = self.env(RECORD_PATIENT_REGISTER, self.register_payload())
		self.push([other_env], user="member2")
		missing = new_uuid()
		with as_user(self.users["member"]):
			out = sync.status(self.session, [env["client_uuid"], other_env["client_uuid"], missing])[
				"results"
			]
		self.assertEqual([r["result"] for r in out], ["accepted", None, None])
		with as_user(self.users["outsider"]), self.assertRaises(frappe.PermissionError):
			sync.status(self.session, [env["client_uuid"]])

	def test_community_activity(self):
		"""community.activity: a submitted Community Activity Record at the session facility led by the member."""
		env = self.env(
			RECORD_COMMUNITY_ACTIVITY,
			{
				"activity_type": "Health Talk",
				"activity_date": str(self.captured.date()),
				"location": "T- Village 3 market",
				"topic": "T- malaria prevention",
				"participants_male": 12,
				"participants_female": 25,
				"participants_total": 37,
				"notes": None,
			},
		)
		result = self.push([env])["results"][0]
		self.assertEqual(result["result"], "accepted", result)
		doc = frappe.get_doc(COMMUNITY_ACTIVITY_DOCTYPE, result["name"])
		self.assertEqual(
			(doc.docstatus, doc.facility, doc.lead_staff, doc.owner, doc.participants_total),
			(1, self.facility, self.users["member"], self.users["member"], 37),
		)

	def test_patient_update_applies_when_server_unchanged(self):
		"""TST-09 (non-conflicting): base_modified equals the server version -> the changes are applied."""
		patient = make_patient(krcs_residence=self.leaf)
		env = self.env(
			RECORD_PATIENT_UPDATE,
			{
				"patient": patient.name,
				"base_modified": str(patient.modified),
				"changes": {"mobile": "0712000111"},
				"add_identifiers": [],
			},
		)
		result = self.push([env])["results"][0]
		self.assertEqual(result["result"], "accepted", result)
		self.assertEqual(frappe.db.get_value("Patient", patient.name, "mobile"), "0712000111")
		bad = self.env(
			RECORD_PATIENT_UPDATE,
			{
				"patient": patient.name,
				"base_modified": str(patient.modified),
				"changes": {"krcs_merged_into": "X"},
			},
		)
		self.assertIn("cannot be changed offline", self.push([bad])["results"][0]["error"])


class TestOfflineCapture(SyncCase):
	"""TST-05: offline capture then sync."""

	def test_register_and_full_visit(self):
		"""Register + one visit: vitals, 2 diagnoses, MRDT at the point of care (positive), a Fasting Blood Sugar
		sent to the facility lab, 2 prescriptions (Paracetamol dispensed 4, Amoxicillin not), Discharged home.

		Expected (by hand):
		- Patient, Visit, Vital Signs, Encounter, Dispensing Record owned by the member; Encounter practitioner is the
		  member's Healthcare Practitioner;
		- visit date / arrival / triage, routing and consultation start = captured_at; vitals and encounter dated
		  the capture day; MRDT approved at resulted_at; dispense at dispensed_at;
		- lab_open (FBS pending) and pharmacy_open (Amoxicillin) -> status Awaiting Pharmacy; outcome Discharged home;
		  the completion carries the outreach lab-pending reason;
		- 2 Service Requests (MRDT Observation submitted, FBS sample still Open), 2 Medication Requests, 1 Dispensing
		  Record of 4."""
		captured = self.captured - timedelta(hours=20)  # yesterday's field day
		resulted = captured + timedelta(minutes=13)
		dispensed = captured + timedelta(minutes=23)
		reg = self.env(
			RECORD_PATIENT_REGISTER, self.register_payload(), captured=captured - timedelta(minutes=5)
		)
		payload = self.visit_payload(
			patient_ref=reg["client_uuid"],
			dx=((MALARIA, "New", 1), (PNEUMONIA, "New", 0)),
			labs=(
				{
					"line_id": "l1",
					"observation_template": MRDT,
					"poc_result": {"result": MRDT_POSITIVE, "resulted_at": stamp(resulted)},
				},
				{"line_id": "l2", "observation_template": LAB_TEST},
			),
			drugs=(
				self.drug(PARACETAMOL, dispensed={"quantity": 4, "dispensed_at": stamp(dispensed)}),
				self.drug(AMOXICILLIN, dosage="TID"),
			),
		)
		visit_env = self.env(
			RECORD_VISIT_CAPTURE, payload, captured=captured, depends_on=[reg["client_uuid"]]
		)
		out = self.push([reg, visit_env])
		self.assertEqual(self.results(out), ["accepted", "accepted"], out)
		member = self.users["member"]
		patient = out["results"][0]["name"]
		visit = frappe.get_doc(VISIT_DOCTYPE, out["results"][1]["name"])

		self.assertEqual(frappe.db.get_value("Patient", patient, "owner"), member)
		self.assertEqual((visit.patient, visit.owner, visit.is_outreach), (patient, member, 1))
		self.assertEqual((visit.outreach_session, visit.facility), (self.session, self.facility))
		self.assertEqual(getdate(visit.visit_date), captured.date())
		self.assertEqual(get_datetime(visit.arrived_at), captured)
		for field in ("triage_started_at", "routed_at", "consultation_started_at"):
			self.assertEqual(get_datetime(visit.get(field)), captured, field)
		self.assertEqual(visit.status, VISIT_STATUS_AWAITING_PHARMACY)
		self.assertEqual((visit.lab_open, visit.pharmacy_open), (1, 1))
		self.assertEqual(visit.outcome, OUTCOME_DISCHARGED)
		self.assertEqual(get_datetime(visit.outcome_at), dispensed)
		self.assertEqual(visit.current_department, "Outpatient")
		self.assertEqual({row.user for row in visit.status_log}, {member})
		complete = [r for r in visit.status_log if r.action == "complete_consultation"]
		self.assertEqual(complete[0].comment, OUTREACH_LAB_PENDING_REASON)

		vitals = frappe.get_doc("Vital Signs", visit.triage_vital_signs)
		self.assertEqual((vitals.docstatus, vitals.owner, vitals.krcs_visit), (1, member, visit.name))
		self.assertEqual(getdate(vitals.signs_date), captured.date())
		self.assertEqual((vitals.temperature, vitals.pulse, vitals.krcs_spo2), ("38.4", "96", "98"))

		enc = frappe.get_doc(ENCOUNTER_DOCTYPE, visit.encounter)
		self.assertEqual((enc.docstatus, enc.owner, enc.krcs_visit), (1, member, visit.name))
		self.assertEqual(getdate(enc.encounter_date), captured.date())
		self.assertEqual(frappe.db.get_value("Healthcare Practitioner", enc.practitioner, "user_id"), member)
		self.assertEqual(
			[(d.code, d.case_type, d.is_primary) for d in enc.krcs_diagnoses],
			[(MALARIA, "New", 1), (PNEUMONIA, "New", 0)],
		)

		orders = {
			r.template_dn: r.name
			for r in frappe.get_all(
				"Service Request",
				filters={"order_group": enc.name, "docstatus": 1},
				fields=["name", "template_dn"],
			)
		}
		self.assertEqual(set(orders), {MRDT, LAB_TEST})
		mrdt = frappe.get_all(
			"Observation",
			filters={"service_request": orders[MRDT], "docstatus": 1},
			fields=["name", "result", "time_of_approval", "status"],
		)
		self.assertEqual(len(mrdt), 1)
		self.assertEqual((mrdt[0].result, mrdt[0].status), (MRDT_POSITIVE, "Approved"))
		self.assertEqual(get_datetime(mrdt[0].time_of_approval), resulted)
		self.assertEqual(
			frappe.db.get_value("Observation Sample Collection", {"service_request": orders[MRDT]}, "status"),
			"Collected",
		)
		self.assertFalse(
			frappe.db.exists("Observation", {"service_request": orders[LAB_TEST], "docstatus": 1})
		)
		self.assertEqual(
			frappe.db.get_value(
				"Observation Sample Collection", {"service_request": orders[LAB_TEST]}, "status"
			),
			"Open",
		)

		lines = {
			r.medication: r.name
			for r in frappe.get_all(
				"Medication Request",
				filters={"order_group": enc.name, "docstatus": 1},
				fields=["name", "medication"],
			)
		}
		self.assertEqual(set(lines), {PARACETAMOL, AMOXICILLIN})
		records = frappe.get_all(
			DISPENSING_DOCTYPE,
			filters={"encounter": enc.name},
			fields=[
				"name",
				"docstatus",
				"medication_request",
				"quantity",
				"dispensed_by",
				"dispensed_at",
				"owner",
			],
		)
		self.assertEqual(len(records), 1)
		self.assertEqual(
			(records[0].docstatus, records[0].medication_request, records[0].quantity),
			(1, lines[PARACETAMOL], 4),
		)
		self.assertEqual((records[0].dispensed_by, records[0].owner), (member, member))
		self.assertEqual(get_datetime(records[0].dispensed_at), dispensed)

		rec = self.record(visit_env["client_uuid"])
		self.assertEqual(
			(rec.status, rec.target_doctype, rec.target_name, rec.patient),
			(RECORD_STATUS_ACCEPTED, VISIT_DOCTYPE, visit.name, patient),
		)

	def test_visit_without_vitals_or_labs_discharged(self):
		"""No vitals: routed with the outreach no-vitals reason; no lab, nothing prescribed -> Discharged."""
		patient = make_patient(krcs_residence=self.leaf).name
		env = self.env(RECORD_VISIT_CAPTURE, self.visit_payload(patient=patient, vitals=False))
		result = self.push([env])["results"][0]
		self.assertEqual(result["result"], "accepted", result)
		visit = frappe.get_doc(VISIT_DOCTYPE, result["name"])
		self.assertEqual((visit.status, visit.routed_without_vitals), (VISIT_STATUS_DISCHARGED, 1))
		route = next(r for r in visit.status_log if r.action == "route")
		self.assertIn(OUTREACH_NO_VITALS_REASON, route.comment)
		self.assertEqual(get_datetime(visit.closed_at), self.captured)

	def test_invalid_poc_and_non_poc_result_refused(self):
		"""A POC result for a test that is not point-of-care, or a result outside the options: failed, no visit."""
		patient = make_patient(krcs_residence=self.leaf).name
		not_poc = self.visit_payload(
			patient=patient,
			labs=(
				{
					"observation_template": LAB_TEST,
					"poc_result": {"result": "5.1", "resulted_at": stamp(self.captured)},
				},
			),
		)
		bad_value = self.visit_payload(
			patient=patient,
			labs=(
				{
					"observation_template": MRDT,
					"poc_result": {"result": "Maybe", "resulted_at": stamp(self.captured)},
				},
			),
		)
		out = self.push([self.env(RECORD_VISIT_CAPTURE, not_poc), self.env(RECORD_VISIT_CAPTURE, bad_value)])
		self.assertEqual(self.results(out), ["failed", "failed"])
		self.assertIn("not a point-of-care test", out["results"][0]["error"])
		self.assertIn("Choose one of", out["results"][1]["error"])
		self.assertFalse(frappe.db.exists(VISIT_DOCTYPE, {"patient": patient}))


class TestResumeAndIdempotency(SyncCase):
	def test_interrupted_sync_resumes(self):
		"""TST-06 / OUT-16: 4 registrations; the first call carries 2 and is acknowledged; the connection drops.
		The device then re-sends the 2 it has no answer for, plus (being cautious) one acknowledged one.
		Expected: call 2 -> [duplicate, accepted, accepted]; 4 Patients in all, 4 Sync Records."""
		envs = [self.env(RECORD_PATIENT_REGISTER, self.register_payload()) for _ in range(4)]
		first = self.push(envs[:2])
		self.assertEqual(self.results(first), ["accepted", "accepted"])
		second = self.push([envs[1], envs[2], envs[3]])
		self.assertEqual(self.results(second), ["duplicate", "accepted", "accepted"])
		self.assertEqual(second["results"][0]["name"], first["results"][1]["name"])
		names = {r["name"] for r in first["results"] + second["results"]}
		self.assertEqual(len(names), 4)
		self.assertEqual(
			frappe.db.count(OUTREACH_RECORD_DOCTYPE, {"name": ["in", [e["client_uuid"] for e in envs]]}), 4
		)

	def test_repeated_sends_create_no_duplicates(self):
		"""TST-07: the same batch re-sent (same batch_id), the same records in a new batch, and a record re-sent
		after it failed. Expected: one Patient per registration, one visit, one batch row for the re-sent batch_id
		(calls 2), the failed record accepted once corrected and duplicate afterwards."""
		patient = make_patient(krcs_residence=self.leaf).name
		reg = self.env(RECORD_PATIENT_REGISTER, self.register_payload())
		visit = self.env(RECORD_VISIT_CAPTURE, self.visit_payload(patient=patient))
		batch_id = new_uuid()
		first = self.push([reg, visit], batch_id=batch_id)
		again = self.push([reg, visit], batch_id=batch_id)
		self.assertEqual(self.results(first), ["accepted", "accepted"])
		self.assertEqual(self.results(again), ["duplicate", "duplicate"])
		self.assertEqual(first["batch"], again["batch"])
		batch = frappe.get_doc(OUTREACH_BATCH_DOCTYPE, first["batch"])
		self.assertEqual((batch.calls, batch.received, batch.duplicate, batch.accepted), (2, 2, 2, 0))
		third = self.push([visit, reg])
		self.assertEqual(self.results(third), ["duplicate", "duplicate"])
		self.assertNotEqual(third["batch"], first["batch"])
		self.assertEqual(frappe.db.count("Patient", {"first_name": reg["payload"]["first_name"]}), 1)
		self.assertEqual(frappe.db.count(VISIT_DOCTYPE, {"patient": patient}), 1)
		self.assertEqual(self.record(reg["client_uuid"]).attempts, 3)

		# After a failure: the corrected record (same UUID) is retried, then answered as a duplicate.
		bad = self.env(RECORD_PATIENT_REGISTER, self.register_payload(estimated_age_years=None))
		self.assertEqual(self.push([bad])["results"][0]["result"], "failed")
		self.assertEqual(self.push([bad])["results"][0]["result"], "failed")  # unchanged: fails again
		bad["payload"]["estimated_age_years"] = 4
		fixed = self.push([bad])["results"][0]
		self.assertEqual(fixed["result"], "accepted", fixed)
		self.assertEqual(self.push([bad])["results"][0], {**fixed, "result": "duplicate"})
		self.assertEqual(frappe.db.count("Patient", {"first_name": bad["payload"]["first_name"]}), 1)
		self.assertEqual(self.record(bad["client_uuid"]).attempts, 4)

	def test_record_of_another_member_not_replayed(self):
		"""A UUID already received from member is not applied again for member2 (no second visit)."""
		env, first = self.register()
		other = self.push([env], user="member2")["results"][0]
		self.assertEqual(other["result"], "failed")
		self.assertEqual(frappe.db.count("Patient", {"first_name": env["payload"]["first_name"]}), 1)
		self.assertEqual(first["result"], "accepted")


class TestLargeBatch(SyncCase):
	def test_three_batches_of_fifty(self):
		"""TST-11: 150 records in 3 calls of 50 (the maximum): 50 registrations, 50 visits that depend on them,
		50 community activities. Expected: 150 accepted, 50 Patients, 50 outreach visits of the session, 50
		activities; each call well under a minute on the test bench (timings printed)."""
		regs = [
			self.env(RECORD_PATIENT_REGISTER, self.register_payload(estimated_age_years=5 + i))
			for i in range(50)
		]
		visits = [
			self.env(
				RECORD_VISIT_CAPTURE,
				self.visit_payload(patient_ref=r["client_uuid"]),
				depends_on=[r["client_uuid"]],
			)
			for r in regs
		]
		acts = [
			self.env(
				RECORD_COMMUNITY_ACTIVITY,
				{
					"activity_type": "Home Visit",
					"location": f"T- home {i}",
					"participants_female": 2,
					"participants_total": 2,
				},
			)
			for i in range(50)
		]
		timings = []
		for batch in (regs, visits, acts):
			start = time.monotonic()
			out = self.push(batch)
			timings.append(round(time.monotonic() - start, 2))
			self.assertEqual(
				self.results(out),
				["accepted"] * 50,
				[r for r in out["results"] if r["result"] != "accepted"][:3],
			)
		print(f"\nTST-11 outreach sync, 3 x 50 records: {timings} s (registrations, visits, activities)")
		self.assertEqual(frappe.db.count(VISIT_DOCTYPE, {"outreach_session": self.session}), 50)
		self.assertEqual(
			frappe.db.count(
				COMMUNITY_ACTIVITY_DOCTYPE,
				{"location": ["like", "T- home %"], "lead_staff": self.users["member"]},
			),
			50,
		)
		for seconds in timings:
			self.assertLess(seconds, 60)


class TestOutreachInReports(SyncCase):
	"""An outreach visit counts in MOH 705 (POC MRDT confirms malaria) and on the 717 outreach line."""

	FROM, TO = date(2002, 3, 1), date(2002, 3, 31)

	def setUp(self):
		super().setUp()
		# A quiet past month at the session facility (705 tests use 2019, 717 tests 2001).
		frappe.db.set_value(
			OUTREACH_SESSION_DOCTYPE, self.session, {"start_date": self.FROM, "end_date": self.TO}
		)
		self.captured = datetime(2002, 3, 15, 10, 0, 0)

	def test_poc_malaria_confirmed_and_outreach_attendance(self):
		"""Adult (30, estimated), 1F40.Z New with a positive POC MRDT approved, one Paracetamol dispensed in full.
		705B: Confirmed Malaria 1, Clinical Malaria 0. 717: outreach-only attendance New 1; facility attendance
		(outreach excluded) 0."""
		from onerc_healthcare.reporting import diagnosis, registry, workload

		reg = self.env(
			RECORD_PATIENT_REGISTER, self.register_payload(estimated_age_years=30), captured=self.captured
		)
		resulted = stamp(self.captured + timedelta(minutes=15))
		visit = self.env(
			RECORD_VISIT_CAPTURE,
			self.visit_payload(
				patient_ref=reg["client_uuid"],
				labs=(
					{
						"observation_template": MRDT,
						"poc_result": {"result": MRDT_POSITIVE, "resulted_at": resulted},
					},
				),
				drugs=(self.drug(PARACETAMOL, dispensed={"quantity": 1, "dispensed_at": resulted}),),
			),
			captured=self.captured + timedelta(minutes=5),
			depends_on=[reg["client_uuid"]],
		)
		out = self.push([reg, visit])
		self.assertEqual(self.results(out), ["accepted", "accepted"], out)
		facility = get_facility(0)
		self.assertEqual(self.facility, facility)

		def el(category):
			return registry.ElementSpec(
				f"B-{category}", category, "Diagnosis Category", {"category": category, "age_min_months": 60}
			)

		ctx = registry.SourceContext(report="T-O2", from_date=self.FROM, to_date=self.TO, facility=facility)
		cells = diagnosis.evaluate(ctx, [el("Confirmed Malaria"), el("Clinical Malaria")])
		got = {key[0]: value for key, value in cells.items() if value}
		self.assertEqual(got, {"B-Confirmed Malaria": 1})

		elements = [
			registry.ElementSpec(
				"OUT-N", "OUT-N", "Visit Attendance", {"attendance_type": "New", "outreach": "only"}
			),
			registry.ElementSpec("FAC-N", "FAC-N", "Visit Attendance", {"attendance_type": "New"}),
		]
		ctx = registry.SourceContext(report="T-O2", from_date=self.FROM, to_date=self.TO, facility=facility)
		got = {k: v for k, v in workload.evaluate_attendance(ctx, elements).items() if v}
		self.assertEqual(got, {("OUT-N", "", ""): 1})
