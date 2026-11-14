# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Data protection: consent, data access requests, access log and audit (FR-37, FR-38, FR-40, NFR-7,
BR-12, C-3; SDD §5.10, §6.2; D15)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.utils import CallbackManager, add_days, getdate, today

from onerc_healthcare.constants import (
	ACCESS_LOG_DOCTYPE,
	CONSENT_DOCTYPE,
	CONSENT_STATEMENT_DOCTYPE,
	DATA_ACCESS_REQUEST_DOCTYPE,
	DEPT_MENTAL_HEALTH,
	DEPT_OUTPATIENT,
	ENCOUNTER_DOCTYPE,
	REPORT_ACCESS_AUDIT,
	REPORT_CONSENT_EXCEPTIONS,
	ROLE_CLINICIAN,
	ROLE_HEAD_OPS,
	ROLE_HIS,
	ROLE_LAB,
	ROLE_MHO,
	ROLE_MIC,
	ROLE_PHARMACIST,
	ROLE_RECEPTIONIST,
	ROLE_SYSADMIN,
	ROLE_TRIAGE,
)
from onerc_healthcare.emr_data_protection.doctype.access_log_entry.access_log_entry import (
	AccessLogImmutableError,
)
from onerc_healthcare.emr_data_protection.doctype.patient_consent.patient_consent import PatientConsentError
from onerc_healthcare.emr_data_protection.report.access_audit import access_audit
from onerc_healthcare.emr_data_protection.report.consent_exceptions import consent_exceptions
from onerc_healthcare.privacy import access_log, consent, dar, read_log, statements
from onerc_healthcare.privacy import setup as privacy_setup
from onerc_healthcare.privacy.lifecycle import IllegalTransitionError, LockedFieldError
from onerc_healthcare.privacy.statements import ConsentStatementLockedError
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import make_patient, make_user
from onerc_healthcare.visit.patients import InactivePatientError

USERS = {
	"reception": ROLE_RECEPTIONIST,
	"triage": ROLE_TRIAGE,
	"clinician": ROLE_CLINICIAN,
	"mho": ROLE_MHO,
	"lab": ROLE_LAB,
	"pharmacist": ROLE_PHARMACIST,
	"his": ROLE_HIS,
	"mic": ROLE_MIC,
	"sysadmin": ROLE_SYSADMIN,
	"hoo": ROLE_HEAD_OPS,
}


class PrivacyTestCase(EMRTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.users = {key: make_user(f"dp-{key}", [role]) for key, role in USERS.items()}

	# --- helpers -------------------------------------------------------------------------------------
	def as_user(self, key: str):
		frappe.set_user(self.users[key] if key in self.users else key)

	def active_statement(self, language: str = "en") -> str:
		name = statements.get_active_statement(language)
		if name:
			return name
		return self.make_statement("T-EN-BASE", language=language, activate=True).name

	def make_statement(
		self, version: str, language: str = "en", activate: bool = False, text: str = "T- text"
	):
		doc = frappe.get_doc(
			{"doctype": CONSENT_STATEMENT_DOCTYPE, "version": version, "language": language, "text": text}
		).insert(ignore_permissions=True)
		if activate:
			doc = statements.activate(doc.name, system=True)
		return doc

	def make_consent(self, patient: str, outcome: str = "Given", submit: bool = True, **kw):
		values = {
			"doctype": CONSENT_DOCTYPE,
			"patient": patient,
			"statement": kw.pop("statement", None) or self.active_statement(),
			"outcome": outcome,
		}
		if outcome != "Given":
			values["reason"] = "T- reason"
		values.update(kw)
		doc = frappe.get_doc(values).insert(ignore_permissions=True)
		if submit:
			doc.submit()
		return doc

	def make_dar(self, patient: str | None = None, request_type: str = "Access", **kw):
		values = {
			"doctype": DATA_ACCESS_REQUEST_DOCTYPE,
			"patient": patient or make_patient().name,
			"request_type": request_type,
			"requester_type": "Patient",
			"requester_name": "T- Requester",
			"details": "T- copy of my record",
		}
		values.update(kw)
		return frappe.get_doc(values).insert(ignore_permissions=True)

	def dar_at(self, status: str, request_type: str = "Access", **kw):
		"""A Data Access Request driven (as Administrator) to `status`."""
		doc = self.make_dar(request_type=request_type, **kw)
		path = {
			dar.STATUS_RECEIVED: [],
			dar.STATUS_IDENTITY_VERIFIED: ["verify"],
			dar.STATUS_APPROVED: ["verify", "approve"],
			dar.STATUS_REJECTED: ["verify", "reject"],
			dar.STATUS_RESPONSE_READY: ["verify", "approve", "ready"],
			dar.STATUS_CLOSED: ["verify", "approve", "ready", "respond"],
		}[status]
		for step in path:
			if step == "verify":
				dar.transition(
					doc.name, "verify_identity", identity_method="National ID", identity_evidence="T-1"
				)
			elif step == "approve":
				dar.transition(doc.name, "approve")
			elif step == "reject":
				dar.transition(doc.name, "reject", decision_reason="T- no")
			elif step == "ready":
				if request_type == "Access":
					dar.transition(doc.name, "prepare_extract")
				else:
					self._record_correction(doc.name)
			elif step == "respond":
				dar.transition(doc.name, "respond", response_date=today(), response_method="In person")
		return frappe.get_doc(DATA_ACCESS_REQUEST_DOCTYPE, doc.name)

	def _change_patient(self, patient: str) -> str:
		"""Change the patient's record as Administrator; return the new Version name."""
		user = frappe.session.user
		frappe.set_user("Administrator")
		doc = frappe.get_doc("Patient", patient)
		doc.occupation = f"T-occupation-{frappe.generate_hash(length=6)}"
		doc.save(ignore_permissions=True, ignore_version=False)  # Frappe skips Versions in tests by default
		frappe.set_user(user)
		return frappe.get_all(
			"Version",
			filters={"ref_doctype": "Patient", "docname": patient},
			pluck="name",
			order_by="creation desc",
			limit=1,
		)[0]

	def _record_correction(self, request: str, **kw):
		patient = frappe.db.get_value(DATA_ACCESS_REQUEST_DOCTYPE, request, "patient")
		version = self._change_patient(patient)
		return dar.transition(
			request,
			"record_correction",
			corrected_doctype="Patient",
			corrected_record=patient,
			correction_version=version,
			**kw,
		)

	def entries(self, **filters) -> list[dict]:
		return frappe.get_all(
			ACCESS_LOG_DOCTYPE,
			filters=filters,
			fields=[
				"name",
				"user",
				"action",
				"reference_doctype",
				"reference_name",
				"patient",
				"sensitive",
				"ip",
				"details",
			],
			ignore_permissions=True,
		)


# =====================================================================================================
# Consent Statement (FR-37, SDD §5.10)
# =====================================================================================================
class TestConsentStatement(PrivacyTestCase):
	def test_transitions_are_introspectable(self):
		self.assertEqual(set(statements.TRANSITIONS), {"activate", "retire"})
		self.assertEqual(statements.TRANSITIONS["activate"].from_statuses, ("Draft",))
		self.assertEqual(set(statements.TRANSITIONS["activate"].roles), {ROLE_MIC, ROLE_SYSADMIN})
		self.assertTrue(statements.TRANSITIONS["retire"].is_system)

	def test_new_statement_is_always_draft(self):
		doc = frappe.get_doc(
			{
				"doctype": CONSENT_STATEMENT_DOCTYPE,
				"version": "T-ST-1",
				"language": "en",
				"text": "T- text",
				"status": "Active",
			}
		).insert(ignore_permissions=True)
		self.assertEqual(doc.status, "Draft")
		self.assertFalse(doc.log)

	def test_activate_retires_previous_of_same_language(self):
		previous = self.active_statement()
		doc = self.make_statement("T-ST-2")
		self.as_user("mic")
		statements.activate_statement(doc.name)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(CONSENT_STATEMENT_DOCTYPE, doc.name, "status"), "Active")
		self.assertEqual(frappe.db.get_value(CONSENT_STATEMENT_DOCTYPE, previous, "status"), "Retired")
		self.assertEqual(statements.get_active_statement("en"), doc.name)
		self.assertEqual(
			frappe.db.count(CONSENT_STATEMENT_DOCTYPE, {"language": "en", "status": "Active"}), 1
		)
		old = frappe.get_doc(CONSENT_STATEMENT_DOCTYPE, previous)
		self.assertEqual(old.log[-1].action, "retire")
		self.assertTrue(old.retired_at)
		new = frappe.get_doc(CONSENT_STATEMENT_DOCTYPE, doc.name)
		self.assertEqual(new.log[-1].action, "activate")
		self.assertEqual(new.log[-1].user, self.users["mic"])
		self.assertTrue(new.activated_at and new.effective_date)

	def test_activate_other_language_keeps_english_active(self):
		if not frappe.db.exists("Language", "sw"):
			self.skipTest("Language sw not installed")
		english = self.active_statement()
		self.make_statement("T-ST-SW", language="sw", activate=True)
		self.assertEqual(frappe.db.get_value(CONSENT_STATEMENT_DOCTYPE, english, "status"), "Active")

	def test_activate_roles(self):
		for key in ("reception", "his", "clinician", "lab", "triage"):
			doc = self.make_statement(f"T-ST-R-{key}")
			self.as_user(key)
			with self.assertRaises(frappe.PermissionError, msg=key):
				statements.activate_statement(doc.name)
			frappe.set_user("Administrator")
		for key in ("mic", "sysadmin"):
			doc = self.make_statement(f"T-ST-R-{key}")
			self.as_user(key)
			self.assertEqual(statements.activate_statement(doc.name)["status"], "Active", key)
			frappe.set_user("Administrator")

	def test_activate_only_from_draft(self):
		active = self.make_statement("T-ST-3", activate=True)
		with self.assertRaises(IllegalTransitionError):
			statements.activate(active.name)
		self.make_statement("T-ST-4", activate=True)
		with self.assertRaises(IllegalTransitionError):
			statements.activate(active.name)  # now Retired

	def test_activate_needs_text(self):
		doc = self.make_statement("T-ST-5")
		frappe.db.set_value(CONSENT_STATEMENT_DOCTYPE, doc.name, "text", "<p> </p>")
		with self.assertRaises(IllegalTransitionError):
			statements.activate(doc.name)

	def test_status_and_history_not_editable(self):
		doc = self.make_statement("T-ST-6", activate=True)
		doc.reload()
		doc.status = "Retired"
		with self.assertRaises(LockedFieldError):
			doc.save(ignore_permissions=True)
		doc.reload()
		doc.log[0].comment = "T- edited"
		with self.assertRaises(LockedFieldError):
			doc.save(ignore_permissions=True)

	def test_text_locked_once_used(self):
		doc = self.make_statement("T-ST-7", activate=True)
		doc.reload()
		doc.text = "T- edited before use"
		doc.save(ignore_permissions=True)  # not used yet: allowed
		self.make_consent(make_patient().name, statement=doc.name)
		doc.reload()
		doc.text = "T- edited after use"
		with self.assertRaises(ConsentStatementLockedError):
			doc.save(ignore_permissions=True)
		doc.reload()
		doc.effective_date = add_days(today(), 1)
		doc.save(ignore_permissions=True)  # other fields stay editable

	def test_earlier_consent_keeps_its_version(self):
		first = self.make_statement("T-ST-8", activate=True)
		patient = make_patient().name
		old = self.make_consent(patient, statement=first.name)
		self.make_statement("T-ST-9", activate=True)
		self.assertEqual(frappe.db.get_value(CONSENT_DOCTYPE, old.name, "statement_version"), "T-ST-8")
		self.assertEqual(consent.consent_status(patient)["version"], "T-ST-8")

	def test_seeded_provisional_statement_and_idempotent_install(self):
		seed = frappe.db.get_value(
			CONSENT_STATEMENT_DOCTYPE,
			privacy_setup.SEED_VERSION,
			["language", "is_provisional", "provisional_note", "text"],
			as_dict=True,
		)
		self.assertTrue(seed, "the provisional statement is seeded on install")
		self.assertEqual(seed.language, "en")
		self.assertTrue(seed.is_provisional)
		self.assertIn("OI-14", seed.provisional_note)
		self.assertIn("PROVISIONAL", seed.text)
		count = frappe.db.count(CONSENT_STATEMENT_DOCTYPE)
		privacy_setup.install()
		privacy_setup.install()
		self.assertEqual(frappe.db.count(CONSENT_STATEMENT_DOCTYPE), count)
		self.assertEqual(
			dar.get_response_days(), frappe.db.get_single_value(dar.SETTINGS_DOCTYPE, "response_days")
		)


