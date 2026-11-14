# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Referral lifecycle (FR-12, BR-14, SDD §4.2 Referral, §5.3; D8 pattern).

Status changes only through `transition()` (whitelisted as `apply_action`). Every change appends a row
to the append-only `status_log`.

| Action | From | To | Roles |
|---|---|---|---|
| save | (new) | Draft | Clinical Officer, Mental Health Officer |
| send | Draft | Sent | Clinical Officer, Mental Health Officer |
| receive | Sent (internal only) | Received | system: the target visit starts its consultation |
| record_outcome | Sent, Received | Completed | Clinical Officer, Mental Health Officer, HIS Officer |
| mark_lost | Sent | Lost to Follow-up | Clinical Officer, Mental Health Officer, Medical In-Charge; reason |
| cancel | Draft | Cancelled | Clinical Officer, Mental Health Officer |

`send` runs in one transaction:

1. completes the source consultation (diagnosis rule, no unsent lab rows; prescriptions are released
   and raise `prescribe`);
2. applies the visit engine's `refer` to the source visit (the reason is the log comment), which closes
   it as Referred;
3. for an internal referral, registers a new Patient Visit at the same facility and raises
   `receive_referral` on it (Registered → Routed to Department, no triage), then links it as
   `target_visit`. The new visit is allowed because the source visit is already closed.

`receive` is applied when the target visit enters In Consultation (`visit_on_update`, a Patient Visit
`on_update` hook, and `consultation.start_consultation`).

`send` works while the visit is In Consultation, Awaiting Laboratory or Awaiting Pharmacy (the engine's
`refer` rule). Prescriptions released by the completion raise `prescribe` before `refer`, so the Referred
visit keeps its open laboratory and pharmacy tasks; `lab_results_ready` and `dispensed` clear them later.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe import _
from frappe.model import no_value_fields, table_fields
from frappe.utils import cint, get_datetime, getdate, now_datetime, today

from onerc_healthcare.clinical.utils import CONSULTING_ROLES, has_any_role, lock_visit_and_encounter
from onerc_healthcare.constants import (
	ENCOUNTER_DOCTYPE,
	ENCOUNTER_VISIT_FIELD,
	REFERRAL_DOCTYPE,
	REFERRAL_FACILITY_DOCTYPE,
	ROLE_HIS,
	ROLE_MIC,
	VISIT_CLOSED_STATUSES,
	VISIT_DOCTYPE,
	VISIT_STATUS_IN_CONSULTATION,
)
from onerc_healthcare.visit import engine, tasks

STATUS_NEW = ""
STATUS_DRAFT = "Draft"
STATUS_SENT = "Sent"
STATUS_RECEIVED = "Received"
STATUS_COMPLETED = "Completed"
STATUS_LOST = "Lost to Follow-up"
STATUS_CANCELLED = "Cancelled"
STATUSES = (STATUS_DRAFT, STATUS_SENT, STATUS_RECEIVED, STATUS_COMPLETED, STATUS_LOST, STATUS_CANCELLED)
OPEN_STATUSES = (STATUS_SENT, STATUS_RECEIVED)

TYPE_INTERNAL = "Internal"
TYPE_EXTERNAL = "External"

ACTION_SAVE = "save"
ACTION_SEND = "send"
ACTION_RECEIVE = "receive"
ACTION_RECORD_OUTCOME = "record_outcome"
ACTION_MARK_LOST = "mark_lost"
ACTION_CANCEL = "cancel"

MAX_COMMENT_LENGTH = engine.MAX_COMMENT_LENGTH
# Fields only the state machine changes.
PROTECTED_FIELDS = ("status", "sent_at", "target_visit", "outcome", "outcome_date")
# Fields set from the source consultation.
SOURCE_FIELDS = ("patient", "source_visit", "source_encounter")
LOG_FIELDS = ("from_status", "to_status", "action", "user", "timestamp", "comment")
# The archive flag added to every clinical DocType by security/setup.py (NFR-9) and who may set it (L5).
ARCHIVE_FIELDS = ("krcs_archived", "krcs_archive_section")
ARCHIVE_ROLES = (ROLE_MIC,)


