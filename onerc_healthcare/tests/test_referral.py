# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""B1 referral tests: FR-12, BR-14, SDD §4.2 (Referral) and §5.3. Every transition, allowed and denied."""

from __future__ import annotations

import frappe

from onerc_healthcare.clinical import referral as ref
from onerc_healthcare.clinical.consultation import start_consultation
from onerc_healthcare.clinical.diagnosis import DiagnosisError
from onerc_healthcare.clinical.referral import (
	IllegalReferralTransitionError,
	ReferralLockedError,
)
from onerc_healthcare.constants import (
	DEPT_MENTAL_HEALTH,
	DEPT_OUTPATIENT,
	DEPT_RECEPTION,
	DISPENSING_DOCTYPE,
	ENCOUNTER_DOCTYPE,
	REFERRAL_DOCTYPE,
	REFERRAL_FACILITY_DOCTYPE,
	ROLE_SYSADMIN,
	VISIT_DOCTYPE,
	VISIT_STATUS_AWAITING_LAB,
	VISIT_STATUS_IN_CONSULTATION,
	VISIT_STATUS_REFERRED,
	VISIT_STATUS_ROUTED,
)
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import (
	add_diagnosis,
	add_lab_row,
	as_user,
	routed_visit,
	start,
	users,
	visit_status,
)

# Every user key of visit_factories.VISIT_USERS.
ALL_KEYS = ("reception", "triage", "clinician", "mho", "lab", "pharmacist", "his", "mic", "hoo", "sysadmin")
ROLE_KEYS = {
	"Clinical Officer": "clinician",
	"Mental Health Officer": "mho",
	"HIS Officer": "his",
	"Medical In-Charge": "mic",
}


