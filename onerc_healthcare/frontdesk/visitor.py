# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Visitor Record lifecycle (FR-41, BR-13, SDD §5.11; D8 pattern).

| Action        | From   | To     | Roles              | Rules                                            |
|---------------|--------|--------|--------------------|--------------------------------------------------|
| (insert)      | -      | Inside | Nurse Receptionist | entry time not in the future; log row "record_entry" |
| `record_exit` | Inside | Exited | Nurse Receptionist | entry time <= exit time <= now; log row appended |

`status`, `exit_time` and the log change only through `transition()` (flag `from_workflow`); the log is
append-only. An Exited record is read-only. Records are never deleted (NFR-9). Visitor data is limited to
what the entry record needs (C-3): name, relationship, an optional identifier and the patient accompanied.

Archiving (`set_archived`, POST): Exited records only, Nurse Receptionist or Medical In-Charge (L5).
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from onerc_healthcare.constants import ROLE_MIC, ROLE_RECEPTIONIST, VISITOR_DOCTYPE

STATUS_INSIDE = "Inside"
STATUS_EXITED = "Exited"
STATUSES = (STATUS_INSIDE, STATUS_EXITED)

ACTION_RECORD_ENTRY = "record_entry"
ACTION_RECORD_EXIT = "record_exit"

# Archiving an Exited record (NFR-9, fix round L5): the Receptionist owns the record, the Medical In-Charge
# manages retention. The flag is the security setup's `krcs_archived`.
ARCHIVE_ROLES = (ROLE_RECEPTIONIST, ROLE_MIC)
ARCHIVED_FIELD = "krcs_archived"

# Allowed clock skew between the browser and the server for "not in the future" checks.
FUTURE_TOLERANCE_SECONDS = 300


class IllegalVisitorTransitionError(frappe.ValidationError):
	"""The action is not allowed from the visitor record's current status."""


@dataclass(frozen=True)
class VisitorTransition:
	label: str
	from_statuses: tuple[str, ...]
	to_status: str
	roles: tuple[str, ...]


TRANSITIONS: dict[str, VisitorTransition] = {
	ACTION_RECORD_EXIT: VisitorTransition(
		_("Record Exit"), (STATUS_INSIDE,), STATUS_EXITED, (ROLE_RECEPTIONIST,)
	),
}


def is_future(value) -> bool:
	return (get_datetime(value) - now_datetime()).total_seconds() > FUTURE_TOLERANCE_SECONDS


def _check_role(rule: VisitorTransition, user: str) -> None:
	if user == "Administrator":
		return
	if not set(frappe.get_roles(user)) & set(rule.roles):
		frappe.throw(
			_("You are not allowed to perform {0}. Required role: {1}.").format(
				frappe.bold(rule.label), ", ".join(_(r) for r in rule.roles)
			),
			frappe.PermissionError,
		)


def transition(name: str, action: str, exit_time=None, comment: str | None = None):
	"""Apply `action` to Visitor Record `name` as the current user. Returns the saved document."""
	rule = TRANSITIONS.get(action)
	if not rule:
		frappe.throw(_("Unknown visitor action: {0}").format(action), IllegalVisitorTransitionError)
	user = frappe.session.user
	_check_role(rule, user)
	doc = frappe.get_doc(VISITOR_DOCTYPE, name, for_update=True)
	doc.check_permission("write")
	if doc.status not in rule.from_statuses:
		frappe.throw(
			_("{0} is not allowed: visitor record {1} is {2}.").format(
				frappe.bold(rule.label), doc.name, frappe.bold(_(doc.status))
			),
			IllegalVisitorTransitionError,
		)
	now = now_datetime()
	if action == ACTION_RECORD_EXIT:
		exit_at = get_datetime(exit_time) if exit_time else now
		if exit_at < get_datetime(doc.entry_time):
			frappe.throw(
				_("The exit time cannot be before the entry time ({0}).").format(doc.entry_time),
				IllegalVisitorTransitionError,
			)
		if is_future(exit_at):
			frappe.throw(_("The exit time cannot be in the future."), IllegalVisitorTransitionError)
		doc.exit_time = exit_at
	from_status = doc.status
	doc.status = rule.to_status
	doc.append(
		"status_log",
		{
			"from_status": from_status,
			"to_status": rule.to_status,
			"action": action,
			"user": user,
			"timestamp": now,
			"comment": (comment or "").strip() or None,
		},
	)
	doc.flags.from_workflow = True
	try:
		doc.save()
	finally:
		doc.flags.from_workflow = False
	return doc


@frappe.whitelist(methods=["POST"])
def record_exit(visitor_record: str, exit_time: str | None = None, comment: str | None = None) -> dict:
	"""Nurse Receptionist: record that the visitor left the facility."""
	doc = transition(visitor_record, ACTION_RECORD_EXIT, exit_time=exit_time, comment=comment)
	return {"name": doc.name, "status": doc.status, "exit_time": str(doc.exit_time)}


def can_archive(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user == "Administrator" or bool(set(frappe.get_roles(user)) & set(ARCHIVE_ROLES))


@frappe.whitelist(methods=["POST"])
def set_archived(visitor_record: str, archived: int = 1) -> dict:
	"""Archive (or un-archive) an Exited visitor record: Nurse Receptionist or Medical In-Charge (L5)."""
	if not can_archive():
		frappe.throw(
			_("Only the Nurse Receptionist or the Medical In-Charge can archive a visitor record."),
			frappe.PermissionError,
		)
	frappe.has_permission(VISITOR_DOCTYPE, "read", doc=visitor_record, throw=True)
	if not frappe.get_meta(VISITOR_DOCTYPE).has_field(ARCHIVED_FIELD):
		frappe.throw(_("Visitor records have no archive flag on this site."))
	doc = frappe.get_doc(VISITOR_DOCTYPE, visitor_record, for_update=True)
	doc.set(ARCHIVED_FIELD, 1 if archived else 0)
	# The Medical In-Charge only reads visitor records; the role check above authorises this one change,
	# and the controller refuses anything but the archive flag on an Exited record.
	doc.save(ignore_permissions=True)
	return {"name": doc.name, ARCHIVED_FIELD: doc.get(ARCHIVED_FIELD)}
