# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Small D8-style state-machine helpers shared by Consent Statement and Data Access Request.

Each lifecycle module declares a module-level ``TRANSITIONS`` dict of `Transition` rows (introspectable
by the S1 auditor). Status changes only through the lifecycle module, which saves with
``doc.flags.from_workflow``; the controllers refuse protected-field edits and log-row changes without it.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, getdate, now_datetime

LOG_FIELDS = ("from_status", "to_status", "action", "user", "timestamp", "comment")
MAX_COMMENT_LENGTH = 1000


class IllegalTransitionError(frappe.ValidationError):
	"""The action is not allowed from the record's current status."""


class LockedFieldError(frappe.ValidationError):
	"""A protected field or a history row was changed outside the lifecycle actions."""


@dataclass(frozen=True)
class Transition:
	label: str
	# Empty for the creating action.
	from_statuses: tuple[str, ...]
	to_status: str
	# Roles allowed to perform it. Empty: system event (code or Administrator only).
	roles: tuple[str, ...] = ()
	# Keyword arguments that must be non-empty.
	required: tuple[str, ...] = ()
	# Data Access Request only: the request type this action applies to (None = any).
	request_type: str | None = None

	@property
	def is_system(self) -> bool:
		return not self.roles


def has_any_role(roles, user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user == "Administrator" or bool(set(roles) & set(frappe.get_roles(user)))


def check_role(rule: Transition, doc=None, system: bool = False) -> None:
	"""Raise PermissionError unless the current user may perform `rule` (and can read `doc`)."""
	user = frappe.session.user
	if system or user == "Administrator":
		return
	if rule.is_system:
		frappe.throw(
			_("{0} is a system event and cannot be performed by a user.").format(frappe.bold(rule.label)),
			frappe.PermissionError,
		)
	if not has_any_role(rule.roles, user):
		frappe.throw(
			_("You are not allowed to perform {0}. Required role: {1}.").format(
				frappe.bold(rule.label), ", ".join(_(r) for r in rule.roles)
			),
			frappe.PermissionError,
		)
	if doc is not None and not doc.has_permission("read"):
		frappe.throw(
			_("You do not have access to {0} {1}.").format(_(doc.doctype), doc.name), frappe.PermissionError
		)


def check_from_status(rule: Transition, doc) -> None:
	if doc.status not in rule.from_statuses:
		frappe.throw(
			_("{0} is not allowed for {1} {2} in status {3}.").format(
				frappe.bold(rule.label), _(doc.doctype), doc.name, frappe.bold(_(doc.status))
			),
			IllegalTransitionError,
		)


def clean_text(value: str | None, label: str, required: bool = False) -> str | None:
	value = (value or "").strip() or None
	if required and not value:
		frappe.throw(_("Enter the {0}.").format(_(label)), IllegalTransitionError, title=_("Missing Value"))
	if value and len(value) > MAX_COMMENT_LENGTH:
		frappe.throw(
			_("{0} is {1} characters long; the limit is {2}.").format(
				_(label), len(value), MAX_COMMENT_LENGTH
			),
			IllegalTransitionError,
		)
	return value


def append_log(doc, from_status: str | None, to_status: str, action: str, comment: str | None = None):
	doc.append(
		"log",
		{
			"from_status": from_status,
			"to_status": to_status,
			"action": action,
			"user": frappe.session.user,
			"timestamp": now_datetime(),
			"comment": comment,
		},
	)


def save_from_workflow(doc) -> None:
	doc.flags.from_workflow = True
	try:
		doc.save(ignore_permissions=True)
	finally:
		doc.flags.from_workflow = False


def _norm(doc, fieldname: str, value):
	"""Comparable value (form saves send dates/datetimes as strings and checks as str/int)."""
	df = doc.meta.get_field(fieldname)
	fieldtype = df.fieldtype if df else None
	if fieldtype in ("Check", "Int"):
		return cint(value)
	if fieldtype == "Datetime":
		return get_datetime(value) if value else None
	if fieldtype == "Date":
		return getdate(value) if value else None
	if isinstance(value, str):
		value = value.strip()
	return value or None


def guard_fields(doc, before, fields) -> None:
	"""Refuse changes to `fields` unless saved by the lifecycle module."""
	if doc.flags.from_workflow:
		return
	changed = [f for f in fields if _norm(doc, f, doc.get(f)) != _norm(doc, f, before.get(f))]
	if changed:
		frappe.throw(
			_("{0} can only change through the record's actions.").format(
				", ".join(frappe.bold(_(doc.meta.get_label(f))) for f in changed)
			),
			LockedFieldError,
			title=_("Not Editable"),
		)


def guard_log(doc, before, table: str = "log") -> None:
	"""History rows are append-only: existing rows stay identical; only the lifecycle module appends."""
	old = before.get(table) or []
	new = doc.get(table) or []

	def key(row):
		return tuple(
			(get_datetime(row.get(f)) if row.get(f) else None) if f == "timestamp" else (row.get(f) or "")
			for f in LOG_FIELDS
		)

	if len(new) < len(old) or [key(r) for r in new[: len(old)]] != [key(r) for r in old]:
		frappe.throw(_("The status history cannot be edited."), LockedFieldError, title=_("Not Editable"))
	if len(new) > len(old) and not doc.flags.from_workflow:
		frappe.throw(
			_("The status history is written by the record's actions only."),
			LockedFieldError,
			title=_("Not Editable"),
		)