# =====================================================================================================
# Patient Consent (FR-37, BR-12, C-3)
# =====================================================================================================
class TestPatientConsent(PrivacyTestCase):
	def test_receptionist_records_consent(self):
		patient = make_patient().name
		self.as_user("reception")
		status = consent.record_consent(patient=patient, statement=self.active_statement(), outcome="Given")
		self.assertEqual(status["outcome"], "Given")
		frappe.set_user("Administrator")
		doc = frappe.get_doc(CONSENT_DOCTYPE, status["consent"])
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(doc.recorded_by, self.users["reception"])
		self.assertTrue(doc.recorded_at)
		self.assertIn("CON", doc.name)
		statement = frappe.get_doc(CONSENT_STATEMENT_DOCTYPE, doc.statement)
		self.assertEqual(doc.statement_version, statement.version)
		self.assertEqual(doc.statement_language, statement.language)

	def test_recorded_by_is_server_set(self):
		patient = make_patient().name
		doc = self.make_consent(
			patient, submit=False, recorded_by=self.users["mic"], recorded_at="2020-01-01"
		)
		self.assertEqual(doc.recorded_by, "Administrator")
		self.assertNotEqual(getdate(doc.recorded_at), getdate("2020-01-01"))

	def test_other_roles_cannot_record(self):
		patient = make_patient().name
		statement = self.active_statement()
		for key in ("clinician", "mho", "his", "mic", "lab", "triage", "sysadmin"):
			self.as_user(key)
			with self.assertRaises(frappe.PermissionError, msg=key):
				consent.record_consent(patient=patient, statement=statement, outcome="Given")
			frappe.set_user("Administrator")

	def test_read_roles(self):
		doc = self.make_consent(make_patient().name)
		for key, allowed in {
			"reception": True,
			"clinician": True,
			"mho": True,
			"his": True,
			"mic": True,
			"lab": False,
			"pharmacist": False,
			"triage": False,
			"sysadmin": False,
		}.items():
			self.assertEqual(
				frappe.has_permission(CONSENT_DOCTYPE, "read", doc=doc, user=self.users[key]), allowed, key
			)

	def test_representative_needs_name_and_relationship(self):
		patient = make_patient().name
		with self.assertRaises(PatientConsentError):
			self.make_consent(patient, given_by="Representative", representative_name="T- Mother")
		with self.assertRaises(PatientConsentError):
			self.make_consent(patient, given_by="Representative", relationship="Mother")
		doc = self.make_consent(
			patient, given_by="Representative", representative_name="T- Amina", relationship="Mother"
		)
		self.assertEqual(doc.representative_name, "T- Amina")

	def test_patient_given_by_clears_representative(self):
		doc = self.make_consent(make_patient().name, representative_name="T- X", relationship="Uncle")
		self.assertIsNone(doc.representative_name)
		self.assertIsNone(doc.relationship)

	def test_reason_required_unless_given(self):
		patient = make_patient().name
		for outcome in ("Withheld", "Unable to Consent"):
			with self.assertRaises(PatientConsentError, msg=outcome):
				self.make_consent(patient, outcome=outcome, reason="  ")
			self.make_consent(patient, outcome=outcome, reason="T- declined")
		self.make_consent(patient, outcome="Given", reason=None)

	def test_statement_must_be_active(self):
		patient = make_patient().name
		draft = self.make_statement("T-ST-D")
		with self.assertRaises(PatientConsentError):
			self.make_consent(patient, statement=draft.name)
		old = self.active_statement()
		self.make_statement("T-ST-NEW", activate=True)
		with self.assertRaises(PatientConsentError):
			self.make_consent(patient, statement=old)  # now Retired

	def test_draft_with_retired_statement_cannot_be_submitted(self):
		patient = make_patient().name
		doc = self.make_consent(patient, submit=False)
		self.make_statement("T-ST-NEW2", activate=True)
		with self.assertRaises(PatientConsentError):
			doc.submit()

	def test_withdrawn_requires_current_given(self):
		patient = make_patient().name
		with self.assertRaises(PatientConsentError):
			self.make_consent(patient, outcome="Withdrawn")
		self.make_consent(patient, outcome="Withheld")
		with self.assertRaises(PatientConsentError):
			self.make_consent(patient, outcome="Withdrawn")
		self.make_consent(patient, outcome="Given")
		self.make_consent(patient, outcome="Withdrawn")
		self.assertEqual(consent.consent_status(patient)["outcome"], "Withdrawn")

	def test_no_cancel_for_anyone(self):
		doc = self.make_consent(make_patient().name)
		with self.assertRaises(frappe.PermissionError):
			doc.cancel()
		for key in self.users:
			self.assertFalse(frappe.has_permission(CONSENT_DOCTYPE, "cancel", user=self.users[key]), key)
			self.assertFalse(frappe.has_permission(CONSENT_DOCTYPE, "delete", user=self.users[key]), key)

	def test_latest_submitted_is_current(self):
		patient = make_patient().name
		self.make_consent(patient, outcome="Withheld")
		given = self.make_consent(patient, outcome="Given")
		self.make_consent(patient, outcome="Withheld", submit=False)  # drafts never count
		status = consent.consent_status(patient)
		self.assertEqual(status["outcome"], "Given")
		self.assertEqual(status["consent"], given.name)

	def test_inactive_patient_refused(self):
		patient = make_patient()
		frappe.db.set_value("Patient", patient.name, "status", "Disabled")
		with self.assertRaises(InactivePatientError):
			self.make_consent(patient.name)