class ReferralTestCase(EMRTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users = users()
		frappe.set_user("Administrator")
		if not frappe.db.exists(REFERRAL_FACILITY_DOCTYPE, "T-Kakuma Mission Hospital"):
			frappe.get_doc(
				{
					"doctype": REFERRAL_FACILITY_DOCTYPE,
					"facility_name": "T-Kakuma Mission Hospital",
					"facility_type": "Hospital",
					"county": "Turkana",
				}
			).insert(ignore_permissions=True)

	def u(self, key: str) -> str:
		return self.users[key]

	def consultation(self, diagnosed: bool = True):
		visit = routed_visit(DEPT_OUTPATIENT)
		enc = start(visit.name, self.u("clinician"))
		if diagnosed:
			add_diagnosis(enc)
		return visit.name, enc

	def draft(self, referral_type: str = "Internal", user: str = "clinician", diagnosed: bool = True, **kw):
		_visit, enc = self.consultation(diagnosed)
		values = {"referral_type": referral_type, "reason": "T- needs specialist review"}
		if referral_type == "Internal":
			values["to_department"] = DEPT_MENTAL_HEALTH
		else:
			values["to_facility"] = "T-Kakuma Mission Hospital"
		values.update(kw)
		with as_user(self.u(user)):
			out = ref.create_referral(enc, **values)
		return out["name"]

	def referral_in(self, status: str, referral_type: str = "Internal") -> str:
		"""A referral advanced to `status` along the normal path."""
		name = self.draft(referral_type)
		if status == ref.STATUS_DRAFT:
			return name
		if status == ref.STATUS_CANCELLED:
			ref.transition(name, ref.ACTION_CANCEL, system=True)
			return name
		ref.transition(name, ref.ACTION_SEND, system=True)
		if status == ref.STATUS_SENT:
			return name
		if status == ref.STATUS_RECEIVED:
			ref.transition(name, ref.ACTION_RECEIVE, system=True)
		elif status == ref.STATUS_COMPLETED:
			ref.transition(name, ref.ACTION_RECORD_OUTCOME, system=True, outcome="T- seen")
		elif status == ref.STATUS_LOST:
			ref.transition(name, ref.ACTION_MARK_LOST, system=True, comment="T- never arrived")
		self.assertEqual(frappe.db.get_value(REFERRAL_DOCTYPE, name, "status"), status)
		return name

	def act(self, name: str, action: str, user: str, **kw):
		with as_user(self.u(user)):
			return ref.apply_action(name, action, **kw)

	def status(self, name: str) -> str:
		return frappe.db.get_value(REFERRAL_DOCTYPE, name, "status")


class TestReferralMeta(ReferralTestCase):
	def test_transition_table(self):
		"""The D8 table is introspectable and matches the documented lifecycle."""
		t = ref.TRANSITIONS
		self.assertEqual(
			{a: (r.from_statuses, r.to_status) for a, r in t.items()},
			{
				"save": (("",), "Draft"),
				"send": (("Draft",), "Sent"),
				"receive": (("Sent",), "Received"),
				"record_outcome": (("Sent", "Received"), "Completed"),
				"mark_lost": (("Sent",), "Lost to Follow-up"),
				"cancel": (("Draft",), "Cancelled"),
			},
		)
		self.assertTrue(t["receive"].is_system and t["receive"].internal_only)
		self.assertTrue(t["mark_lost"].needs_comment)
		self.assertTrue(t["record_outcome"].needs_outcome)

	def test_permissions_in_json(self):
		"""CO/MHO rwc; HIS and MIC read; no delete; EMR System Administrator nothing (SDD §6.1)."""
		perms = {p.role: p for p in frappe.get_meta(REFERRAL_DOCTYPE).permissions}
		self.assertEqual(
			set(perms), {"Clinical Officer", "Mental Health Officer", "HIS Officer", "Medical In-Charge"}
		)
		for role, p in perms.items():
			self.assertFalse(p.delete, role)
		for role in ("Clinical Officer", "Mental Health Officer"):
			self.assertTrue(perms[role].read and perms[role].write and perms[role].create)
		self.assertTrue(perms["HIS Officer"].read)
		self.assertFalse(perms["HIS Officer"].write or perms["HIS Officer"].create)
		# The Medical In-Charge writes only to archive (L5; enforced by the controller).
		self.assertTrue(perms["Medical In-Charge"].read and perms["Medical In-Charge"].write)
		self.assertFalse(perms["Medical In-Charge"].create)
		self.assertNotIn(ROLE_SYSADMIN, perms)
		for doctype in (REFERRAL_FACILITY_DOCTYPE, "Referral Status Log", "EMR Encounter Diagnosis"):
			self.assertFalse(any(p.delete for p in frappe.get_meta(doctype).permissions), doctype)
		meta = frappe.get_meta(REFERRAL_DOCTYPE)
		self.assertEqual(meta.autoname, "naming_series:")
		self.assertTrue(meta.track_changes)
		self.assertTrue(meta.get_field("status").read_only)


class TestReferralSave(ReferralTestCase):
	def test_save_creates_draft_with_log(self):
		name = self.draft()
		doc = frappe.get_doc(REFERRAL_DOCTYPE, name)
		self.assertEqual(doc.status, "Draft")
		self.assertEqual(
			[(r.from_status, r.to_status, r.action) for r in doc.status_log], [("", "Draft", "save")]
		)
		self.assertTrue(doc.patient and doc.source_visit)
		self.assertIn("REF-", doc.name)

	def test_save_roles(self):
		"""save: Clinical Officer and MHO only."""
		self.draft(user="mho")
		_visit, enc = self.consultation()
		for key in ALL_KEYS:
			if key in ("clinician", "mho"):
				continue
			with as_user(self.u(key)), self.assertRaises(frappe.PermissionError, msg=key):
				ref.create_referral(enc, "Internal", "T- reason", to_department=DEPT_MENTAL_HEALTH)

	def test_server_fields_ignored_on_insert(self):
		_visit, enc = self.consultation()
		with as_user(self.u("clinician")):
			doc = frappe.get_doc(
				{
					"doctype": REFERRAL_DOCTYPE,
					"source_encounter": enc,
					"referral_type": "External",
					"to_facility_name": "T-Lodwar County Referral Hospital",
					"reason": "T-",
					"status": "Completed",
					"outcome": "forged",
					"patient": frappe.db.get_value("Patient", {}, "name"),
				}
			).insert()
		self.assertEqual((doc.status, doc.outcome), ("Draft", None))
		self.assertEqual(doc.patient, frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "patient"))

	def test_destination_rules(self):
		with self.assertRaises(frappe.ValidationError):
			self.draft("Internal", to_department=None)
		with self.assertRaises(IllegalReferralTransitionError):
			self.draft("Internal", to_department=DEPT_RECEPTION)
		with self.assertRaises(frappe.ValidationError):
			self.draft("External", to_facility=None)
		name = self.draft("External", to_facility=None, to_facility_name="T-Lodwar Hospital")
		self.assertEqual(frappe.db.get_value(REFERRAL_DOCTYPE, name, "to_facility_name"), "T-Lodwar Hospital")

	def test_draft_edit_and_lock(self):
		name = self.draft()
		with as_user(self.u("clinician")):
			doc = frappe.get_doc(REFERRAL_DOCTYPE, name)
			doc.reason = "T- updated reason"
			doc.save()
			doc.status = "Sent"
			with self.assertRaises(ReferralLockedError):
				doc.save()
		doc = frappe.get_doc(REFERRAL_DOCTYPE, name)
		doc.status_log[0].comment = "rewritten"
		with self.assertRaises(ReferralLockedError):
			doc.save(ignore_permissions=True)
		doc = frappe.get_doc(REFERRAL_DOCTYPE, name)
		doc.append("status_log", {"from_status": "Draft", "to_status": "Sent", "action": "send"})
		with self.assertRaises(ReferralLockedError):
			doc.save(ignore_permissions=True)
		ref.transition(name, ref.ACTION_SEND, system=True)
		doc = frappe.get_doc(REFERRAL_DOCTYPE, name)
		doc.reason = "after send"
		with self.assertRaises(ReferralLockedError):
			doc.save(ignore_permissions=True)


