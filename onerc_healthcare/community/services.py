# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Service Delivered: social-work and rehabilitation services given during a visit (SDD §5.12.7, MOH 711).

A separate DocType, not a child table of Patient Visit: everyone who reads a visit would otherwise read
the services, which reveal social-work details. Only the Clinical Officer, Mental Health Officer,
Medical In-Charge and HIS Officer (read) hold DocPerms; the visit form shows a Services button to the
first three only. Reads are access-logged (`ACCESS_LOGGED_DOCTYPES`). Nobody deletes (NFR-9).

**Recording** (insert): Clinical Officer or Mental Health Officer, on an open or closed visit they may
read, unless the visit was closed as "Registered in error" or "Superseded by a new visit" (it is not an
attendance). Patient and facility come from the visit; `provided_by` is the recorder; the service date
lies between the visit date and today, and the service type must be enabled.

**Editing** an Active record (service, date, notes): its recorder or the Medical In-Charge. Patient,
visit, facility, recorder, status and the void fields change only through the lifecycle.

**Lifecycle** (`TRANSITIONS`; status is read-only):

| Action | From | To | Who | Rules |
|---|---|---|---|---|
| (insert) | - | Active | Clinical Officer, Mental Health Officer | as above |
| `void` (`void_service`, POST) | Active | Voided | the recorder (CO / MHO), or the Medical In-Charge | reason required; sets void_reason, voided_by, voided_at |

A Voided record is read-only and is never counted (R4 counts Active only). The record's version history
(track changes) keeps every edit.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime, today

from onerc_healthcare.constants import (
	REPORT_EXCLUDED_CLOSURE_REASONS,
	ROLE_CLINICIAN,
	ROLE_MHO,
	ROLE_MIC,
	SERVICE_DELIVERED_DOCTYPE,
	SERVICE_TYPE_DOCTYPE,
	VISIT_DOCTYPE,
)

STATUS_ACTIVE = "Active"
STATUS_VOIDED = "Voided"
STATUSES = (STATUS_ACTIVE, STATUS_VOIDED)

ACTION_RECORD = "record"
ACTION_VOID = "void"

RECORDER_ROLES = (ROLE_CLINICIAN, ROLE_MHO)
# Roles that act on any record; recorder roles act only on records they recorded.
SUPERVISOR_ROLES = (ROLE_MIC,)

REASON_MAX_LENGTH = 1000


class IllegalServiceTransitionError(frappe.ValidationError):
	"""The action is not allowed from the record's current status."""


class ServiceDeliveredError(frappe.ValidationError):
	"""The service cannot be recorded or changed this way."""


@dataclass(frozen=True)
class ServiceTransition:
	label: str
	from_statuses: tuple[str, ...]
	to_status: str
	# Roles allowed on records they recorded (`provided_by`).
	own_roles: tuple[str, ...]
	# Roles allowed on any record.
	any_roles: tuple[str, ...]
	needs_reason: bool = False


TRANSITIONS: dict[str, ServiceTransition] = {
	ACTION_RECORD: ServiceTransition(_("Record Service"), ("",), STATUS_ACTIVE, RECORDER_ROLES, ()),
	ACTION_VOID: ServiceTransition(
		_("Void Service"),
		(STATUS_ACTIVE,),
		STATUS_VOIDED,
		RECORDER_ROLES,
		SUPERVISOR_ROLES,
		needs_reason=True,
	),
}


def _roles(user: str) -> set[str]:
	return set(frappe.get_roles(user))


def is_recorder(doc, user: str) -> bool:
	return user in (doc.get("provided_by"), doc.get("owner"))


def may_act(rule: ServiceTransition, doc, user: str | None = None) -> bool:
	"""Whether `user` may apply `rule` to `doc` (role and ownership; status is checked separately)."""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	roles = _roles(user)
	if roles & set(rule.any_roles):
		return True
	return bool(roles & set(rule.own_roles)) and (doc is None or doc.is_new() or is_recorder(doc, user))


def may_edit(doc, user: str | None = None) -> bool:
	"""Recorder or Medical In-Charge, on an Active record."""
	user = user or frappe.session.user
	if doc.status != STATUS_ACTIVE:
		return False
	if user == "Administrator" or _roles(user) & set(SUPERVISOR_ROLES):
		return True
	return bool(_roles(user) & set(RECORDER_ROLES)) and is_recorder(doc, user)


def visit_values(visit: str) -> frappe._dict:
	row = frappe.db.get_value(
		VISIT_DOCTYPE, visit, ["name", "patient", "facility", "visit_date", "closure_reason"], as_dict=True
	)
	if not row:
		frappe.throw(_("Patient Visit {0} does not exist.").format(visit), ServiceDeliveredError)
	return row