class TestConsentStatus(PrivacyTestCase):
	def test_none(self):
		patient = make_patient().name
		self.as_user("reception")
		status = consent.get_consent_status(patient)
		self.assertEqual(status["outcome"], "none")
		self.assertIsNone(status["version"])
		self.assertTrue(status["needs_attention"])

	def test_given(self):
		patient = make_patient().name
		doc = self.make_consent(patient)
		self.as_user("clinician")
		status = consent.get_consent_status(patient)
		self.assertEqual(status["outcome"], "Given")
		self.assertEqual(status["date"], getdate(doc.recorded_at))
		self.assertEqual(status["version"], doc.statement_version)
		self.assertFalse(status["needs_attention"])

	def test_permission(self):
		patient = make_patient().name
		for key in ("lab", "pharmacist", "triage", "sysadmin"):
			self.as_user(key)
			with self.assertRaises(frappe.PermissionError, msg=key):
				consent.get_consent_status(patient)
			frappe.set_user("Administrator")
		with self.assertRaises(frappe.DoesNotExistError):
			consent.get_consent_status("T-NO-SUCH-PATIENT")

	def test_visit_warning_is_not_a_block(self):
		patient = make_patient().name
		self.assertEqual(consent.consent_warning(patient)["level"], "warning")
		self.make_consent(patient, outcome="Withheld")
		self.assertEqual(consent.consent_warning(patient)["level"], "info")
		self.make_consent(patient, outcome="Given")
		self.assertIsNone(consent.consent_warning(patient))
		self.as_user("reception")
		self.assertIsNone(consent.get_visit_consent_warning(patient))
		# The optional visit hook only shows a message; it never raises.
		consent.visit_after_insert(frappe._dict(patient=make_patient().name))


# =====================================================================================================
# Consent Exceptions report (FR-37, DPR-04)
# =====================================================================================================
class TestConsentExceptionsReport(PrivacyTestCase):
	def run_report(self, user_key="mic", **filters):
		self.as_user(user_key)
		try:
			_columns, data = consent_exceptions.execute(filters)
		finally:
			frappe.set_user("Administrator")
		return {r["patient"]: r for r in data}

	def test_lists_exceptions_with_last_visit(self):
		from onerc_healthcare.tests.visit_factories import make_visit

		none_patient = make_patient().name
		make_visit(patient=none_patient)
		given = make_patient().name
		self.make_consent(given)
		withheld = make_patient().name
		self.make_consent(withheld, outcome="Withheld")
		withdrawn = make_patient().name
		self.make_consent(withdrawn, outcome="Given")
		self.make_consent(withdrawn, outcome="Withdrawn")

		for patient, expected in (
			(none_patient, consent_exceptions.NO_CONSENT),
			(withheld, "Withheld"),
			(withdrawn, "Withdrawn"),
		):
			rows = self.run_report(patient=patient)
			self.assertIn(patient, rows)
			self.assertEqual(rows[patient]["outcome"], expected)
		self.assertEqual(
			self.run_report(patient=none_patient)[none_patient]["last_visit_date"], getdate(today())
		)
		self.assertNotIn(given, self.run_report(patient=given))
		self.assertNotIn(withheld, self.run_report(patient=withheld, outcome="Withdrawn"))
		self.assertIn(
			none_patient, self.run_report(patient=none_patient, outcome=consent_exceptions.NO_CONSENT)
		)
		self.assertNotIn(withheld, self.run_report(patient=withheld, with_visits_only=1))

	def test_merged_patients_left_out(self):
		from onerc_healthcare.visit.patients import MERGED_INTO_FIELD

		if not frappe.get_meta("Patient").has_field(MERGED_INTO_FIELD):
			self.skipTest("merge field not installed")
		source, target = make_patient().name, make_patient().name
		frappe.db.set_value("Patient", source, MERGED_INTO_FIELD, target)
		self.assertNotIn(source, self.run_report(patient=source))

	def test_roles(self):
		self.run_report("his")
		for key in ("reception", "clinician", "sysadmin", "lab"):
			with self.assertRaises(frappe.PermissionError, msg=key):
				self.run_report(key)
		roles = set(frappe.get_all("Has Role", filters={"parent": REPORT_CONSENT_EXCEPTIONS}, pluck="role"))
		self.assertEqual(roles, {ROLE_MIC, ROLE_HIS})

	def test_run_is_logged(self):
		self.run_report("mic")
		rows = self.entries(user=self.users["mic"], action="Report", reference_name=REPORT_CONSENT_EXCEPTIONS)
		self.assertEqual(len(rows), 1)