class IllegalReferralTransitionError(frappe.ValidationError):
	"""The referral action is not allowed in the referral's current status or type."""


class ReferralLockedError(frappe.ValidationError):
	"""A referral field or its history was changed outside the state machine."""


@dataclass(frozen=True)
class ReferralTransition:
	label: str
	from_statuses: tuple[str, ...]
	to_status: str
	# Empty: system event (``system=True`` or Administrator only).
	roles: tuple[str, ...] = ()
	needs_comment: bool = False
	needs_outcome: bool = False
	internal_only: bool = False

	@property
	def is_system(self) -> bool:
		return not self.roles


TRANSITIONS: dict[str, ReferralTransition] = {
	ACTION_SAVE: ReferralTransition(_("Save"), (STATUS_NEW,), STATUS_DRAFT, CONSULTING_ROLES),
	ACTION_SEND: ReferralTransition(_("Send Referral"), (STATUS_DRAFT,), STATUS_SENT, CONSULTING_ROLES),
	ACTION_RECEIVE: ReferralTransition(
		_("Received"), (STATUS_SENT,), STATUS_RECEIVED, (), internal_only=True
	),
	ACTION_RECORD_OUTCOME: ReferralTransition(
		_("Record Outcome"),
		(STATUS_SENT, STATUS_RECEIVED),
		STATUS_COMPLETED,
		(*CONSULTING_ROLES, ROLE_HIS),
		needs_outcome=True,
	),
	ACTION_MARK_LOST: ReferralTransition(
		_("Mark Lost to Follow-up"),
		(STATUS_SENT,),
		STATUS_LOST,
		(*CONSULTING_ROLES, ROLE_MIC),
		needs_comment=True,
	),
	ACTION_CANCEL: ReferralTransition(
		_("Cancel Referral"), (STATUS_DRAFT,), STATUS_CANCELLED, CONSULTING_ROLES
	),
}


# --------------------------------------------------------------------------- state machine


def check_role(rule: ReferralTransition, system: bool = False) -> None:
	if system or frappe.session.user == "Administrator":
		return
	if rule.is_system:
		frappe.throw(
			_("{0} is a system event and cannot be performed by a user.").format(frappe.bold(rule.label)),
			frappe.PermissionError,
		)
	if not has_any_role(rule.roles):
		frappe.throw(
			_("You are not allowed to perform {0}. Required role: {1}.").format(
				frappe.bold(rule.label), ", ".join(_(r) for r in rule.roles)
			),
			frappe.PermissionError,
		)


def transition(
	referral: str,
	action: str,
	comment: str | None = None,
	outcome: str | None = None,
	outcome_date: str | None = None,
	system: bool = False,
):
	"""Apply `action` to Referral `referral` and return the saved document.

	Raises ``frappe.PermissionError`` for a wrong role and ``IllegalReferralTransitionError`` for an
	action that is not allowed from the current status (or type).
	"""
	rule = TRANSITIONS.get(action)
	if not rule:
		frappe.throw(_("Unknown referral action: {0}").format(action), IllegalReferralTransitionError)
	doc = frappe.get_doc(REFERRAL_DOCTYPE, referral, for_update=True)
	check_role(rule, system)
	if not system and frappe.session.user != "Administrator" and not doc.has_permission("read"):
		frappe.throw(_("You do not have access to referral {0}.").format(doc.name), frappe.PermissionError)
	if doc.status not in rule.from_statuses:
		frappe.throw(
			_("Action {0} is not allowed for referral {1} in status {2}.").format(
				frappe.bold(rule.label), doc.name, frappe.bold(_(doc.status))
			),
			IllegalReferralTransitionError,
		)
	if rule.internal_only and doc.referral_type != TYPE_INTERNAL:
		frappe.throw(
			_("Action {0} applies to internal referrals only.").format(frappe.bold(rule.label)),
			IllegalReferralTransitionError,
		)
	comment = _clean(comment, _("reason"))
	outcome = _clean(outcome, _("outcome"))
	if rule.needs_comment and not comment:
		frappe.throw(
			_("Enter a reason for {0}.").format(frappe.bold(rule.label)),
			IllegalReferralTransitionError,
			title=_("Reason Required"),
		)
	if rule.needs_outcome and not outcome:
		frappe.throw(
			_("Enter the outcome of the referral."),
			IllegalReferralTransitionError,
			title=_("Outcome Required"),
		)

	from_status = doc.status
	if action == ACTION_SEND:
		_send(doc, system=system)
	elif action == ACTION_RECORD_OUTCOME:
		doc.outcome = outcome
		doc.outcome_date = getdate(outcome_date) if outcome_date else getdate(today())
		if doc.outcome_date > getdate(today()):
			frappe.throw(_("The outcome date cannot be in the future."), IllegalReferralTransitionError)
		comment = comment or outcome
	elif action == ACTION_MARK_LOST:
		doc.outcome_date = getdate(today())

	doc.status = rule.to_status
	_append_log(doc, from_status, rule.to_status, action, comment)
	_save(doc)
	return doc


