# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Data Access Request lifecycle (FR-38, BR-12, C-3; SDD §5.10). Status changes only through here (D8).

| Action              | From                       | To                | Roles                              | Requires                         |
|---------------------|----------------------------|-------------------|------------------------------------|----------------------------------|
| `create` (insert)   | -                          | Received          | Nurse Receptionist, HIS Officer    | requester; due date set          |
| `verify_identity`   | Received                   | Identity Verified | Nurse Receptionist, HIS Officer    | identity_method (+ evidence for a representative) |
| `approve`           | Identity Verified          | Approved          | Medical In-Charge                  | -                                |
| `reject`            | Identity Verified          | Rejected          | Medical In-Charge                  | decision_reason                  |
| `prepare_extract`   | Approved (Access)          | Response Ready    | HIS Officer                        | server builds a private HTML extract; logged "Export" |
| `record_correction` | Approved (Correction)      | Response Ready    | HIS Officer                        | corrected record of this patient + its Version entry  |
| `respond`           | Response Ready, Rejected   | Closed            | HIS Officer, Medical In-Charge     | response_date, response_method   |

- `due_date` = received date + Data Protection Settings `response_days` (default 7, provisional).
- Overdue: `due_date` before today and status not Closed (`is_overdue`).
- The correction itself is made on the record by the role that owns it; the change stays in that
  record's Version history (SDD §5.10). This request only links it.
- Administrator passes every role check. Insert with ``ignore_permissions`` (system code) skips the
  create role check.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, getdate, now_datetime, today

from onerc_healthcare.constants import DATA_ACCESS_REQUEST_DOCTYPE, ROLE_HIS, ROLE_MIC, ROLE_RECEPTIONIST
from onerc_healthcare.privacy.lifecycle import (
	IllegalTransitionError,
	Transition,
	append_log,
	check_from_status,
	check_role,
	clean_text,
	has_any_role,
	save_from_workflow,
)

SETTINGS_DOCTYPE = "Data Protection Settings"
DEFAULT_RESPONSE_DAYS = 7  # provisional (OI-14)

STATUS_RECEIVED = "Received"
STATUS_IDENTITY_VERIFIED = "Identity Verified"
STATUS_APPROVED = "Approved"
STATUS_REJECTED = "Rejected"
STATUS_RESPONSE_READY = "Response Ready"
STATUS_CLOSED = "Closed"
STATUSES = (
	STATUS_RECEIVED,
	STATUS_IDENTITY_VERIFIED,
	STATUS_APPROVED,
	STATUS_REJECTED,
	STATUS_RESPONSE_READY,
	STATUS_CLOSED,
)

TYPE_ACCESS = "Access"
TYPE_CORRECTION = "Correction"
REQUESTER_PATIENT = "Patient"
REQUESTER_REPRESENTATIVE = "Authorised Representative"

ACTION_CREATE = "create"
ACTION_VERIFY_IDENTITY = "verify_identity"
ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
ACTION_PREPARE_EXTRACT = "prepare_extract"
ACTION_RECORD_CORRECTION = "record_correction"
ACTION_RESPOND = "respond"

INTAKE_ROLES = (ROLE_RECEPTIONIST, ROLE_HIS)

TRANSITIONS: dict[str, Transition] = {
	ACTION_CREATE: Transition(_("Receive Request"), (), STATUS_RECEIVED, INTAKE_ROLES),
	ACTION_VERIFY_IDENTITY: Transition(
		_("Verify Identity"),
		(STATUS_RECEIVED,),
		STATUS_IDENTITY_VERIFIED,
		INTAKE_ROLES,
		required=("identity_method",),
	),
	ACTION_APPROVE: Transition(_("Approve"), (STATUS_IDENTITY_VERIFIED,), STATUS_APPROVED, (ROLE_MIC,)),
	ACTION_REJECT: Transition(
		_("Reject"), (STATUS_IDENTITY_VERIFIED,), STATUS_REJECTED, (ROLE_MIC,), required=("decision_reason",)
	),
	ACTION_PREPARE_EXTRACT: Transition(
		_("Prepare Extract"),
		(STATUS_APPROVED,),
		STATUS_RESPONSE_READY,
		(ROLE_HIS,),
		request_type=TYPE_ACCESS,
	),
	ACTION_RECORD_CORRECTION: Transition(
		_("Record Correction"),
		(STATUS_APPROVED,),
		STATUS_RESPONSE_READY,
		(ROLE_HIS,),
		required=("corrected_doctype", "corrected_record", "correction_version"),
		request_type=TYPE_CORRECTION,
	),
	ACTION_RESPOND: Transition(
		_("Record Response"),
		(STATUS_RESPONSE_READY, STATUS_REJECTED),
		STATUS_CLOSED,
		(ROLE_HIS, ROLE_MIC),
		required=("response_date", "response_method"),
	),
}