# =====================================================================================================
# Data Access Request (FR-38, BR-12)
# =====================================================================================================
class TestDataAccessRequest(PrivacyTestCase):
	def test_transitions_are_introspectable(self):
		self.assertEqual(
			set(dar.TRANSITIONS),
			{
				"create",
				"verify_identity",
				"approve",
				"reject",
				"prepare_extract",
				"record_correction",
				"respond",
			},
		)
		self.assertEqual(dar.TRANSITIONS["prepare_extract"].request_type, "Access")
		self.assertEqual(dar.TRANSITIONS["record_correction"].request_type, "Correction")
		self.assertEqual(set(dar.TRANSITIONS["respond"].from_statuses), {"Response Ready", "Rejected"})

	def test_create_sets_server_values(self):
		doc = self.make_dar(status="Closed", due_date="2000-01-01", decision_reason="T- forged")
		self.assertEqual(doc.status, "Received")
		self.assertEqual(getdate(doc.received_date), getdate(today()))
		self.assertEqual(getdate(doc.due_date), getdate(add_days(today(), 7)))
		self.assertIsNone(doc.decision_reason)
		self.assertEqual(len(doc.log), 1)
		self.assertEqual(doc.log[0].action, "create")
		self.assertIn("DAR", doc.name)

	def test_due_date_uses_settings(self):
		frappe.db.set_single_value(dar.SETTINGS_DOCTYPE, "response_days", 10)
		doc = self.make_dar()
		self.assertEqual(getdate(doc.due_date), getdate(add_days(today(), 10)))

	def test_create_roles(self):
		patient = make_patient().name
		for key in ("reception", "his"):
			self.as_user(key)
			doc = frappe.get_doc(
				{
					"doctype": DATA_ACCESS_REQUEST_DOCTYPE,
					"patient": patient,
					"requester_name": "T- Self",
					"details": "T- copy",
				}
			).insert()
			self.assertEqual(doc.status, "Received", key)
			frappe.set_user("Administrator")
		for key in ("clinician", "mic", "lab", "sysadmin", "triage"):
			self.as_user(key)
			with self.assertRaises(frappe.PermissionError, msg=key):
				frappe.get_doc(
					{
						"doctype": DATA_ACCESS_REQUEST_DOCTYPE,
						"patient": patient,
						"requester_name": "T- Self",
						"details": "T- copy",
					}
				).insert()
			frappe.set_user("Administrator")

	def test_representative_needs_relationship(self):
		with self.assertRaises(frappe.MandatoryError):
			self.make_dar(requester_type="Authorised Representative")
		doc = self.make_dar(requester_type="Authorised Representative", relationship="Mother")
		self.assertEqual(doc.relationship, "Mother")

	def test_inactive_patient_refused(self):
		patient = make_patient()
		frappe.db.set_value("Patient", patient.name, "status", "Disabled")
		with self.assertRaises(InactivePatientError):
			self.make_dar(patient=patient.name)

	def test_access_path_by_role_extract_private_and_logged(self):
		doc = self.make_dar()
		self.as_user("reception")
		dar.apply_action(
			doc.name, "verify_identity", identity_method="National ID", identity_evidence="T-123"
		)
		self.as_user("mic")
		dar.apply_action(doc.name, "approve")
		self.as_user("his")
		result = dar.apply_action(doc.name, "prepare_extract")
		self.assertEqual(result["status"], "Response Ready")
		self.assertTrue(result["extract_file"].startswith("/private/files/"))
		dar.apply_action(doc.name, "respond", response_date=today(), response_method="Email")
		frappe.set_user("Administrator")

		doc.reload()
		self.assertEqual(doc.status, "Closed")
		self.assertEqual(
			[r.action for r in doc.log],
			["create", "verify_identity", "approve", "prepare_extract", "respond"],
		)
		self.assertEqual(doc.identity_verified_by, self.users["reception"])
		self.assertEqual(doc.decided_by, self.users["mic"])
		self.assertEqual(doc.responded_by, self.users["his"])
		file_doc = frappe.get_doc("File", {"file_url": doc.extract_file})
		self.assertTrue(file_doc.is_private)
		# H1: not attached to the request (its readers would inherit the file) and owned by Administrator.
		self.assertFalse(file_doc.attached_to_doctype)
		self.assertEqual(file_doc.owner, "Administrator")
		content = file_doc.get_content()
		content = content.decode() if isinstance(content, bytes) else content
		self.assertIn(doc.patient, content)
		self.assertIn("Patient Record Extract", content)
		exports = self.entries(action="Export", reference_name=doc.name)
		self.assertEqual(len(exports), 1)
		self.assertEqual(exports[0]["patient"], doc.patient)
		self.assertEqual(exports[0]["user"], self.users["his"])

	def test_correction_path(self):
		doc = self.dar_at("Approved", request_type="Correction")
		self.as_user("his")
		version = self._change_patient(doc.patient)
		dar.apply_action(
			doc.name,
			"record_correction",
			corrected_doctype="Patient",
			corrected_record=doc.patient,
			correction_version=version,
		)
		self.as_user("mic")
		dar.apply_action(doc.name, "respond", response_date=today(), response_method="In person")
		frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(doc.status, "Closed")
		self.assertEqual(doc.correction_version, version)
		self.assertEqual(doc.corrected_record, doc.patient)

	def test_correction_must_belong_to_patient_and_follow_request(self):
		doc = self.dar_at("Approved", request_type="Correction")
		other = make_patient().name
		other_version = self._change_patient(other)
		with self.assertRaises(IllegalTransitionError):
			dar.transition(
				doc.name,
				"record_correction",
				corrected_doctype="Patient",
				corrected_record=other,
				correction_version=other_version,
			)
		with self.assertRaises(IllegalTransitionError):  # version of a different record
			dar.transition(
				doc.name,
				"record_correction",
				corrected_doctype="Patient",
				corrected_record=doc.patient,
				correction_version=other_version,
			)
		version = self._change_patient(doc.patient)
		frappe.db.set_value("Version", version, "creation", add_days(doc.creation, -1), update_modified=False)
		with self.assertRaises(IllegalTransitionError):  # made before the request
			dar.transition(
				doc.name,
				"record_correction",
				corrected_doctype="Patient",
				corrected_record=doc.patient,
				correction_version=version,
			)
		with self.assertRaises(IllegalTransitionError):
			dar.transition(doc.name, "record_correction")

	def test_reject_path(self):
		doc = self.dar_at("Identity Verified")
		self.as_user("mic")
		with self.assertRaises(IllegalTransitionError):
			dar.apply_action(doc.name, "reject", decision_reason=" ")
		dar.apply_action(doc.name, "reject", decision_reason="T- identity doubtful")
		self.as_user("his")
		dar.apply_action(doc.name, "respond", response_date=today(), response_method="Telephone")
		frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(doc.status, "Closed")
		self.assertEqual(doc.decision_reason, "T- identity doubtful")

	def test_action_role_matrix(self):
		"""Every action, allowed and denied, by role."""
		cases = {
			"verify_identity": ("Received", "Access", {"identity_method": "National ID"}),
			"approve": ("Identity Verified", "Access", {}),
			"reject": ("Identity Verified", "Access", {"decision_reason": "T- no"}),
			"prepare_extract": ("Approved", "Access", {}),
			"record_correction": ("Approved", "Correction", None),
			"respond": ("Response Ready", "Access", {"response_date": today(), "response_method": "Post"}),
		}
		for action, (status, request_type, kwargs) in cases.items():
			allowed = set(dar.TRANSITIONS[action].roles)
			for key, role in USERS.items():
				doc = self.dar_at(status, request_type=request_type)
				if kwargs is None:
					kw = {
						"corrected_doctype": "Patient",
						"corrected_record": doc.patient,
						"correction_version": self._change_patient(doc.patient),
					}
				else:
					kw = kwargs
				self.as_user(key)
				try:
					if role in allowed:
						result = dar.apply_action(doc.name, action, **kw)
						self.assertEqual(
							result["status"], dar.TRANSITIONS[action].to_status, f"{action}/{key}"
						)
					else:
						with self.assertRaises(frappe.PermissionError, msg=f"{action}/{key}"):
							dar.apply_action(doc.name, action, **kw)
				finally:
					frappe.set_user("Administrator")

	def test_illegal_from_status(self):
		received = self.dar_at("Received")
		for action in ("approve", "reject", "prepare_extract", "respond"):
			with self.assertRaises(IllegalTransitionError, msg=action):
				dar.transition(
					received.name,
					action,
					decision_reason="T-",
					response_date=today(),
					response_method="Post",
				)
		closed = self.dar_at("Closed")
		for action in ("verify_identity", "approve", "reject", "prepare_extract", "respond"):
			with self.assertRaises(IllegalTransitionError, msg=action):
				dar.transition(closed.name, action, identity_method="National ID", decision_reason="T-")
		approved = self.dar_at("Approved")
		with self.assertRaises(IllegalTransitionError):
			dar.transition(approved.name, "respond", response_date=today(), response_method="Post")
		with self.assertRaises(IllegalTransitionError):
			dar.transition(approved.name, "create")

	def test_request_type_guard(self):
		correction = self.dar_at("Approved", request_type="Correction")
		with self.assertRaises(dar.RequestTypeMismatchError):
			dar.transition(correction.name, "prepare_extract")
		access = self.dar_at("Approved")
		with self.assertRaises(dar.RequestTypeMismatchError):
			self._record_correction(access.name)
		actions = {a["action"] for a in dar.get_available_actions(access)}
		self.assertEqual(actions, {"prepare_extract"})

	def test_required_inputs(self):
		doc = self.dar_at("Received")
		with self.assertRaises(IllegalTransitionError):
			dar.transition(doc.name, "verify_identity")
		with self.assertRaises(IllegalTransitionError):
			dar.transition(doc.name, "verify_identity", identity_method="Fingerprint")
		rep = self.make_dar(requester_type="Authorised Representative", relationship="Son")
		with self.assertRaises(IllegalTransitionError):  # a representative needs evidence
			dar.transition(rep.name, "verify_identity", identity_method="National ID")
		dar.transition(
			rep.name, "verify_identity", identity_method="National ID", identity_evidence="T- letter"
		)
		ready = self.dar_at("Response Ready")
		with self.assertRaises(IllegalTransitionError):
			dar.transition(ready.name, "respond", response_method="Post")
		with self.assertRaises(IllegalTransitionError):
			dar.transition(ready.name, "respond", response_date=today())
		with self.assertRaises(IllegalTransitionError):
			dar.transition(ready.name, "respond", response_date=add_days(today(), 1), response_method="Post")
		with self.assertRaises(IllegalTransitionError):
			dar.transition(ready.name, "respond", response_date=add_days(today(), -1), response_method="Post")

	def test_lifecycle_fields_and_history_locked(self):
		doc = self.make_dar()
		doc.details = "T- edited while Received"
		doc.save(ignore_permissions=True)  # intake fields editable while Received
		doc.status = "Approved"
		with self.assertRaises(LockedFieldError):
			doc.save(ignore_permissions=True)
		doc.reload()
		doc.due_date = add_days(today(), 30)
		with self.assertRaises(LockedFieldError):
			doc.save(ignore_permissions=True)
		doc.reload()
		doc.append(
			"log",
			{
				"to_status": "Closed",
				"action": "respond",
				"user": "Administrator",
				"timestamp": frappe.utils.now_datetime(),
			},
		)
		with self.assertRaises(LockedFieldError):
			doc.save(ignore_permissions=True)
		verified = self.dar_at("Identity Verified")
		verified.details = "T- edited after verification"
		with self.assertRaises(LockedFieldError):
			verified.save(ignore_permissions=True)

	def test_form_save_with_string_dates_is_not_a_change(self):
		doc = self.make_dar()
		payload = json.loads(doc.as_json())
		payload["details"] = "T- edited from the form"
		form_doc = frappe.get_doc(payload)
		form_doc.save(ignore_permissions=True)
		self.assertEqual(
			frappe.db.get_value(DATA_ACCESS_REQUEST_DOCTYPE, doc.name, "details"), "T- edited from the form"
		)

	def test_overdue(self):
		doc = self.make_dar()
		self.assertFalse(dar.is_overdue(doc))
		self.assertTrue(dar.is_overdue(doc, on_date=add_days(today(), 8)))
		frappe.db.set_value(DATA_ACCESS_REQUEST_DOCTYPE, doc.name, "due_date", add_days(today(), -1))
		doc.reload()
		self.assertTrue(dar.is_overdue(doc))
		closed = self.dar_at("Closed")
		frappe.db.set_value(DATA_ACCESS_REQUEST_DOCTYPE, closed.name, "due_date", add_days(today(), -1))
		closed.reload()
		self.assertFalse(dar.is_overdue(closed))
		rejected = self.dar_at("Rejected")
		self.assertTrue(dar.is_overdue(rejected, on_date=add_days(today(), 30)))

	def test_read_permissions(self):
		doc = self.make_dar()
		for key, allowed in {
			"reception": True,
			"his": True,
			"mic": True,
			"clinician": False,
			"lab": False,
			"sysadmin": False,
		}.items():
			self.assertEqual(
				frappe.has_permission(DATA_ACCESS_REQUEST_DOCTYPE, "read", doc=doc, user=self.users[key]),
				allowed,
				key,
			)
			self.assertFalse(
				frappe.has_permission(DATA_ACCESS_REQUEST_DOCTYPE, "delete", user=self.users[key]), key
			)