class TestReferralArchive(ReferralTestCase):
	def test_mic_archives_in_any_status(self):
		"""L5 / NFR-9: the Medical In-Charge may archive a referral after Draft; nothing else changes."""
		for status in (ref.STATUS_SENT, ref.STATUS_COMPLETED, ref.STATUS_DRAFT):
			name = self.referral_in(status)
			with as_user(self.u("mic")):
				doc = frappe.get_doc(REFERRAL_DOCTYPE, name)
				doc.krcs_archived = 1
				doc.save()
			self.assertEqual(frappe.db.get_value(REFERRAL_DOCTYPE, name, "krcs_archived"), 1, status)
			self.assertEqual(self.status(name), status)

	def test_archive_with_other_change_refused(self):
		name = self.referral_in(ref.STATUS_SENT)
		with as_user(self.u("mic")):
			doc = frappe.get_doc(REFERRAL_DOCTYPE, name)
			doc.krcs_archived = 1
			doc.reason = "T- rewritten"
			with self.assertRaises(ReferralLockedError):
				doc.save()
		draft = self.draft()
		with as_user(self.u("mic")):
			doc = frappe.get_doc(REFERRAL_DOCTYPE, draft)
			doc.reason = "T- MIC may not edit a draft"
			with self.assertRaises(ReferralLockedError):
				doc.save()

	def test_only_mic_archives(self):
		name = self.referral_in(ref.STATUS_SENT)
		for key in ("clinician", "mho"):
			with as_user(self.u(key)):
				doc = frappe.get_doc(REFERRAL_DOCTYPE, name)
				doc.krcs_archived = 1
				with self.assertRaises(ReferralLockedError, msg=key):
					doc.save()
		with as_user(self.u("his")):
			doc = frappe.get_doc(REFERRAL_DOCTYPE, name)
			doc.krcs_archived = 1
			with self.assertRaises(frappe.PermissionError):
				doc.save()