# Fields set only by the lifecycle (refused in a normal save after insert).
PROTECTED_FIELDS = (
	"status",
	"received_date",
	"due_date",
	"identity_method",
	"identity_evidence",
	"identity_verified_by",
	"identity_verified_at",
	"decision_reason",
	"decided_by",
	"decided_at",
	"extract_file",
	"corrected_doctype",
	"corrected_record",
	"correction_version",
	"response_date",
	"response_method",
	"responded_by",
)
# Intake fields: editable by hand only while the request is Received.
INTAKE_FIELDS = (
	"patient",
	"request_type",
	"details",
	"requester_type",
	"requester_name",
	"relationship",
	"contact",
)


class RequestTypeMismatchError(IllegalTransitionError):
	"""The action belongs to the other request type (Access vs Correction)."""


def get_response_days() -> int:
	days = cint(frappe.db.get_single_value(SETTINGS_DOCTYPE, "response_days"))
	return days if days > 0 else DEFAULT_RESPONSE_DAYS


def is_overdue(doc, on_date=None) -> bool:
	"""Due date passed and the request is not Closed."""
	due = doc.get("due_date")
	if not due or doc.get("status") == STATUS_CLOSED:
		return False
	return getdate(due) < getdate(on_date or today())


def option_values(fieldname: str) -> list[str]:
	df = frappe.get_meta(DATA_ACCESS_REQUEST_DOCTYPE).get_field(fieldname)
	return [o for o in (df.options or "").split("\n") if o] if df else []


def _check_request_type(rule: Transition, doc) -> None:
	if rule.request_type and doc.request_type != rule.request_type:
		frappe.throw(
			_("{0} applies to {1} requests only; this is a {2} request.").format(
				frappe.bold(rule.label), _(rule.request_type), _(doc.request_type)
			),
			RequestTypeMismatchError,
		)


def transition(request: str, action: str, system: bool = False, **kwargs):
	"""Apply `action` to Data Access Request `request` as the current user; return the saved doc."""
	rule = TRANSITIONS.get(action)
	if not rule or action == ACTION_CREATE:
		frappe.throw(_("Unknown data access request action: {0}").format(action), IllegalTransitionError)
	doc = frappe.get_doc(DATA_ACCESS_REQUEST_DOCTYPE, request, for_update=True)
	check_role(rule, doc, system)
	check_from_status(rule, doc)
	_check_request_type(rule, doc)
	comment = clean_text(kwargs.pop("comment", None), _("Comment"))

	from_status = doc.status
	log_comment = _HANDLERS[action](doc, rule, comment=comment, **kwargs)
	doc.status = rule.to_status
	append_log(doc, from_status, rule.to_status, action, log_comment or comment)
	save_from_workflow(doc)
	return doc


def _verify_identity(doc, rule, comment=None, **kwargs) -> str | None:
	method = (kwargs.get("identity_method") or "").strip()
	if method not in option_values("identity_method"):
		frappe.throw(
			_("Choose how the requester's identity was verified."),
			IllegalTransitionError,
			title=_("Missing Value"),
		)
	evidence = clean_text(
		kwargs.get("identity_evidence"),
		_("Identity Evidence"),
		required=doc.requester_type == REQUESTER_REPRESENTATIVE,
	)
	doc.identity_method = method
	doc.identity_evidence = evidence
	doc.identity_verified_by = frappe.session.user
	doc.identity_verified_at = now_datetime()
	return comment


def _approve(doc, rule, comment=None, **kwargs) -> str | None:
	doc.decision_reason = clean_text(kwargs.get("decision_reason"), _("Decision Reason")) or comment
	doc.decided_by = frappe.session.user
	doc.decided_at = now_datetime()
	return doc.decision_reason


def _reject(doc, rule, comment=None, **kwargs) -> str | None:
	doc.decision_reason = clean_text(kwargs.get("decision_reason"), _("Decision Reason"), required=True)
	doc.decided_by = frappe.session.user
	doc.decided_at = now_datetime()
	return doc.decision_reason


def _prepare_extract(doc, rule, comment=None, **kwargs) -> str | None:
	from onerc_healthcare.privacy.extract import create_extract_file

	file_doc = create_extract_file(doc)
	doc.extract_file = file_doc.file_url
	return comment