# =====================================================================================================
# Access Log Entry (FR-40, NFR-7, SDD §5.10, §6.2)
# =====================================================================================================
class TestAccessLog(PrivacyTestCase):
	def open_as(self, key: str, doctype: str, name: str):
		from frappe.desk.form.load import getdoc

		self.as_user(key)
		try:
			getdoc(doctype, name)
		finally:
			frappe.set_user("Administrator")

	def test_desk_open_writes_one_entry(self):
		patient = make_patient().name
		self.open_as("reception", "Patient", patient)
		rows = self.entries(reference_doctype="Patient", reference_name=patient)
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row["user"], self.users["reception"])
		self.assertEqual(row["action"], "Open")
		self.assertEqual(row["patient"], patient)
		self.assertFalse(row["sensitive"])

	def test_open_of_patient_linked_record_resolves_patient(self):
		doc = self.make_consent(make_patient().name)
		self.open_as("clinician", CONSENT_DOCTYPE, doc.name)
		rows = self.entries(reference_name=doc.name, action="Open")
		self.assertEqual([r["patient"] for r in rows], [doc.patient])

	def test_open_persists_after_get_request_rollback(self):
		"""The real request path: GET rolls back, the entry is inserted after the response and committed."""
		from frappe.desk.form.load import getdoc

		patient = make_patient().name
		request = SimpleNamespace(
			after_response=CallbackManager(), path="/api/method/frappe.desk.form.load.getdoc"
		)
		had_request = hasattr(frappe.local, "request")
		old_request = getattr(frappe.local, "request", None)
		old_ip = getattr(frappe.local, "request_ip", None)
		frappe.local.request = request
		frappe.local.request_ip = "10.1.2.3"
		try:
			with patch.object(frappe, "in_test", False):
				frappe.db.savepoint("t_get_request")
				self.as_user("reception")
				getdoc("Patient", patient)
				frappe.set_user("Administrator")
				self.assertEqual(
					self.entries(reference_name=patient), [], "nothing written during the request"
				)
				# End of a GET request: Frappe rolls back (after_commit callbacks would be lost here).
				frappe.db.rollback(save_point="t_get_request")
				with patch.object(frappe.db, "commit", lambda *a, **k: None):
					request.after_response.run()
		finally:
			if had_request:
				frappe.local.request = old_request
			else:
				del frappe.local.request
			frappe.local.request_ip = old_ip
			frappe.set_user("Administrator")
		rows = self.entries(reference_name=patient)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["ip"], "10.1.2.3")
		self.assertEqual(rows[0]["user"], self.users["reception"])

	def test_after_commit_would_not_persist_on_get(self):
		"""Why commit_after_response: a GET request rolls back, which drops after_commit callbacks."""
		called = []
		frappe.db.after_commit.add(lambda: called.append(1))
		frappe.db.after_commit.reset()  # what db.rollback() does at the end of a GET request
		self.assertEqual(called, [])

	def test_administrator_and_save_not_logged(self):
		patient = make_patient().name
		self.open_as("Administrator", "Patient", patient)
		self.assertEqual(self.entries(reference_name=patient), [])
		doc = frappe.get_doc("Patient", patient)
		try:
			self.as_user("reception")  # set_user resets form_dict, so set the command afterwards
			frappe.local.form_dict.cmd = "frappe.desk.form.save.savedocs"
			access_log.log_open(doc)
		finally:
			frappe.local.form_dict.pop("cmd", None)
			frappe.set_user("Administrator")
		self.assertEqual(self.entries(reference_name=patient), [])

	def test_log_open_never_raises(self):
		doc = frappe.get_doc("Patient", make_patient().name)
		self.as_user("reception")
		with patch.object(access_log, "build_entry", side_effect=RuntimeError("boom")):
			access_log.log_open(doc)
		with patch.object(access_log, "insert_entry", side_effect=RuntimeError("boom")):
			access_log.log_open(doc)

	def test_entries_cannot_be_updated_or_deleted(self):
		entry = access_log.write_entry(
			"Open", reference_doctype="Patient", reference_name="T-X", patient="T-X"
		)
		entry.reload()
		entry.details = "T- tampered"
		with self.assertRaises(AccessLogImmutableError):
			entry.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc(ACCESS_LOG_DOCTYPE, entry.name, ignore_permissions=True)
		self.assertTrue(frappe.db.exists(ACCESS_LOG_DOCTYPE, entry.name))

	def test_only_mic_and_sysadmin_read(self):
		entry = access_log.write_entry(
			"Open", reference_doctype="Patient", reference_name="T-Y", patient="T-Y"
		)
		for key in self.users:
			allowed = key in ("mic", "sysadmin")
			user = self.users[key]
			self.assertEqual(
				frappe.has_permission(ACCESS_LOG_DOCTYPE, "read", doc=entry, user=user), allowed, key
			)
			for ptype in ("write", "create", "delete"):
				self.assertFalse(
					frappe.has_permission(ACCESS_LOG_DOCTYPE, ptype, doc=entry, user=user), f"{key}/{ptype}"
				)
			self.as_user(key)
			try:
				names = frappe.get_list(ACCESS_LOG_DOCTYPE, filters={"name": entry.name}, pluck="name")
			except frappe.PermissionError:
				names = []
			finally:
				frappe.set_user("Administrator")
			self.assertEqual(names, [entry.name] if allowed else [], key)

	def test_merge_does_not_rewrite_patient(self):
		from onerc_healthcare.registration.merge import merge_patients

		source, target = make_patient().name, make_patient().name
		self.open_as("reception", "Patient", source)
		consent_doc = self.make_consent(source)
		merge_patients(source, target, "T- duplicate registration")
		rows = self.entries(reference_name=source)
		self.assertEqual([r["patient"] for r in rows], [source])
		# Linked records do move (Patient Consent.patient is a Link), the audit trail does not.
		self.assertEqual(frappe.db.get_value(CONSENT_DOCTYPE, consent_doc.name, "patient"), target)

	def _core_log(self, **values):
		from frappe.deferred_insert import insert_record

		values.setdefault("user", self.users["reception"])
		insert_record(values, "Access Log")  # the production path (deferred insert -> insert())

	def test_core_print_is_mirrored(self):
		patient = make_patient().name
		self._core_log(
			export_from="Patient",
			reference_document=patient,
			file_type="PDF",
			method="Print",
			page="Print Format: Standard",
		)
		rows = self.entries(reference_name=patient, action="Print")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["patient"], patient)
		self.assertEqual(rows[0]["user"], self.users["reception"])
		self.assertIn("Print Format: Standard", rows[0]["details"])

	def test_core_pdf_page_html_and_filters_not_copied(self):
		self._core_log(file_type="PDF", method="PDF", page="<p>T- secret clinical content</p>")
		# An export of a DocType that read_log does not wrap is still mirrored (metadata only).
		self._core_log(
			export_from="Lab Test",
			report_name="Lab Test",
			file_type="CSV",
			method="Export",
			filters=json.dumps({"patient_name": "T- Secret Name"}),
		)
		rows = self.entries(user=self.users["reception"])
		# The unreferenced PDF row (report_to_pdf shape) is not mirrored at all; the export is.
		self.assertEqual({r["action"] for r in rows}, {"Export"})
		for r in rows:
			self.assertNotIn("secret", (r["details"] or "").lower())
		export = next(r for r in rows if r["action"] == "Export")
		self.assertEqual((export["reference_doctype"], export["reference_name"]), ("Report", "Lab Test"))

	def test_core_rows_of_wrapped_exports_not_mirrored_twice(self):
		"""read_log logs these exports itself (with the row count); the core row would duplicate it."""
		self._core_log(
			export_from="Patient Consent",
			report_name=REPORT_CONSENT_EXCEPTIONS,
			file_type="CSV",
			method="Export",
		)
		self._core_log(export_from="Patient", file_type="CSV")  # reportview.export_query shape
		self.assertEqual(self.entries(user=self.users["reception"]), [])

	def test_core_download_of_extract_resolves_patient(self):
		request = self.dar_at("Response Ready")
		file_name = frappe.db.get_value("File", {"file_url": request.extract_file}, "name")
		self._core_log(export_from="File", reference_document=file_name, file_type="html")
		rows = self.entries(reference_name=file_name, action="Download")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["patient"], request.patient)

	def _encounter(self, department: str) -> str:
		"""A Patient Encounter row in `department`, written directly: only the open/logging path is under
		test here, not B1's consultation rules (an encounter normally starts from a visit)."""
		from frappe.utils import nowdate, nowtime

		from onerc_healthcare.setup.operations import get_company
		from onerc_healthcare.tests.visit_factories import get_test_appointment_type, get_test_practitioner

		if not frappe.db.exists("Medical Department", department):
			self.skipTest(f"{department} not seeded")
		meta = frappe.get_meta(ENCOUNTER_DOCTYPE)
		field = "krcs_department" if meta.has_field("krcs_department") else "medical_department"
		doc = frappe.get_doc(
			{
				"doctype": ENCOUNTER_DOCTYPE,
				"patient": make_patient().name,
				"practitioner": get_test_practitioner(),
				"appointment_type": get_test_appointment_type(),
				"encounter_date": nowdate(),
				"encounter_time": nowtime(),
				"company": get_company(),
				field: department,
			}
		)
		doc.set_new_name()
		doc.db_insert()
		return doc.name

	def test_mental_health_encounter_view_is_sensitive(self):
		mh = self._encounter(DEPT_MENTAL_HEALTH)
		opd = self._encounter(DEPT_OUTPATIENT)
		for name in (mh, opd):
			self.open_as("mic", ENCOUNTER_DOCTYPE, name)
		self.assertEqual([r["sensitive"] for r in self.entries(reference_name=mh)], [1])
		self.assertEqual([r["sensitive"] for r in self.entries(reference_name=opd)], [0])


# =====================================================================================================
# Access Audit report (FR-40, NFR-7, D15)
# =====================================================================================================
class TestAccessAuditReport(PrivacyTestCase):
	def run_report(self, user_key="mic", **filters):
		self.as_user(user_key)
		try:
			result = access_audit.execute(filters)
		finally:
			frappe.set_user("Administrator")
		return result[1]

	def test_lists_access_and_changes_metadata_only(self):
		from frappe.desk.form.load import getdoc

		patient = make_patient().name
		self.as_user("reception")
		getdoc("Patient", patient)
		frappe.set_user("Administrator")
		self._change_patient(patient)
		occupation = frappe.db.get_value("Patient", patient, "occupation")

		rows = self.run_report(patient=patient)
		actions = {(r["action"], r["user"]) for r in rows}
		self.assertIn(("Open", self.users["reception"]), actions)
		self.assertIn(("Change", "Administrator"), actions)
		self.assertIn("Create", {r["action"] for r in rows})
		change = next(r for r in rows if r["action"] == "Change")
		self.assertIn("occupation", change["details"])
		self.assertNotIn(occupation, json.dumps(rows, default=str))
		self.assertTrue(all(r["patient"] == patient for r in rows))

	def test_filters(self):
		patient = make_patient().name
		access_log.write_entry(
			"Print",
			reference_doctype="Patient",
			reference_name=patient,
			patient=patient,
			user=self.users["his"],
		)
		access_log.write_entry(
			"Open",
			reference_doctype="Patient",
			reference_name=patient,
			patient=patient,
			user=self.users["mic"],
			sensitive=True,
		)
		self.assertEqual({r["action"] for r in self.run_report(patient=patient, action="Print")}, {"Print"})
		self.assertEqual(
			{r["user"] for r in self.run_report(patient=patient, user=self.users["his"])}, {self.users["his"]}
		)
		self.assertEqual([r["action"] for r in self.run_report(patient=patient, sensitive_only=1)], ["Open"])
		old = self.run_report(
			patient=patient, from_date=add_days(today(), -30), to_date=add_days(today(), -10)
		)
		self.assertEqual(old, [])

	def test_roles(self):
		self.run_report("sysadmin")
		for key in ("reception", "his", "clinician", "lab"):
			with self.assertRaises(frappe.PermissionError, msg=key):
				self.run_report(key)
		roles = set(frappe.get_all("Has Role", filters={"parent": REPORT_ACCESS_AUDIT}, pluck="role"))
		self.assertEqual(roles, {ROLE_MIC, ROLE_SYSADMIN})

	def test_run_is_logged(self):
		self.run_report("sysadmin", patient="T-AUDITED")
		rows = self.entries(user=self.users["sysadmin"], action="Report", reference_name=REPORT_ACCESS_AUDIT)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["patient"], "T-AUDITED")
		self.assertEqual(rows[0]["details"], "Filters: patient")