def _clean(value: str | None, label: str) -> str | None:
	value = (value or "").strip() or None
	if value and len(value) > MAX_COMMENT_LENGTH:
		frappe.throw(
			_("The {0} is {1} characters long; the limit is {2}.").format(
				label, len(value), MAX_COMMENT_LENGTH
			),
			IllegalReferralTransitionError,
		)
	return value


def _append_log(doc, from_status: str, to_status: str, action: str, comment: str | None = None) -> None:
	doc.append(
		"status_log",
		{
			"from_status": from_status,
			"to_status": to_status,
			"action": action,
			"user": frappe.session.user,
			"timestamp": now_datetime(),
			"comment": comment,
		},
	)


def _save(doc) -> None:
	doc.flags.from_referral_action = True
	try:
		doc.save(ignore_permissions=True)
	finally:
		doc.flags.from_referral_action = False


def _send(doc, system: bool = False) -> None:
	"""Complete the consultation, refer the source visit, and for an internal referral open the new visit."""
	from onerc_healthcare.clinical.consultation import submit_encounter

	validate_destination(doc)
	visit_doc, encounter = lock_visit_and_encounter(doc.source_encounter)  # Visit -> Encounter (M7)
	visit = visit_doc.name
	refer_rule = engine.TRANSITIONS[engine.ACTION_REFER]
	if visit_doc.status not in refer_rule.from_statuses:
		frappe.throw(
			_("Visit {0} is {1}; a referral is sent during the consultation.").format(
				visit, frappe.bold(_(visit_doc.status))
			),
			IllegalReferralTransitionError,
			title=_("Not Possible Now"),
		)
	medication_requests = submit_encounter(encounter) if encounter.docstatus == 0 else []
	if visit_doc.get("encounter") != encounter.name:
		engine.link_encounter(visit, encounter.name)
	if medication_requests:
		# The Referred visit keeps the pharmacy task; `dispensed` clears it later (engine invariant 1).
		tasks.raise_event(visit, engine.ACTION_PRESCRIBE)
	engine.transition(visit, engine.ACTION_REFER, comment=doc.reason, system=system)
	doc.sent_at = now_datetime()

	if doc.referral_type == TYPE_INTERNAL:
		new_visit = frappe.get_doc(
			{"doctype": VISIT_DOCTYPE, "patient": doc.patient, "facility": visit_doc.facility}
		)
		new_visit.insert(ignore_permissions=True)
		doc.target_visit = new_visit.name
		tasks.raise_event(
			new_visit.name, engine.ACTION_RECEIVE_REFERRAL, department=doc.to_department, referral=doc.name
		)


def validate_destination(doc) -> None:
	if doc.referral_type == TYPE_INTERNAL:
		doc.to_facility = doc.to_facility_name = None
		if not doc.to_department:
			frappe.throw(_("Select the department to refer the patient to."), frappe.MandatoryError)
		if doc.to_department in engine.NON_ROUTABLE_DEPARTMENTS:
			frappe.throw(
				_("A patient cannot be referred to {0}. Choose the clinical department.").format(
					frappe.bold(_(doc.to_department))
				),
				IllegalReferralTransitionError,
			)
		if not frappe.db.exists("Medical Department", doc.to_department):
			frappe.throw(_("Medical Department {0} does not exist.").format(doc.to_department))
	elif doc.referral_type == TYPE_EXTERNAL:
		doc.to_department = None
		doc.to_facility_name = (doc.to_facility_name or "").strip() or None
		if not doc.to_facility and not doc.to_facility_name:
			frappe.throw(
				_("Select the facility to refer the patient to, or enter its name."), frappe.MandatoryError
			)
		if doc.to_facility and not frappe.db.get_value(
			REFERRAL_FACILITY_DOCTYPE, doc.to_facility, "is_active"
		):
			frappe.throw(_("Referral Facility {0} is not active.").format(frappe.bold(doc.to_facility)))
	else:
		frappe.throw(_("Choose the referral type."), frappe.MandatoryError)