def _record_correction(doc, rule, comment=None, **kwargs) -> str | None:
	from onerc_healthcare.privacy.access_log import resolve_patient

	corrected_doctype = (kwargs.get("corrected_doctype") or "").strip()
	corrected_record = (kwargs.get("corrected_record") or "").strip()
	version = (kwargs.get("correction_version") or "").strip()
	if not (corrected_doctype and corrected_record and version):
		frappe.throw(
			_("Link the corrected record and the Version entry that holds the correction."),
			IllegalTransitionError,
			title=_("Missing Value"),
		)
	if not frappe.db.exists("DocType", corrected_doctype) or not frappe.db.exists(
		corrected_doctype, corrected_record
	):
		frappe.throw(
			_("{0} {1} does not exist.").format(corrected_doctype, corrected_record), IllegalTransitionError
		)
	if resolve_patient(corrected_doctype, corrected_record) != doc.patient:
		frappe.throw(
			_("{0} {1} is not a record of patient {2}.").format(
				_(corrected_doctype), corrected_record, doc.patient
			),
			IllegalTransitionError,
		)
	row = frappe.db.get_value("Version", version, ["ref_doctype", "docname", "creation"], as_dict=True)
	if not row or row.ref_doctype != corrected_doctype or row.docname != corrected_record:
		frappe.throw(
			_("Version {0} is not a change of {1} {2}.").format(
				version, _(corrected_doctype), corrected_record
			),
			IllegalTransitionError,
		)
	if row.creation < doc.creation:
		frappe.throw(
			_("Version {0} was recorded before this request was received.").format(version),
			IllegalTransitionError,
		)
	doc.corrected_doctype = corrected_doctype
	doc.corrected_record = corrected_record
	doc.correction_version = version
	return comment


def _respond(doc, rule, comment=None, **kwargs) -> str | None:
	response_date = kwargs.get("response_date")
	method = (kwargs.get("response_method") or "").strip()
	if not response_date or method not in option_values("response_method"):
		frappe.throw(
			_("Enter the response date and how the response was given."),
			IllegalTransitionError,
			title=_("Missing Value"),
		)
	response_date = getdate(response_date)
	if response_date > getdate(today()):
		frappe.throw(_("The response date cannot be in the future."), IllegalTransitionError)
	if doc.received_date and response_date < getdate(doc.received_date):
		frappe.throw(
			_("The response date cannot be before the request was received."), IllegalTransitionError
		)
	doc.response_date = response_date
	doc.response_method = method
	doc.responded_by = frappe.session.user
	return comment


_HANDLERS = {
	ACTION_VERIFY_IDENTITY: _verify_identity,
	ACTION_APPROVE: _approve,
	ACTION_REJECT: _reject,
	ACTION_PREPARE_EXTRACT: _prepare_extract,
	ACTION_RECORD_CORRECTION: _record_correction,
	ACTION_RESPOND: _respond,
}


def get_available_actions(doc, user: str | None = None) -> list[dict]:
	"""Actions `user` may perform on `doc` now (form buttons)."""
	out = []
	for action, rule in TRANSITIONS.items():
		if action == ACTION_CREATE or doc.status not in rule.from_statuses:
			continue
		if rule.request_type and doc.request_type != rule.request_type:
			continue
		if not has_any_role(rule.roles, user):
			continue
		out.append({"action": action, "label": rule.label, "required": list(rule.required)})
	return out


@frappe.whitelist(methods=["POST"])
def apply_action(
	request: str,
	action: str,
	identity_method: str | None = None,
	identity_evidence: str | None = None,
	decision_reason: str | None = None,
	corrected_doctype: str | None = None,
	corrected_record: str | None = None,
	correction_version: str | None = None,
	response_date: str | None = None,
	response_method: str | None = None,
	comment: str | None = None,
) -> dict:
	"""Perform a request action as the current user (form buttons)."""
	doc = transition(
		request,
		action,
		identity_method=identity_method,
		identity_evidence=identity_evidence,
		decision_reason=decision_reason,
		corrected_doctype=corrected_doctype,
		corrected_record=corrected_record,
		correction_version=correction_version,
		response_date=response_date,
		response_method=response_method,
		comment=comment,
	)
	return {"name": doc.name, "status": doc.status, "extract_file": doc.extract_file}


@frappe.whitelist(methods=["GET"])
def get_correction_versions(request: str, corrected_doctype: str, corrected_record: str) -> list[dict]:
	"""Version entries of the corrected record made since the request was received (metadata only)."""
	from onerc_healthcare.privacy.access_log import resolve_patient

	doc = frappe.get_doc(DATA_ACCESS_REQUEST_DOCTYPE, request)
	doc.check_permission("read")
	if resolve_patient(corrected_doctype, corrected_record) != doc.patient:
		return []
	return frappe.get_all(
		"Version",
		filters={
			"ref_doctype": corrected_doctype,
			"docname": corrected_record,
			"creation": (">=", doc.creation),
		},
		fields=["name", "owner", "creation"],
		order_by="creation desc",
		limit=20,
	)