# =====================================================================================================
# Fix round: H1 extract protection, M2 handler, M4 read logging, L6 make_access_log, L5 archive
# =====================================================================================================
class TestExtractProtection(PrivacyTestCase):
	"""H1: the private record extract is reachable only through download_extract (HIS, MIC), logged."""

	def setUp(self) -> None:
		super().setUp()
		self.request = self.dar_at("Response Ready")
		self.file_doc = frappe.get_doc("File", {"file_url": self.request.extract_file})

	def download_as(self, key: str):
		from onerc_healthcare.privacy.extract import download_extract

		self.as_user(key)
		try:
			download_extract(self.request.name)
			return frappe._dict(
				filename=frappe.local.response.get("filename"),
				content=frappe.local.response.get("filecontent"),
				type=frappe.local.response.get("type"),
			)
		finally:
			for k in ("filename", "filecontent", "type"):
				frappe.local.response.pop(k, None)
			frappe.set_user("Administrator")

	def test_receptionist_denied_everywhere(self):
		from frappe.core.doctype.file.utils import find_file_by_url

		recep = self.users["reception"]
		self.assertTrue(
			frappe.has_permission(DATA_ACCESS_REQUEST_DOCTYPE, "read", doc=self.request, user=recep)
		)
		self.assertFalse(frappe.has_permission("File", "read", doc=self.file_doc, user=recep))
		self.as_user("reception")
		try:
			# /private/files: download_private_file serves only what find_file_by_url finds downloadable.
			self.assertIsNone(find_file_by_url(self.file_doc.file_url))
			self.assertFalse(self.file_doc.is_downloadable())
			with self.assertRaises(frappe.PermissionError):
				frappe.client.get("File", self.file_doc.name)
			# extract_file is permlevel 1: not even the path is sent to the receptionist.
			self.assertIsNone(
				frappe.client.get(DATA_ACCESS_REQUEST_DOCTYPE, self.request.name).get("extract_file")
			)
		finally:
			frappe.set_user("Administrator")
		with self.assertRaises(frappe.PermissionError):
			self.download_as("reception")
		self.assertEqual(self.entries(action="Download", reference_name=self.request.name), [])

	def test_other_roles_denied(self):
		for key in ("lab", "pharmacist", "hoo", "clinician", "sysadmin"):
			with self.assertRaises(frappe.PermissionError, msg=key):
				self.download_as(key)
			self.assertFalse(
				frappe.has_permission("File", "read", doc=self.file_doc, user=self.users[key]), key
			)
		self.assertEqual(self.entries(action="Download", reference_name=self.request.name), [])

	def test_his_and_mic_download_logged_once_each(self):
		result = self.download_as("his")
		self.assertEqual(result.type, "download")
		content = result.content.decode() if isinstance(result.content, bytes) else result.content
		self.assertIn(self.request.patient, content)
		rows = self.entries(action="Download", reference_name=self.request.name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["user"], self.users["his"])
		self.assertEqual(rows[0]["patient"], self.request.patient)
		self.download_as("mic")
		self.assertEqual(len(self.entries(action="Download", reference_name=self.request.name)), 2)

	def test_file_hook_denies_extract_files_only(self):
		"""The hook denies the extract (by path) and a legacy extract still attached through the old Attach
		field; any other file, including another attachment of the request, is left to Frappe's rules."""
		from onerc_healthcare.privacy.permissions import file_has_permission

		legacy = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "T-legacy-extract.html",
				"attached_to_doctype": DATA_ACCESS_REQUEST_DOCTYPE,
				"attached_to_name": self.request.name,
				"attached_to_field": "extract_file",
				"is_private": 1,
				"content": "<p>T-</p>",
			}
		).insert(ignore_permissions=True)
		scan = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "T-scan.txt",
				"attached_to_doctype": DATA_ACCESS_REQUEST_DOCTYPE,
				"attached_to_name": self.request.name,
				"is_private": 1,
				"content": "T- scan",
			}
		).insert(ignore_permissions=True)
		for key in self.users:
			for ptype in ("read", "write", "create"):
				self.assertFalse(file_has_permission(legacy, ptype, user=self.users[key]), key)
				self.assertFalse(file_has_permission(self.file_doc, ptype, user=self.users[key]), key)
			self.assertTrue(file_has_permission(scan, "read", user=self.users[key]), key)
		hook = "onerc_healthcare.privacy.permissions.file_has_permission"
		self.assertIn(hook, frappe.get_hooks("has_permission").get("File", []))
		for key in ("reception", "his", "mic"):
			self.assertFalse(frappe.has_permission("File", "read", doc=legacy, user=self.users[key]), key)

	def test_extract_stays_unattached_after_saves(self):
		"""An Attach field would make Frappe attach the file to the request on save (and so expose it to
		every reader of the request); extract_file is plain Data for that reason."""
		self.assertEqual(
			frappe.get_meta(DATA_ACCESS_REQUEST_DOCTYPE).get_field("extract_file").fieldtype, "Data"
		)
		dar.transition(self.request.name, "respond", response_date=today(), response_method="Email")
		self.assertFalse(frappe.db.get_value("File", self.file_doc.name, "attached_to_doctype"))

	def test_receptionist_form_save_keeps_hidden_extract(self):
		self.as_user("reception")
		try:
			data = frappe.client.get(DATA_ACCESS_REQUEST_DOCTYPE, self.request.name)
			frappe.get_doc(data).save()  # a desk save without the permlevel-1 field
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(
			frappe.db.get_value(DATA_ACCESS_REQUEST_DOCTYPE, self.request.name, "extract_file"),
			self.request.extract_file,
		)


class TestCorrectionVersionsHandler(PrivacyTestCase):
	"""M2: get_correction_versions is GET-only; the form must call it with GET."""

	def call(self, http_method: str, **args):
		import frappe.handler

		cmd = "onerc_healthcare.privacy.dar.get_correction_versions"
		had = hasattr(frappe.local, "request")
		old = getattr(frappe.local, "request", None)
		frappe.local.request = SimpleNamespace(method=http_method, path=f"/api/method/{cmd}")
		try:
			self.as_user("his")
			frappe.local.form_dict = frappe._dict(cmd=cmd, **args)
			return frappe.handler.execute_cmd(cmd)
		finally:
			if had:
				frappe.local.request = old
			else:
				del frappe.local.request
			frappe.set_user("Administrator")

	def test_end_to_end_through_handler(self):
		doc = self.dar_at("Approved", request_type="Correction")
		version = self._change_patient(doc.patient)
		args = {"request": doc.name, "corrected_doctype": "Patient", "corrected_record": doc.patient}
		rows = self.call("GET", **args)
		self.assertIn(version, [r.name for r in rows])
		with self.assertRaises(frappe.PermissionError):
			self.call("POST", **args)
		self.assertEqual(self.call("GET", **{**args, "corrected_record": make_patient().name}), [])