class TestReferralSend(ReferralTestCase):
	def test_internal_send_creates_routed_visit(self):
		"""Internal referral: source visit Referred, new visit Routed (no triage), referral Received on start."""
		visit, enc = self.consultation()
		with as_user(self.u("clinician")):
			out = ref.create_referral(
				enc, "Internal", "T- psychosis review", to_department=DEPT_MENTAL_HEALTH, send=True
			)
		self.assertEqual(out["status"], "Sent")
		self.assertEqual(out["source_visit_status"], VISIT_STATUS_REFERRED)
		self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "docstatus"), 1)
		source = frappe.get_doc(VISIT_DOCTYPE, visit)
		self.assertEqual(
			(source.status_log[-1].action, source.status_log[-1].comment), ("refer", "T- psychosis review")
		)
		target = frappe.get_doc(VISIT_DOCTYPE, out["target_visit"])
		self.assertEqual(target.status, VISIT_STATUS_ROUTED)
		self.assertEqual(target.current_department, DEPT_MENTAL_HEALTH)
		self.assertEqual(target.referred_from, out["name"])
		self.assertEqual(target.facility, source.facility)
		self.assertEqual(target.patient, source.patient)
		self.assertFalse(target.triage_vital_signs)
		self.assertFalse(target.routed_without_vitals)

		# The target department starts the consultation: the referral becomes Received.
		with as_user(self.u("mho")):
			res = start_consultation(target.name)
		self.assertEqual(visit_status(target.name), VISIT_STATUS_IN_CONSULTATION)
		self.assertEqual(self.status(out["name"]), "Received")
		self.assertTrue(res["encounter"])
		log = frappe.get_doc(REFERRAL_DOCTYPE, out["name"]).status_log
		self.assertEqual([r.action for r in log], ["save", "send", "receive"])

	def test_visit_hook_receives(self):
		"""`visit_on_update` (Patient Visit on_update) receives when the desk Start Consultation is used."""
		name = self.referral_in("Sent")
		target = frappe.db.get_value(REFERRAL_DOCTYPE, name, "target_visit")
		from onerc_healthcare.visit.engine import transition

		doc = transition(target, "start_consultation", system=True)
		ref.visit_on_update(doc)
		self.assertEqual(self.status(name), "Received")
		ref.visit_on_update(doc)  # idempotent
		self.assertEqual(self.status(name), "Received")

	def test_external_send(self):
		visit, enc = self.consultation()
		with as_user(self.u("mho")):
			out = ref.create_referral(
				enc,
				"External",
				"T- surgery",
				to_facility="T-Kakuma Mission Hospital",
				urgency="Urgent",
				send=True,
			)
		self.assertEqual(out["status"], "Sent")
		self.assertFalse(out["target_visit"])
		self.assertEqual(visit_status(visit), VISIT_STATUS_REFERRED)

	def test_send_needs_diagnosis(self):
		"""Sending completes the consultation, so the diagnosis rule applies (FR-11)."""
		name = self.draft(diagnosed=False)
		with self.assertRaises(DiagnosisError):
			self.act(name, "send", "clinician")
		self.assertEqual(self.status(name), "Draft")

	def test_send_while_lab_pending(self):
		"""Engine integration fix: refer is allowed from Awaiting Laboratory; the lab task stays open."""
		visit, enc = self.consultation()
		add_lab_row(enc)
		from onerc_healthcare.clinical.consultation import send_lab_orders

		with as_user(self.u("clinician")):
			send_lab_orders(enc)
		self.assertEqual(visit_status(visit), VISIT_STATUS_AWAITING_LAB)
		with as_user(self.u("clinician")):
			out = ref.create_referral(
				enc, "Internal", "T- refer before results", to_department=DEPT_MENTAL_HEALTH, send=True
			)
		self.assertEqual(out["status"], "Sent")
		source = frappe.get_doc(VISIT_DOCTYPE, visit)
		self.assertEqual((source.status, source.lab_open), (VISIT_STATUS_REFERRED, 1))
		self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "docstatus"), 1)
		self.assertEqual(visit_status(out["target_visit"]), VISIT_STATUS_ROUTED)

	def test_send_with_prescription_keeps_pharmacy_task(self):
		"""Prescriptions released by send raise `prescribe`; the Referred visit keeps pharmacy_open until the
		line is dispensed (B3 Dispensing Record), which clears it."""
		medication = frappe.db.get_value("Medication", {"is_orderable": 1}, "name")
		if not medication:
			self.skipTest("no orderable Medication (B3 catalogue not seeded)")
		item = frappe.db.get_value("Medication Linked Item", {"parent": medication}, "item")
		visit, enc = self.consultation()
		doc = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
		doc.append(
			"drug_prescription",
			{
				"medication": medication,
				"drug_code": item,
				"dosage": frappe.db.get_value("Prescription Dosage", {}, "name"),
				"period": frappe.db.get_value("Prescription Duration", {}, "name"),
				"dosage_form": frappe.db.get_value("Medication", medication, "dosage_form"),
			},
		)
		doc.save(ignore_permissions=True)
		with as_user(self.u("clinician")):
			ref.create_referral(
				enc, "External", "T- surgery", to_facility="T-Kakuma Mission Hospital", send=True
			)
		source = frappe.get_doc(VISIT_DOCTYPE, visit)
		self.assertEqual((source.status, source.pharmacy_open), (VISIT_STATUS_REFERRED, 1))
		self.assertEqual([r.action for r in source.status_log][-2:], ["prescribe", "refer"])

		mr = frappe.get_all("Medication Request", filters={"order_group": enc, "docstatus": 1}, pluck="name")
		self.assertEqual(len(mr), 1)
		qty = frappe.db.get_value("Medication Request", mr[0], "total_dispensable_quantity") or 1
		with as_user(self.u("pharmacist")):
			rec = frappe.get_doc(
				{
					"doctype": DISPENSING_DOCTYPE,
					"medication_request": mr[0],
					"outcome": "Dispensed",
					"quantity": qty,
					"final": 1,
				}
			).insert()
			rec.submit()
		source.reload()
		self.assertEqual((source.status, source.pharmacy_open), (VISIT_STATUS_REFERRED, 0))

	def test_inactive_facility_refused(self):
		frappe.db.set_value(REFERRAL_FACILITY_DOCTYPE, "T-Kakuma Mission Hospital", "is_active", 0)
		with self.assertRaises(frappe.ValidationError):
			self.draft("External")


