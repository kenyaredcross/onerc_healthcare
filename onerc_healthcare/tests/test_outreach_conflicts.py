# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Sync Conflicts (Phase 4, O2): raising, marking, resolving and replaying (FR-26, OUT-19..23, TST-08/09;
OI-11 / D58). Never commit.

Records are synced as the team member through `outreach.sync.push`; conflicts are resolved through
`outreach.conflicts.resolve` / `retry` as the HIS Officer or the Medical In-Charge. Expectations are worked out by
hand in each docstring."""

from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.utils import add_years, getdate, now_datetime

from onerc_healthcare.constants import (
	ACCESS_LOG_DOCTYPE,
	CONFLICT_DECISIONS,
	CONFLICT_DEMOGRAPHIC_CHANGE,
	CONFLICT_DUPLICATE_REGISTRATION,
	CONFLICT_OPEN,
	CONFLICT_OVERLAPPING_VISIT,
	CONFLICT_RESOLVED,
	CONFLICT_TYPES,
	DECISION_ACKNOWLEDGE,
	DECISION_KEEP_FACILITY,
	DECISION_KEEP_OUTREACH,
	DECISION_LINK,
	DECISION_MERGE,
	IDENTIFIER_UNHCR,
	OUTREACH_RECORD_DOCTYPE,
	PATIENT_OPEN_CONFLICTS_FIELD,
	RECORD_PATIENT_REGISTER,
	RECORD_PATIENT_UPDATE,
	RECORD_STATUS_ACCEPTED,
	RECORD_STATUS_CONFLICT,
	RECORD_STATUS_DISCARDED,
	RECORD_STATUS_HELD,
	RECORD_VISIT_CAPTURE,
	SYNC_CONFLICT_DOCTYPE,
	VISIT_DOCTYPE,
)
from onerc_healthcare.emr_outreach.doctype.sync_conflict.sync_conflict import SyncConflictLockedError
from onerc_healthcare.outreach import conflicts, sync
from onerc_healthcare.tests.clinical_factories import as_user
from onerc_healthcare.tests.factories import make_patient
from onerc_healthcare.tests.test_outreach_sync import SyncCase, random_name
from onerc_healthcare.tests.visit_factories import make_visit
from onerc_healthcare.visit import engine

RATION = "Ration Card Number"


class ConflictCase(SyncCase):
	def resolve(self, conflict, decision, reason="T- checked with the patient", user="his", **kw) -> dict:
		with as_user(self.users[user]):
			return conflicts.resolve(conflict, decision, reason, **kw)

	def count(self, patient) -> int:
		return frappe.db.get_value("Patient", patient, PATIENT_OPEN_CONFLICTS_FIELD)

	def existing(self, **kw):
		values = {
			"first_name": random_name(),
			"last_name": f"T-{random_name()}",
			"sex": "Female",
			"dob": "1990-05-01",
			"krcs_residence": self.leaf,
		}
		values.update(kw)
		return make_patient(**values)

	def duplicate_with_held_visit(self, payload: dict):
		"""Sync a registration that conflicts, plus a visit that depends on it. Returns (reg env, visit env, out)."""
		reg = self.env(RECORD_PATIENT_REGISTER, payload)
		visit = self.env(
			RECORD_VISIT_CAPTURE,
			self.visit_payload(patient_ref=reg["client_uuid"]),
			depends_on=[reg["client_uuid"]],
		)
		return reg, visit, self.push([reg, visit])


class TestDuplicateRegistration(ConflictCase):
	def test_identifier_conflict_keep_facility_replays_held_visit(self):
		"""TST-08 (identifier): the outreach registration carries the UNHCR ID of an existing patient.
		Expected: registration -> conflict, visit -> held on the same conflict; the existing patient is marked
		(open conflicts 1). Keep facility version: registration Discarded (answered as accepted + discarded with
		the existing patient), the held visit is replayed onto the existing patient as the member; count 0."""
		existing = self.existing()
		unhcr = frappe.get_all(
			"Patient Identifier", filters={"parent": existing.name}, pluck="identifier_value", limit=1
		)[0]
		payload = self.register_payload(
			identifiers=[{"identifier_type": IDENTIFIER_UNHCR, "identifier_value": unhcr}]
		)
		reg, visit, out = self.duplicate_with_held_visit(payload)
		self.assertEqual(self.results(out), ["conflict", "held"])
		name = out["results"][0]["conflict"]
		self.assertEqual(out["results"][1]["conflict"], name)
		doc = frappe.get_doc(SYNC_CONFLICT_DOCTYPE, name)
		self.assertEqual(
			(doc.conflict_type, doc.status, doc.patient, doc.raised_by, doc.session),
			(
				CONFLICT_DUPLICATE_REGISTRATION,
				CONFLICT_OPEN,
				existing.name,
				self.users["member"],
				self.session,
			),
		)
		candidates = frappe.parse_json(doc.candidates)
		self.assertEqual(candidates[0]["patient"], existing.name)
		self.assertIn("identifier", candidates[0]["matched_by"])
		self.assertEqual(self.count(existing.name), 1)
		self.assertEqual(self.record(visit["client_uuid"]).status, RECORD_STATUS_HELD)
		self.assertTrue(self.record(visit["client_uuid"]).payload)
		self.assertFalse(frappe.db.exists("Patient", {"first_name": payload["first_name"]}))
		# Re-sending while held: no change.
		self.assertEqual(self.results(self.push([reg, visit])), ["conflict", "held"])

		result = self.resolve(name, DECISION_KEEP_FACILITY)
		self.assertEqual(result["record_status"], RECORD_STATUS_DISCARDED)
		self.assertEqual([r["result"] for r in result["replayed"]], ["accepted"])
		rec = self.record(reg["client_uuid"])
		self.assertEqual((rec.status, rec.target_name), (RECORD_STATUS_DISCARDED, existing.name))
		self.assertTrue(rec.payload)  # kept
		new_visit = frappe.get_doc(VISIT_DOCTYPE, self.record(visit["client_uuid"]).target_name)
		self.assertEqual((new_visit.patient, new_visit.owner), (existing.name, self.users["member"]))
		self.assertEqual({r.user for r in new_visit.status_log}, {self.users["member"]})
		self.assertEqual(self.count(existing.name), 0)
		doc.reload()
		self.assertEqual(
			(doc.status, doc.decision, doc.resolved_by, doc.chosen_patient),
			(CONFLICT_RESOLVED, DECISION_KEEP_FACILITY, self.users["his"], existing.name),
		)
		self.assertEqual([r.action for r in doc.log], ["raise", "resolve", "replay"])
		self.assertEqual(doc.log[1].reason, "T- checked with the patient")
		with as_user(self.users["member"]):
			status = sync.status(self.session, [reg["client_uuid"], visit["client_uuid"]])["results"]
		self.assertEqual(
			[(s["result"], s.get("discarded")) for s in status], [("accepted", True), ("accepted", None)]
		)
		self.assertEqual(self.results(self.push([reg])), ["duplicate"])

	def test_similarity_conflict_link_adds_identifiers(self):
		"""TST-08 (similarity): same first name, last name one letter longer, same sex, DOB and residence -> score
		3 + 2 + 1 + 3 + 2 = 11 >= 8: conflict (not a strong duplicate: the names differ).
		Link as the same patient: the outreach Ration Card is added (a type the patient lacks); the outreach UNHCR
		ID is not (the patient already has one) and the result says so. Record Accepted as the existing patient."""
		existing = self.existing()
		payload = self.register_payload(
			first_name=existing.first_name,
			last_name=existing.last_name + "e",
			dob="1990-05-01",
			estimated_age_years=None,
			identifiers=[
				{"identifier_type": IDENTIFIER_UNHCR, "identifier_value": f"T-UN-{random_name()}"},
				{"identifier_type": RATION, "identifier_value": f"T-RC-{random_name()}"},
			],
		)
		_reg, visit, out = self.duplicate_with_held_visit(payload)
		self.assertEqual(self.results(out), ["conflict", "held"])
		doc = frappe.get_doc(SYNC_CONFLICT_DOCTYPE, out["results"][0]["conflict"])
		best = frappe.parse_json(doc.candidates)[0]
		self.assertEqual(
			(best["patient"], best["matched_by"], best["score"]), (existing.name, ["similarity"], 11)
		)

		result = self.resolve(doc.name, DECISION_LINK, user="mic")
		self.assertEqual((result["record_status"], result["name"]), (RECORD_STATUS_ACCEPTED, existing.name))
		rows = frappe.get_all(
			"Patient Identifier",
			filters={"parent": existing.name},
			fields=["identifier_type", "identifier_value"],
		)
		self.assertIn(
			(RATION, payload["identifiers"][1]["identifier_value"].upper()),
			{(r.identifier_type, r.identifier_value) for r in rows},
		)
		self.assertEqual(sum(r.identifier_type == IDENTIFIER_UNHCR for r in rows), 1)
		doc.reload()
		self.assertIn("Identifiers not added", doc.result_note)
		self.assertEqual(doc.resolved_by, self.users["mic"])
		self.assertEqual(
			frappe.db.get_value(VISIT_DOCTYPE, self.record(visit["client_uuid"]).target_name, "patient"),
			existing.name,
		)

	def test_similarity_conflict_merge_chosen_fields(self):
		"""Merge field by field: mobile taken from the outreach version, last name kept from the facility."""
		existing = self.existing()
		payload = self.register_payload(
			first_name=existing.first_name,
			last_name=existing.last_name + "e",
			dob="1990-05-01",
			estimated_age_years=None,
			mobile="0700111222",
		)
		reg = self.env(RECORD_PATIENT_REGISTER, payload)
		name = self.push([reg])["results"][0]["conflict"]
		with self.assertRaises(conflicts.SyncConflictError):
			self.resolve(name, DECISION_MERGE, chosen_fields={"last_name": "facility"})
		self.resolve(name, DECISION_MERGE, chosen_fields={"mobile": "outreach", "last_name": "facility"})
		row = frappe.db.get_value("Patient", existing.name, ["mobile", "last_name"], as_dict=True)
		self.assertEqual((row.mobile, row.last_name), ("0700111222", existing.last_name))
		self.assertEqual(self.record(reg["client_uuid"]).status, RECORD_STATUS_ACCEPTED)
		self.assertEqual(frappe.get_doc(SYNC_CONFLICT_DOCTYPE, name).chosen_patient, existing.name)

	def test_strong_duplicate_keep_outreach_registers_new_patient(self):
		"""Same name, DOB and sex (strong duplicate) -> conflict. Keep outreach version: a new patient whose
		duplicate-override reason names the conflict; the held visit goes to the new patient."""
		existing = self.existing()
		payload = self.register_payload(
			first_name=existing.first_name,
			last_name=existing.last_name,
			dob="1990-05-01",
			estimated_age_years=None,
		)
		_reg, visit, out = self.duplicate_with_held_visit(payload)
		name = out["results"][0]["conflict"]
		self.assertIn(
			"strong duplicate",
			frappe.parse_json(frappe.get_doc(SYNC_CONFLICT_DOCTYPE, name).candidates)[0]["matched_by"],
		)
		result = self.resolve(name, DECISION_KEEP_OUTREACH, reason="T- a different woman, same name")
		new = result["name"]
		self.assertNotEqual(new, existing.name)
		patient = frappe.get_doc("Patient", new)
		self.assertIn(name, patient.krcs_duplicate_override_reason)
		self.assertEqual(patient.owner, self.users["member"])
		self.assertEqual(
			frappe.db.get_value(VISIT_DOCTYPE, self.record(visit["client_uuid"]).target_name, "patient"), new
		)

	def test_decision_checks_and_roles(self):
		"""A decision outside the type's list, a patient that is not a candidate, an empty reason: refused.
		Team member, outsider, receptionist and clinician may not resolve; the HIS Officer may; a second decision on
		a resolved conflict is refused."""
		existing = self.existing()
		payload = self.register_payload(
			first_name=existing.first_name,
			last_name=existing.last_name,
			dob="1990-05-01",
			estimated_age_years=None,
		)
		name = self.push([self.env(RECORD_PATIENT_REGISTER, payload)])["results"][0]["conflict"]
		other = make_patient().name
		with self.assertRaises(conflicts.SyncConflictError):
			self.resolve(name, DECISION_ACKNOWLEDGE)
		with self.assertRaises(conflicts.SyncConflictError):
			self.resolve(name, DECISION_LINK, patient=other)
		with self.assertRaises(conflicts.SyncConflictError):
			self.resolve(name, DECISION_KEEP_FACILITY, reason="  ")
		for user in ("member", "outsider", "reception", "clinician"):
			with self.assertRaises(frappe.PermissionError, msg=user):
				self.resolve(name, DECISION_KEEP_FACILITY, user=user)
		self.assertEqual(frappe.db.get_value(SYNC_CONFLICT_DOCTYPE, name, "status"), CONFLICT_OPEN)
		self.resolve(name, DECISION_KEEP_FACILITY)
		with self.assertRaises(conflicts.SyncConflictError):
			self.resolve(name, DECISION_KEEP_OUTREACH, user="mic")


class TestDemographicChange(ConflictCase):
	def package(self, **kw):
		patient = self.existing(**kw)
		return patient, str(frappe.db.get_value("Patient", patient.name, "modified"))

	def update_env(self, patient, base, changes):
		return self.env(
			RECORD_PATIENT_UPDATE,
			{"patient": patient, "base_modified": base, "changes": changes, "add_identifiers": []},
		)

	def server_change(self, patient, **values):
		doc = frappe.get_doc("Patient", patient)
		doc.update(values)
		doc.save(ignore_permissions=True)

	def test_overlapping_edit_conflicts_and_resolutions(self):
		"""TST-09: after the package the facility changed the mobile to 0711. The device changes the mobile to 0722
		(differs: conflict), and in another record to 0711 (same as the server: applied, nothing differs).
		Keep outreach version -> 0722. A second conflicting change (mobile 0733) resolved Keep facility -> stays
		0722, record Discarded. A third (mobile 0744 + allergies) merged with only allergies from the outreach."""
		patient, base = self.package()
		self.server_change(patient.name, mobile="0711000000")
		conflicting = self.update_env(patient.name, base, {"mobile": "0722000000"})
		same = self.update_env(patient.name, base, {"mobile": "0711000000"})
		out = self.push([conflicting, same])
		self.assertEqual(self.results(out), ["conflict", "accepted"])
		name = out["results"][0]["conflict"]
		doc = frappe.get_doc(SYNC_CONFLICT_DOCTYPE, name)
		self.assertEqual((doc.conflict_type, doc.patient), (CONFLICT_DEMOGRAPHIC_CHANGE, patient.name))
		self.assertEqual(frappe.parse_json(doc.facility_version)["mobile"], "0711000000")
		self.assertEqual(frappe.parse_json(doc.outreach_version)["mobile"], "0722000000")
		self.assertEqual(self.count(patient.name), 1)
		self.resolve(name, DECISION_KEEP_OUTREACH)
		self.assertEqual(frappe.db.get_value("Patient", patient.name, "mobile"), "0722000000")
		self.assertEqual(self.count(patient.name), 0)

		second = self.update_env(patient.name, base, {"mobile": "0733000000"})
		name2 = self.push([second])["results"][0]["conflict"]
		self.resolve(name2, DECISION_KEEP_FACILITY)
		self.assertEqual(frappe.db.get_value("Patient", patient.name, "mobile"), "0722000000")
		self.assertEqual(self.record(second["client_uuid"]).status, RECORD_STATUS_DISCARDED)

		third = self.update_env(patient.name, base, {"mobile": "0744000000", "allergies": "T- penicillin"})
		name3 = self.push([third])["results"][0]["conflict"]
		self.resolve(name3, DECISION_MERGE, chosen_fields={"allergies": "outreach", "mobile": "facility"})
		row = frappe.db.get_value("Patient", patient.name, ["mobile", "allergies"], as_dict=True)
		self.assertEqual((row.mobile, row.allergies), ("0722000000", "T- penicillin"))


class TestOverlappingVisit(ConflictCase):
	def test_open_facility_visit_held_then_discarded(self):
		"""An open facility visit: the outreach visit is held in an Overlapping visit conflict (count 1). Keep
		facility version: Discarded with the facility visit as its target; no outreach visit is created."""
		patient = self.existing().name
		facility_visit = make_visit(patient=patient).name
		env = self.env(RECORD_VISIT_CAPTURE, self.visit_payload(patient=patient))
		result = self.push([env])["results"][0]
		self.assertEqual(result["result"], "conflict")
		doc = frappe.get_doc(SYNC_CONFLICT_DOCTYPE, result["conflict"])
		self.assertEqual(
			(doc.conflict_type, doc.review_only, doc.patient), (CONFLICT_OVERLAPPING_VISIT, 0, patient)
		)
		self.assertEqual(self.count(patient), 1)
		self.resolve(doc.name, DECISION_KEEP_FACILITY)
		rec = self.record(env["client_uuid"])
		self.assertEqual((rec.status, rec.target_name), (RECORD_STATUS_DISCARDED, facility_visit))
		self.assertEqual(frappe.db.count(VISIT_DOCTYPE, {"patient": patient}), 1)
		self.assertEqual(self.count(patient), 0)

	def test_retry_after_facility_visit_closed(self):
		"""Retry while the facility visit is open: refused, logged, conflict stays Open. After the facility visit
		is closed, Retry applies the outreach visit (as the member) and resolves the conflict (Keep outreach)."""
		patient = self.existing().name
		facility_visit = make_visit(patient=patient).name
		env = self.env(RECORD_VISIT_CAPTURE, self.visit_payload(patient=patient))
		name = self.push([env])["results"][0]["conflict"]
		with as_user(self.users["his"]):
			first = conflicts.retry(name)
		self.assertFalse(first["resolved"])
		self.assertIn("still has the open visit", first["error"])
		doc = frappe.get_doc(SYNC_CONFLICT_DOCTYPE, name)
		self.assertEqual((doc.status, doc.log[-1].action), (CONFLICT_OPEN, "retry_failed"))
		self.assertEqual(self.record(env["client_uuid"]).status, RECORD_STATUS_CONFLICT)

		engine.transition(
			facility_visit, "close_visit", system=True, closure_reason="Other", comment="T- done"
		)
		with as_user(self.users["mic"]):
			second = conflicts.retry(name, "T- facility visit closed")
		self.assertTrue(second["resolved"])
		rec = self.record(env["client_uuid"])
		self.assertEqual(rec.status, RECORD_STATUS_ACCEPTED)
		visit = frappe.get_doc(VISIT_DOCTYPE, rec.target_name)
		self.assertEqual((visit.patient, visit.owner, visit.is_outreach), (patient, self.users["member"], 1))
		doc.reload()
		self.assertEqual((doc.status, doc.decision), (CONFLICT_RESOLVED, DECISION_KEEP_OUTREACH))

	def test_closed_facility_visit_same_day_review_only(self):
		"""A closed facility visit on the capture date: the outreach visit is accepted with a review-only conflict
		(not counted on the patient); its only decision is Reviewed - keep both, which changes nothing."""
		patient = self.existing().name
		captured = (now_datetime() - timedelta(minutes=1)).replace(microsecond=0)
		facility_visit = make_visit(patient=patient, system=True, visit_date=captured.date()).name
		engine.transition(
			facility_visit, "close_visit", system=True, closure_reason="Other", comment="T- seen"
		)
		env = self.env(RECORD_VISIT_CAPTURE, self.visit_payload(patient=patient), captured=captured)
		result = self.push([env])["results"][0]
		self.assertEqual(result["result"], "accepted", result)
		doc = frappe.get_doc(SYNC_CONFLICT_DOCTYPE, result["conflict"])
		self.assertEqual((doc.review_only, doc.status), (1, CONFLICT_OPEN))
		self.assertEqual(self.count(patient), 0)
		self.assertEqual(conflicts.decisions_for(doc), (DECISION_ACKNOWLEDGE,))
		with self.assertRaises(conflicts.SyncConflictError):
			self.resolve(doc.name, DECISION_KEEP_FACILITY)
		self.resolve(doc.name, DECISION_ACKNOWLEDGE)
		self.assertEqual(frappe.db.get_value(SYNC_CONFLICT_DOCTYPE, doc.name, "status"), CONFLICT_RESOLVED)
		self.assertEqual(self.record(env["client_uuid"]).status, RECORD_STATUS_ACCEPTED)
		self.assertEqual(frappe.db.count(VISIT_DOCTYPE, {"patient": patient}), 2)


class TestConflictRecord(ConflictCase):
	def make_conflict(self):
		existing = self.existing()
		payload = self.register_payload(
			first_name=existing.first_name,
			last_name=existing.last_name,
			dob="1990-05-01",
			estimated_age_years=None,
		)
		return existing, self.push([self.env(RECORD_PATIENT_REGISTER, payload)])["results"][0]["conflict"]

	def test_decision_table_is_complete(self):
		"""Every (type, review flag) has decisions from CONFLICT_DECISIONS, each with a documented effect."""
		self.assertEqual({t for t, _review in conflicts.DECISIONS}, set(CONFLICT_TYPES))
		for (conflict_type, _review), decisions in conflicts.DECISIONS.items():
			for decision in decisions:
				self.assertIn(decision, CONFLICT_DECISIONS)
				self.assertIn((conflict_type, decision), conflicts.DECISION_EFFECTS)

	def test_view_is_access_logged_and_form_context(self):
		"""Opening the conflict in the desk writes an Access Log Entry for the HIS Officer, naming the patient; the
		form offers the type's decisions and the side-by-side rows."""
		from frappe.desk.form.load import getdoc

		existing, name = self.make_conflict()
		with as_user(self.users["his"]):
			getdoc(SYNC_CONFLICT_DOCTYPE, name)
			ctx = conflicts.form_context(frappe.get_doc(SYNC_CONFLICT_DOCTYPE, name))
		rows = frappe.get_all(
			ACCESS_LOG_DOCTYPE,
			filters={"reference_doctype": SYNC_CONFLICT_DOCTYPE, "reference_name": name},
			fields=["user", "action", "patient"],
		)
		self.assertEqual(
			[(r.user, r.action, r.patient) for r in rows], [(self.users["his"], "Open", existing.name)]
		)
		self.assertEqual(
			ctx["decisions"], list(conflicts.DECISIONS[(CONFLICT_DUPLICATE_REGISTRATION, False)])
		)
		first = next(r for r in ctx["rows"] if r["field"] == "first_name")
		self.assertEqual(
			(first["facility"], first["outreach"], first["differs"]),
			(existing.first_name, existing.first_name, False),
		)
		self.assertEqual(ctx["candidates"][0]["patient"], existing.name)

	def test_desk_edit_refused(self):
		"""Fields change only through resolve: a desk save of status or decision is refused."""
		_existing, name = self.make_conflict()
		with as_user(self.users["his"]):
			doc = frappe.get_doc(SYNC_CONFLICT_DOCTYPE, name)
			doc.status = CONFLICT_RESOLVED
			doc.decision = DECISION_KEEP_FACILITY
			with self.assertRaises(SyncConflictLockedError):
				doc.save()

	def test_estimated_age_registration_dob(self):
		"""The estimated DOB of a registration in conflict is computed as of the capture date."""
		existing = self.existing(dob=str(add_years(self.captured.date(), -30)))
		payload = self.register_payload(
			first_name=existing.first_name, last_name=existing.last_name, estimated_age_years=30
		)
		name = self.push([self.env(RECORD_PATIENT_REGISTER, payload)])["results"][0]["conflict"]
		outreach = frappe.parse_json(frappe.get_doc(SYNC_CONFLICT_DOCTYPE, name).outreach_version)
		self.assertEqual((getdate(outreach["dob"]), outreach["dob_estimated"]), (getdate(existing.dob), 1))
		self.assertEqual(
			frappe.get_all(OUTREACH_RECORD_DOCTYPE, filters={"conflict": name}, pluck="status"),
			[RECORD_STATUS_CONFLICT],
		)