class TestReadLog(PrivacyTestCase):
	"""M4: read access outside the desk form is logged once per call, metadata only."""

	def with_form(self, key: str, fn, **form):
		self.as_user(key)
		try:
			frappe.local.form_dict = frappe._dict(form)
			return fn()
		finally:
			for k in ("filename", "filecontent", "type"):
				frappe.local.response.pop(k, None)
			frappe.set_user("Administrator")

	def test_log_access_helper(self):
		patient = make_patient().name
		self.as_user("clinician")
		access_log.log_access("Read (API)", "Patient", patient, patient=patient, detail="History panel")
		frappe.set_user("Administrator")
		access_log.log_access("Read (API)", "Patient", patient, patient=patient)  # Administrator: skipped
		rows = self.entries(reference_name=patient)
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0]["user"], rows[0]["details"]), (self.users["clinician"], "History panel"))

	def test_client_get(self):
		patient = make_patient().name
		self.as_user("reception")
		read_log.get("Patient", patient)
		read_log.get("Medical Department", DEPT_OUTPATIENT)  # not a logged DocType
		frappe.set_user("Administrator")
		rows = self.entries(user=self.users["reception"])
		self.assertEqual([(r["action"], r["patient"]) for r in rows], [("Open (API)", patient)])

	def test_client_get_refused_logs_nothing(self):
		doc = self.make_consent(make_patient().name)
		self.as_user("lab")
		with self.assertRaises(frappe.PermissionError):
			read_log.get(CONSENT_DOCTYPE, doc.name)
		frappe.set_user("Administrator")
		self.assertEqual(self.entries(user=self.users["lab"]), [])

	def test_client_get_value(self):
		patient = make_patient().name
		name = frappe.db.get_value("Patient", patient, "patient_name")
		self.as_user("reception")
		read_log.get_value("Patient", "patient_name", patient)
		read_log.get_value("Patient", ["patient_name", "sex"], {"name": patient})
		read_log.get_value("Medical Department", "name", {"name": DEPT_OUTPATIENT})
		frappe.set_user("Administrator")
		rows = self.entries(user=self.users["reception"])
		self.assertEqual(len(rows), 2)
		self.assertEqual({r["action"] for r in rows}, {"Read (API)"})
		self.assertEqual({r["patient"] for r in rows}, {patient})
		for r in rows:
			self.assertIn("patient_name", r["details"])
			self.assertNotIn(name, r["details"])

	def test_reportview_get(self):
		patient = make_patient().name
		self.with_form(
			"reception",
			read_log.reportview_get,
			doctype="Patient",
			fields=json.dumps(["`tabPatient`.`name`"]),
			filters=json.dumps([["Patient", "name", "=", patient]]),
		)
		self.with_form(
			"reception",
			read_log.reportview_get,
			doctype="Medical Department",
			fields=json.dumps(["`tabMedical Department`.`name`"]),
		)
		rows = self.entries(user=self.users["reception"])
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0]["action"], rows[0]["patient"]), ("List", patient))
		self.assertIn("Rows: 1", rows[0]["details"])
		self.assertNotIn(patient, rows[0]["details"])

	def test_reportview_export_query(self):
		patient = make_patient().name
		self.with_form(
			"his",
			read_log.reportview_export_query,
			doctype="Patient",
			file_format_type="CSV",
			fields=json.dumps(["`tabPatient`.`name`", "`tabPatient`.`patient_name`"]),
			filters=json.dumps([["Patient", "name", "=", patient]]),
		)
		rows = self.entries(user=self.users["his"])
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0]["action"], rows[0]["patient"]), ("Export", patient))
		self.assertIn("Rows: 1", rows[0]["details"])
		self.assertIn("File type: CSV", rows[0]["details"])

	def test_query_report_export_logged_once(self):
		patient = make_patient().name
		self.with_form(
			"mic",
			read_log.query_report_export_query,
			report_name=REPORT_CONSENT_EXCEPTIONS,
			file_format_type="CSV",
			filters=json.dumps({"patient": patient}),
			visible_idx="[]",
		)
		rows = self.entries(user=self.users["mic"])
		self.assertEqual([r["action"] for r in rows], ["Export"], "no extra Report-run entry")
		self.assertEqual(
			(rows[0]["reference_name"], rows[0]["patient"]), (REPORT_CONSENT_EXCEPTIONS, patient)
		)
		self.assertIn("Rows: 1", rows[0]["details"])
		self.assertFalse(access_log.is_logged_report("T-No Such Report"))

	def test_rest_get_after_request(self):
		patient = make_patient().name
		consent_doc = self.make_consent(patient)

		def hit(key, path, method="GET", status=200):
			self.as_user(key)
			try:
				read_log.after_request(
					response=SimpleNamespace(status_code=status),
					request=SimpleNamespace(method=method, path=path),
				)
			finally:
				frappe.set_user("Administrator")

		hit("reception", f"/api/resource/Patient/{patient}")
		hit("clinician", f"/api/v2/document/{CONSENT_DOCTYPE.replace(' ', '%20')}/{consent_doc.name}")
		hit("reception", f"/api/resource/Medical%20Department/{DEPT_OUTPATIENT}")  # not logged
		hit("reception", f"/api/resource/Patient/{patient}", status=403)  # refused read
		hit("reception", f"/api/resource/Patient/{patient}", method="PUT")  # not a read
		hit("reception", "/api/resource/Patient")  # list, not one record
		rows = self.entries(action="Open (API)")
		self.assertEqual(
			sorted((r["reference_doctype"], r["user"]) for r in rows),
			sorted([("Patient", self.users["reception"]), (CONSENT_DOCTYPE, self.users["clinician"])]),
		)
		self.assertEqual({r["patient"] for r in rows}, {patient})


class TestMakeAccessLogOverride(PrivacyTestCase):
	"""L6: over HTTP, make_access_log needs read on the record (or permission on the report)."""

	def core_rows(self, **filters) -> int:
		return frappe.db.count("Access Log", filters)

	def test_forged_entry_refused(self):
		doc = self.make_consent(make_patient().name)
		self.as_user("lab")
		try:
			with self.assertRaises(frappe.PermissionError):
				access_log.make_access_log(doctype=CONSENT_DOCTYPE, document=doc.name, method="Print")
			with self.assertRaises(frappe.PermissionError):
				access_log.make_access_log(report_name=REPORT_ACCESS_AUDIT, method="Export", file_type="CSV")
			with self.assertRaises(frappe.PermissionError):
				access_log.make_access_log(method="PDF", page="<p>T-</p>")
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self.core_rows(reference_document=doc.name), 0)
		self.assertEqual(self.entries(reference_name=doc.name), [])

	def test_readable_record_logged(self):
		patient = make_patient().name
		self.as_user("reception")
		try:
			access_log.make_access_log(doctype="Patient", document=patient, method="Print", file_type="PDF")
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self.core_rows(reference_document=patient, user=self.users["reception"]), 1)

	def test_server_side_callers_unaffected(self):
		from frappe.core.doctype.access_log.access_log import make_access_log as core

		self.as_user("lab")
		try:
			core(doctype="Patient", document="T-SERVER-SIDE", method="Print")
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self.core_rows(reference_document="T-SERVER-SIDE"), 1)


class TestArchive(PrivacyTestCase):
	"""L5: the Medical In-Charge archives closed requests and non-Draft statements; nothing else."""

	def test_data_access_request(self):
		from onerc_healthcare.privacy.archive import ArchiveNotAllowedError, archive_record

		if not frappe.get_meta(DATA_ACCESS_REQUEST_DOCTYPE).has_field("krcs_archived"):
			self.skipTest("krcs_archived not installed on Data Access Request")
		open_request = self.dar_at("Approved")
		closed = self.dar_at("Closed")
		self.as_user("mic")
		try:
			with self.assertRaises(ArchiveNotAllowedError):
				archive_record(DATA_ACCESS_REQUEST_DOCTYPE, open_request.name)
			self.assertEqual(archive_record(DATA_ACCESS_REQUEST_DOCTYPE, closed.name)["krcs_archived"], 1)
			self.assertEqual(archive_record(DATA_ACCESS_REQUEST_DOCTYPE, closed.name, 0)["krcs_archived"], 0)
		finally:
			frappe.set_user("Administrator")
		for key in ("his", "reception", "sysadmin"):
			self.as_user(key)
			try:
				with self.assertRaises(frappe.PermissionError, msg=key):
					archive_record(DATA_ACCESS_REQUEST_DOCTYPE, closed.name)
			finally:
				frappe.set_user("Administrator")
		closed.reload()
		closed.krcs_archived = 1
		with self.assertRaises(LockedFieldError):
			closed.save(ignore_permissions=True)

	def test_consent_statement(self):
		from onerc_healthcare.privacy.archive import ArchiveNotAllowedError, archive_record

		draft = self.make_statement("T-ST-ARCH-D")
		active = self.make_statement("T-ST-ARCH-A", activate=True)
		self.as_user("mic")
		try:
			with self.assertRaises(ArchiveNotAllowedError):
				archive_record(CONSENT_STATEMENT_DOCTYPE, draft.name)
			archive_record(CONSENT_STATEMENT_DOCTYPE, active.name)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(CONSENT_STATEMENT_DOCTYPE, active.name, "krcs_archived"), 1)
		self.as_user("sysadmin")
		try:
			with self.assertRaises(frappe.PermissionError):
				archive_record(CONSENT_STATEMENT_DOCTYPE, active.name, 0)
		finally:
			frappe.set_user("Administrator")
		draft.reload()
		draft.krcs_archived = 1
		with self.assertRaises(LockedFieldError):
			draft.save(ignore_permissions=True)


# =====================================================================================================
# Fix round 2: legacy extracts, DAR attachments, remaining read paths, L6 tightening, background export
# =====================================================================================================
class TestLegacyExtracts(PrivacyTestCase):
	"""H1 follow-up: extracts made before the fix (attached, owned by the HIS user) are secured on migrate."""

	def make_legacy(self, clear_field: bool = False):
		request = self.dar_at("Response Ready")
		file_name = frappe.db.get_value("File", {"file_url": request.extract_file}, "name")
		frappe.db.set_value(
			"File",
			file_name,
			{
				"attached_to_doctype": DATA_ACCESS_REQUEST_DOCTYPE,
				"attached_to_name": request.name,
				"attached_to_field": "extract_file",
				"owner": self.users["his"],
			},
			update_modified=False,
		)
		if clear_field:
			frappe.db.set_value(DATA_ACCESS_REQUEST_DOCTYPE, request.name, "extract_file", None)
		return request, file_name

	def downloadable_for(self, key: str, url: str):
		from frappe.core.doctype.file.utils import find_file_by_url

		self.as_user(key)
		try:
			return find_file_by_url(url)
		finally:
			frappe.set_user("Administrator")

	def test_install_secures_legacy_extracts(self):
		request, file_name = self.make_legacy()
		other, other_file = self.make_legacy(clear_field=True)
		url = request.extract_file
		self.assertIsNotNone(self.downloadable_for("reception", url), "legacy extract is open before the fix")
		privacy_setup.install()
		privacy_setup.install()  # idempotent
		for name in (file_name, other_file):
			row = frappe.db.get_value(
				"File", name, ["attached_to_doctype", "attached_to_name", "owner"], as_dict=True
			)
			self.assertEqual(
				(row.attached_to_doctype, row.attached_to_name, row.owner), (None, None, "Administrator")
			)
		self.assertIsNone(self.downloadable_for("reception", url))
		self.assertIsNone(self.downloadable_for("his", url))
		self.assertEqual(
			frappe.db.get_value(DATA_ACCESS_REQUEST_DOCTYPE, other.name, "extract_file"),
			frappe.db.get_value("File", other_file, "file_url"),
		)