def validate_new(doc) -> None:
	"""Insert: role, visit access, values from the visit, recorder, dates and service type."""
	user = frappe.session.user
	if not may_act(TRANSITIONS[ACTION_RECORD], doc, user):
		frappe.throw(
			_("Only a {0} or {1} can record a service.").format(_(ROLE_CLINICIAN), _(ROLE_MHO)),
			frappe.PermissionError,
		)
	visit = visit_values(doc.visit)
	if user != "Administrator" and not frappe.has_permission(VISIT_DOCTYPE, "read", doc=doc.visit, user=user):
		frappe.throw(_("You do not have access to visit {0}.").format(doc.visit), frappe.PermissionError)
	if visit.closure_reason in REPORT_EXCLUDED_CLOSURE_REASONS:
		frappe.throw(
			_("Visit {0} was closed as {1}; record the service on the patient's actual visit.").format(
				doc.visit, frappe.bold(_(visit.closure_reason))
			),
			ServiceDeliveredError,
		)
	from onerc_healthcare.visit.patients import assert_patient_active

	assert_patient_active(visit.patient)
	doc.patient = visit.patient
	doc.facility = visit.facility
	if user != "Administrator" or not doc.provided_by:
		doc.provided_by = user
	doc.status = STATUS_ACTIVE
	doc.void_reason = doc.voided_by = doc.voided_at = None
	validate_values(doc, visit)


def validate_values(doc, visit=None) -> None:
	"""Service date within the visit date .. today; service type enabled (when chosen or changed)."""
	visit = visit or visit_values(doc.visit)
	doc.service_date = getdate(doc.service_date or today())
	if doc.service_date > getdate(today()):
		frappe.throw(_("The service date cannot be in the future."), ServiceDeliveredError)
	if visit.visit_date and doc.service_date < getdate(visit.visit_date):
		frappe.throw(
			_("The service date cannot be before the visit date ({0}).").format(
				frappe.format(visit.visit_date, {"fieldtype": "Date"})
			),
			ServiceDeliveredError,
		)
	if doc.is_new() or doc.has_value_changed("service_type"):
		if not frappe.db.get_value(SERVICE_TYPE_DOCTYPE, doc.service_type, "enabled"):
			frappe.throw(
				_("Service type {0} is not enabled.").format(frappe.bold(doc.service_type)),
				ServiceDeliveredError,
			)
	doc.notes = (doc.notes or "").strip() or None


def transition(name: str, action: str, reason: str | None = None):
	"""Apply `action` to Service Delivered `name` as the current user. Returns the saved document."""
	rule = TRANSITIONS.get(action)
	if not rule or action == ACTION_RECORD:
		frappe.throw(_("Unknown service action: {0}").format(action), IllegalServiceTransitionError)
	doc = frappe.get_doc(SERVICE_DELIVERED_DOCTYPE, name, for_update=True)
	user = frappe.session.user
	if user != "Administrator" and not doc.has_permission("read"):
		frappe.throw(_("You do not have access to {0}.").format(doc.name), frappe.PermissionError)
	if not may_act(rule, doc, user):
		frappe.throw(
			_("Only the person who recorded the service, or the {0}, can {1}.").format(
				_(ROLE_MIC), rule.label.lower()
			),
			frappe.PermissionError,
		)
	if doc.status not in rule.from_statuses:
		frappe.throw(
			_("{0} is not allowed: {1} is {2}.").format(
				frappe.bold(rule.label), doc.name, frappe.bold(_(doc.status))
			),
			IllegalServiceTransitionError,
		)
	reason = (reason or "").strip() or None
	if rule.needs_reason and not reason:
		frappe.throw(
			_("Enter a reason for {0}.").format(frappe.bold(rule.label)),
			IllegalServiceTransitionError,
			title=_("Reason Required"),
		)
	if reason and len(reason) > REASON_MAX_LENGTH:
		frappe.throw(
			_("The reason is {0} characters long; the limit is {1}.").format(len(reason), REASON_MAX_LENGTH),
			IllegalServiceTransitionError,
		)
	if action == ACTION_VOID:
		doc.void_reason = reason
		doc.voided_by = user
		doc.voided_at = now_datetime()
	doc.status = rule.to_status
	doc.flags.from_workflow = True
	try:
		doc.save(ignore_permissions=True)
	finally:
		doc.flags.from_workflow = False
	return doc


@frappe.whitelist(methods=["POST"])
def void_service(name: str, reason: str) -> dict:
	"""Void a service recorded in error: its recorder or the Medical In-Charge, with a reason."""
	doc = transition(name, ACTION_VOID, reason=reason)
	return {"name": doc.name, "status": doc.status}


@frappe.whitelist(methods=["GET"])
def get_visit_services(visit: str) -> dict:
	"""Services recorded on `visit` that the user may read (the visit form's Services section)."""
	frappe.has_permission(SERVICE_DELIVERED_DOCTYPE, "read", throw=True)
	frappe.has_permission(VISIT_DOCTYPE, "read", doc=visit, throw=True)
	rows = frappe.get_list(
		SERVICE_DELIVERED_DOCTYPE,
		filters={"visit": visit},
		fields=["name", "service_type", "service_date", "provided_by", "status"],
		order_by="service_date asc, creation asc",
	)
	if rows:
		from onerc_healthcare.privacy.access_log import ACTION_LIST, log_access

		log_access(
			ACTION_LIST,
			SERVICE_DELIVERED_DOCTYPE,
			None,
			patient=frappe.db.get_value(VISIT_DOCTYPE, visit, "patient"),
			detail=f"Services of visit {visit}: {len(rows)} row(s)",
		)
	user = frappe.session.user
	return {
		"services": rows,
		"can_record": frappe.has_permission(SERVICE_DELIVERED_DOCTYPE, "create")
		and (user == "Administrator" or bool(_roles(user) & set(RECORDER_ROLES))),
	}