def get_available_actions(doc, user: str | None = None) -> list[dict]:
	"""User actions `user` may perform on `doc` now (form buttons). System events are excluded."""
	user = user or frappe.session.user
	actions = []
	for action, rule in TRANSITIONS.items():
		if rule.is_system or action == ACTION_SAVE or doc.status not in rule.from_statuses:
			continue
		if rule.internal_only and doc.referral_type != TYPE_INTERNAL:
			continue
		if not has_any_role(rule.roles, user):
			continue
		actions.append(
			{
				"action": action,
				"label": rule.label,
				"needs_comment": rule.needs_comment,
				"needs_outcome": rule.needs_outcome,
			}
		)
	return actions


# --------------------------------------------------------------------------- receive


def receive_for_visit(visit_doc) -> bool:
	"""Mark the visit's source referral Received once the target visit is In Consultation."""
	referral = visit_doc.get("referred_from")
	if not referral or visit_doc.status != VISIT_STATUS_IN_CONSULTATION:
		return False
	row = frappe.db.get_value(REFERRAL_DOCTYPE, referral, ["status", "referral_type"], as_dict=True)
	if not row or row.status != STATUS_SENT or row.referral_type != TYPE_INTERNAL:
		return False
	transition(referral, ACTION_RECEIVE, system=True)
	return True


def visit_on_update(doc, method=None) -> None:
	"""Patient Visit `on_update` (integrator hook): receive the referral when its visit starts consultation."""
	receive_for_visit(doc)


# --------------------------------------------------------------------------- endpoints


@frappe.whitelist(methods=["POST"])
def create_referral(
	encounter: str,
	referral_type: str,
	reason: str,
	to_department: str | None = None,
	to_facility: str | None = None,
	to_facility_name: str | None = None,
	urgency: str | None = None,
	clinical_summary: str | None = None,
	send: bool = False,
) -> dict:
	"""Create a Draft referral from the consultation; with `send`, send it at once."""
	frappe.has_permission(REFERRAL_DOCTYPE, "create", throw=True)
	frappe.has_permission(ENCOUNTER_DOCTYPE, "read", doc=encounter, throw=True)
	doc = frappe.get_doc(
		{
			"doctype": REFERRAL_DOCTYPE,
			"source_encounter": encounter,
			"referral_type": referral_type,
			"reason": reason,
			"to_department": to_department,
			"to_facility": to_facility,
			"to_facility_name": to_facility_name,
			"urgency": urgency or "Routine",
			"clinical_summary": clinical_summary,
		}
	).insert()
	if send:
		doc = transition(doc.name, ACTION_SEND)
	return _summary(doc)


@frappe.whitelist(methods=["POST"])
def apply_action(
	referral: str,
	action: str,
	comment: str | None = None,
	outcome: str | None = None,
	outcome_date: str | None = None,
) -> dict:
	"""Perform a referral action as the current user (form buttons)."""
	doc = transition(referral, action, comment=comment, outcome=outcome, outcome_date=outcome_date)
	return _summary(doc)


def _summary(doc) -> dict:
	return {
		"name": doc.name,
		"status": doc.status,
		"target_visit": doc.target_visit,
		"source_visit_status": frappe.db.get_value(VISIT_DOCTYPE, doc.source_visit, "status"),
	}


# --------------------------------------------------------------------------- controller helpers