class TestRequestAttachments(PrivacyTestCase):
	"""Only the extract is closed; other attachments of a request follow Frappe's rules."""

	def attach_as(self, key: str, request: str):
		self.as_user(key)
		try:
			return frappe.get_doc(
				{
					"doctype": "File",
					"file_name": f"T-authorisation-{key}.txt",
					"attached_to_doctype": DATA_ACCESS_REQUEST_DOCTYPE,
					"attached_to_name": request,
					"is_private": 1,
					"content": "T- scanned authorisation",
				}
			).insert()
		finally:
			frappe.set_user("Administrator")

	def test_his_and_mic_attach_and_read_others_follow_request(self):
		request = self.dar_at("Response Ready")
		extract = frappe.get_doc("File", {"file_url": request.extract_file})
		scans = [self.attach_as("his", request.name), self.attach_as("mic", request.name)]
		with self.assertRaises(frappe.PermissionError):
			self.attach_as("lab", request.name)
		for scan in scans:
			for key, allowed in {
				"his": True,
				"mic": True,
				"reception": True,
				"lab": False,
				"sysadmin": False,
			}.items():
				self.assertEqual(
					frappe.has_permission("File", "read", doc=scan, user=self.users[key]),
					allowed,
					f"{scan.name}/{key}",
				)
		for key in ("his", "mic", "reception"):
			self.assertFalse(frappe.has_permission("File", "read", doc=extract, user=self.users[key]), key)

	def test_mic_cannot_edit_intake_fields(self):
		doc = self.make_dar()
		self.as_user("mic")
		try:
			doc = frappe.get_doc(DATA_ACCESS_REQUEST_DOCTYPE, doc.name)
			doc.save()  # nothing changed: allowed
			doc.details = "T- changed by the MIC"
			with self.assertRaises(LockedFieldError):
				doc.save()
		finally:
			frappe.set_user("Administrator")


class TestMoreReadPaths(PrivacyTestCase):
	"""M4 follow-up: list APIs, link fetch, print preview, Data Import export."""

	def setUp(self) -> None:
		super().setUp()
		frappe.flags.krcs_list_logged = False

	def tearDown(self) -> None:
		frappe.flags.krcs_list_logged = False
		for k in ("filename", "filecontent", "type"):
			frappe.local.response.pop(k, None)
		super().tearDown()

	def test_client_get_list(self):
		patient = make_patient().name
		self.as_user("reception")
		read_log.get_list("Patient", filters={"name": patient})
		read_log.get_list("Medical Department", filters={"name": DEPT_OUTPATIENT})
		frappe.set_user("Administrator")
		rows = self.entries(user=self.users["reception"])
		self.assertEqual([(r["action"], r["patient"]) for r in rows], [("List", patient)])
		self.assertIn("Rows: 1", rows[0]["details"])

	def test_reportview_get_list(self):
		patient = make_patient().name
		self.as_user("reception")
		try:
			frappe.local.form_dict = frappe._dict(
				doctype="Patient", fields=json.dumps(["name"]), filters=json.dumps({"name": patient})
			)
			read_log.reportview_get_list()
		finally:
			frappe.set_user("Administrator")
		rows = self.entries(user=self.users["reception"])
		self.assertEqual([(r["action"], r["patient"]) for r in rows], [("List", patient)])

	def test_rest_list_once_per_request(self):
		patient = make_patient().name
		response = SimpleNamespace(
			status_code=200, get_data=lambda as_text=False: json.dumps({"data": [{"name": patient}]})
		)

		def hit(path):
			self.as_user("reception")
			try:
				frappe.local.form_dict = frappe._dict(filters=json.dumps({"name": patient}))
				read_log.after_request(response=response, request=SimpleNamespace(method="GET", path=path))
			finally:
				frappe.set_user("Administrator")

		hit("/api/resource/Patient")
		hit("/api/v2/document/Medical%20Department")  # not logged
		rows = self.entries(user=self.users["reception"], action="List")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["patient"], patient)
		self.assertIn("Rows: 1", rows[0]["details"])
		frappe.flags.krcs_list_logged = True  # a wrapper already logged during this request
		hit("/api/v2/document/Patient")
		self.assertEqual(len(self.entries(user=self.users["reception"], action="List")), 1)

	def test_validate_link_and_fetch(self):
		patient = make_patient().name
		had = hasattr(frappe.local, "request")
		old = getattr(frappe.local, "request", None)
		frappe.local.request = SimpleNamespace(
			method="POST", path="/api/method/frappe.client.validate_link_and_fetch"
		)
		try:
			self.as_user("reception")
			result = read_log.validate_link_and_fetch("Patient", patient, fields_to_fetch=["patient_name"])
			read_log.validate_link_and_fetch("Patient", patient)  # no fetch: link check only
		finally:
			if had:
				frappe.local.request = old
			else:
				del frappe.local.request
			frappe.set_user("Administrator")
		self.assertTrue(result)
		rows = self.entries(user=self.users["reception"])
		self.assertEqual([(r["action"], r["patient"]) for r in rows], [("Read (API)", patient)])
		self.assertIn("patient_name", rows[0]["details"])

	def test_print_preview(self):
		patient = make_patient().name
		self.as_user("reception")
		try:
			read_log.get_html_and_style("Patient", patient)
		finally:
			frappe.set_user("Administrator")
		rows = self.entries(user=self.users["reception"])
		self.assertEqual(
			[(r["action"], r["reference_name"], r["patient"]) for r in rows], [("Print", patient, patient)]
		)

	def test_data_import_export_refused_for_emr_roles(self):
		patient = make_patient().name
		for key in ("his", "mic", "reception"):
			self.as_user(key)
			try:
				with self.assertRaises(frappe.PermissionError, msg=key):
					read_log.download_template(
						"Patient", export_records="all", export_fields=json.dumps({"Patient": ["name"]})
					)
			finally:
				frappe.set_user("Administrator")
		self.assertEqual(self.entries(action="Export"), [])
		manager = make_user("dp-sysmanager", ["System Manager"])
		frappe.set_user(manager)
		try:
			read_log.download_template(
				"Patient",
				export_records="by_filter",
				export_fields=json.dumps({"Patient": ["name"]}),
				export_filters=json.dumps({"name": patient}),
			)
		finally:
			frappe.set_user("Administrator")
		rows = self.entries(user=manager, action="Export")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["patient"], patient)


class TestFixRound2Logging(PrivacyTestCase):
	def test_readable_report_does_not_vouch_for_a_document(self):
		from onerc_healthcare.tests.visit_factories import make_vital_signs

		vitals = make_vital_signs(make_patient().name).name
		self.assertFalse(frappe.has_permission("Vital Signs", "read", doc=vitals, user=self.users["his"]))
		self.as_user("his")
		try:
			with self.assertRaises(frappe.PermissionError):
				access_log.make_access_log(
					doctype="Vital Signs",
					document=vitals,
					report_name=REPORT_CONSENT_EXCEPTIONS,
					method="Print",
				)
			access_log.make_access_log(report_name=REPORT_CONSENT_EXCEPTIONS, method="Print", file_type="PDF")
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.count("Access Log", {"reference_document": vitals}), 0)

	def test_background_export_logged_once(self):
		import csv as _csv

		from frappe.desk.query_report import run_export_query_job

		patient = make_patient().name
		self.as_user("mic")
		try:
			frappe.local.form_dict = frappe._dict(
				report_name=REPORT_CONSENT_EXCEPTIONS,
				file_format_type="CSV",
				filters=json.dumps({"patient": patient}),
				export_in_background=1,
			)
			with patch.object(frappe, "enqueue", lambda *a, **k: None):  # queued, not run
				read_log.query_report_export_query()
			# The worker: job context, no request flags.
			frappe.local.job = frappe._dict(method="frappe.desk.query_report.run_export_query_job")
			with patch("frappe.desk.utils.send_report_email", lambda *a, **k: None):
				run_export_query_job(
					"t@test.krcs",
					frappe._dict(
						report_name=REPORT_CONSENT_EXCEPTIONS,
						file_format_type="CSV",
						filters={"patient": patient},
						visible_idx=[],
					),
					{"delimiter": ",", "quoting": _csv.QUOTE_NONNUMERIC, "decimal_sep": "."},
				)
		finally:
			if hasattr(frappe.local, "job"):
				del frappe.local.job
			frappe.set_user("Administrator")
		rows = self.entries(user=self.users["mic"])
		self.assertEqual([r["action"] for r in rows], ["Export"])
		self.assertIn("background export", rows[0]["details"])