class TestReferralMatrix(ReferralTestCase):
	"""Every action from every status, and every role, allowed and denied."""

	def test_status_matrix(self):
		"""Each user action from each status: legal only from its from_statuses (as the system)."""
		for action, rule in ref.TRANSITIONS.items():
			if action == ref.ACTION_SAVE:
				continue
			for status in ref.STATUSES:
				for rtype in ("Internal", "External"):
					if status == ref.STATUS_RECEIVED and rtype == "External":
						continue
					frappe.db.savepoint("ref_matrix")
					try:
						name = self.referral_in(status, rtype)
						kw = {"comment": "T- reason", "outcome": "T- outcome"}
						legal = status in rule.from_statuses and not (
							rule.internal_only and rtype != "Internal"
						)
						msg = f"{action} from {status} ({rtype})"
						if legal:
							doc = ref.transition(name, action, system=True, **kw)
							self.assertEqual(doc.status, rule.to_status, msg)
							self.assertEqual(doc.status_log[-1].action, action, msg)
						else:
							with self.assertRaises(IllegalReferralTransitionError, msg=msg):
								ref.transition(name, action, system=True, **kw)
							self.assertEqual(self.status(name), status, msg)
					finally:
						frappe.db.rollback(save_point="ref_matrix")

	def test_save_is_not_a_transition(self):
		name = self.draft()
		with self.assertRaises(IllegalReferralTransitionError):
			ref.transition(name, ref.ACTION_SAVE, system=True)

	def test_role_matrix(self):
		"""Each user action as each role from a legal status: allowed roles succeed, others PermissionError."""
		start_status = {
			"send": ref.STATUS_DRAFT,
			"record_outcome": ref.STATUS_SENT,
			"mark_lost": ref.STATUS_SENT,
			"cancel": ref.STATUS_DRAFT,
			"receive": ref.STATUS_SENT,
		}
		for action, status in start_status.items():
			rule = ref.TRANSITIONS[action]
			allowed = {ROLE_KEYS[r] for r in rule.roles}
			for key in ALL_KEYS:
				frappe.db.savepoint("ref_roles")
				try:
					name = self.referral_in(status)
					kw = {"comment": "T- reason", "outcome": "T- outcome"}
					msg = f"{action} as {key}"
					if key in allowed:
						out = self.act(name, action, key, **kw)
						self.assertEqual(out["status"], rule.to_status, msg)
					else:
						with self.assertRaises(frappe.PermissionError, msg=msg):
							self.act(name, action, key, **kw)
						self.assertEqual(self.status(name), status, msg)
				finally:
					frappe.db.rollback(save_point="ref_roles")

	def test_reason_and_outcome_required(self):
		name = self.referral_in("Sent")
		with self.assertRaises(IllegalReferralTransitionError):
			self.act(name, "mark_lost", "mic")
		with self.assertRaises(IllegalReferralTransitionError):
			self.act(name, "record_outcome", "his")
		with self.assertRaises(IllegalReferralTransitionError):
			self.act(name, "record_outcome", "his", outcome="T- seen", outcome_date="2999-01-01")
		out = self.act(name, "record_outcome", "his", outcome="T- admitted for review")
		self.assertEqual(out["status"], "Completed")
		doc = frappe.get_doc(REFERRAL_DOCTYPE, name)
		self.assertEqual(doc.outcome, "T- admitted for review")
		self.assertTrue(doc.outcome_date)

	def test_outcome_after_received(self):
		name = self.referral_in("Received")
		out = self.act(name, "record_outcome", "mho", outcome="T- reviewed")
		self.assertEqual(out["status"], "Completed")

	def test_available_actions(self):
		draft = frappe.get_doc(REFERRAL_DOCTYPE, self.draft())
		with as_user(self.u("clinician")):
			self.assertEqual({a["action"] for a in ref.get_available_actions(draft)}, {"send", "cancel"})
		with as_user(self.u("his")):
			self.assertEqual(ref.get_available_actions(draft), [])
		sent = frappe.get_doc(REFERRAL_DOCTYPE, self.referral_in("Sent"))
		self.assertEqual(
			{a["action"] for a in ref.get_available_actions(sent, self.u("his"))}, {"record_outcome"}
		)
		self.assertEqual({a["action"] for a in ref.get_available_actions(sent, self.u("mic"))}, {"mark_lost"})
		self.assertEqual(
			{a["action"] for a in ref.get_available_actions(sent, self.u("clinician"))},
			{"record_outcome", "mark_lost"},
		)


class TestReferralLocking(ReferralTestCase):
	def test_send_locks_visit_before_encounter(self):
		"""M7: `send` locks the source visit before the encounter it completes."""
		from unittest.mock import patch

		name = self.draft()
		db = frappe.local.db
		with patch.object(db, "sql", wraps=db.sql) as spy:
			ref.transition(name, ref.ACTION_SEND, system=True)
		queries = [" ".join(str(c.args[0]).split()).lower() for c in spy.call_args_list if c.args]
		visit_lock = next(i for i, q in enumerate(queries) if "for update" in q and "`tabpatient visit`" in q)
		enc_lock = next(
			i for i, q in enumerate(queries) if "for update" in q and "`tabpatient encounter`" in q
		)
		self.assertLess(visit_lock, enc_lock)