def before_insert(doc) -> None:
	"""`save` action: role check and server-owned fields reset."""
	check_role(TRANSITIONS[ACTION_SAVE], system=bool(doc.flags.krcs_system))
	for field in PROTECTED_FIELDS:
		doc.set(field, None)
	doc.status = STATUS_DRAFT
	doc.set("status_log", [])
	_append_log(doc, STATUS_NEW, STATUS_DRAFT, ACTION_SAVE)


def set_source(doc) -> None:
	"""Patient and visit come from the source consultation."""
	enc = frappe.db.get_value(
		ENCOUNTER_DOCTYPE,
		doc.source_encounter,
		["patient", ENCOUNTER_VISIT_FIELD, "docstatus"],
		as_dict=True,
	)
	if not enc or enc.docstatus == 2:
		frappe.throw(
			_("Consultation {0} does not exist.").format(doc.source_encounter), frappe.ValidationError
		)
	visit = enc.get(ENCOUNTER_VISIT_FIELD)
	if not visit:
		frappe.throw(_("Consultation {0} has no visit.").format(doc.source_encounter))
	doc.patient = enc.patient
	doc.source_visit = visit


def validate_new(doc) -> None:
	from onerc_healthcare.visit.patients import assert_patient_active

	assert_patient_active(doc.patient)
	status = frappe.db.get_value(VISIT_DOCTYPE, doc.source_visit, "status")
	if status in VISIT_CLOSED_STATUSES:
		frappe.throw(
			_("Visit {0} is closed ({1}); a referral is made during the consultation.").format(
				doc.source_visit, _(status)
			),
			IllegalReferralTransitionError,
		)


def guard_changes(doc, before) -> None:
	"""After insert: only Draft referrals are edited by hand, and never their protected fields or log.

	Archiving (L5) is the exception: the Medical In-Charge may change only the archive flag, in any status.
	Other roles with write access to the record (Clinical Officer, MHO) edit Draft referrals only.
	"""
	old_log = [_log_row(r) for r in before.get("status_log") or []]
	new_log = [_log_row(r) for r in doc.get("status_log") or []]
	if new_log[: len(old_log)] != old_log:
		_locked(_("The referral status history cannot be edited."))
	if doc.flags.from_referral_action:
		return
	if len(new_log) != len(old_log):
		_locked(_("Status history rows are added only by referral actions."))
	changed = changed_fields(doc, before)
	if changed and set(changed) <= set(ARCHIVE_FIELDS):
		if not has_any_role(ARCHIVE_ROLES):
			_locked(_("Only the Medical In-Charge can archive a referral."))
		return
	if changed and not has_any_role(CONSULTING_ROLES):
		_locked(_("You can only archive this referral."))
	protected = [f for f in (*PROTECTED_FIELDS, *SOURCE_FIELDS) if f in changed]
	if protected:
		_locked(
			_("{0} can only change through referral actions.").format(
				", ".join(_(doc.meta.get_label(f)) for f in protected)
			)
		)
	if changed and before.status != STATUS_DRAFT:
		_locked(_("Referral {0} is {1} and can no longer be edited.").format(doc.name, _(before.status)))


def changed_fields(doc, before) -> list[str]:
	"""Hand-entered value fields whose value differs from the saved document.

	Tables, layout fields and fetched fields (`fetch_from`, refreshed by the server on save) are skipped.
	"""
	out = []
	for df in doc.meta.fields:
		if df.fieldtype in no_value_fields or df.fieldtype in table_fields or df.fetch_from:
			continue
		new, old = doc.get(df.fieldname), before.get(df.fieldname)
		if df.fieldtype in ("Check", "Int"):
			differs = cint(new) != cint(old)
		elif df.fieldtype == "Date":
			differs = (getdate(new) if new else None) != (getdate(old) if old else None)
		elif df.fieldtype == "Datetime":
			differs = (get_datetime(new) if new else None) != (get_datetime(old) if old else None)
		else:
			differs = (new or None) != (old or None)
		if differs:
			out.append(df.fieldname)
	return out


def _log_row(row) -> tuple:
	values = []
	for field in LOG_FIELDS:
		value = row.get(field)
		values.append((get_datetime(value) if value else None) if field == "timestamp" else (value or ""))
	return tuple(values)


def _locked(message: str) -> None:
	frappe.throw(message, ReferralLockedError, title=_("Referral Locked"))
