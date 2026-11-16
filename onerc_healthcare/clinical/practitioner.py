# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Healthcare Practitioner per clinician user (FR-10).

Marley's mandatory `Patient Encounter.practitioner` is filled from the consulting user. Each Clinical
Officer or Mental Health Officer gets exactly one Healthcare Practitioner, created on first use.

Marley's `HealthcarePractitioner.on_update` adds a User Permission (`apply_to_all_doctypes=1`) for the
linked user. That would hide every colleague's encounters, orders and practitioners from the clinician,
against D22 (no User Permissions inside a site), so it is removed again (`strip_user_permission`).
"""

from __future__ import annotations

import frappe
from frappe import _

from onerc_healthcare.clinical.utils import CONSULTING_ROLES, has_any_role

PRACTITIONER_DOCTYPE = "Healthcare Practitioner"


def get_or_create(user: str | None = None) -> str:
	"""The Healthcare Practitioner of `user` (default: session user); created when missing."""
	user = user or frappe.session.user
	existing = frappe.db.get_value(PRACTITIONER_DOCTYPE, {"user_id": user}, "name")
	if existing:
		return existing
	if user == "Guest" or not has_any_role(CONSULTING_ROLES, user):
		frappe.throw(
			_("Only a Clinical Officer or Mental Health Officer can be the practitioner of a consultation."),
			frappe.PermissionError,
		)
	first_name, last_name = frappe.db.get_value("User", user, ["first_name", "last_name"]) or (None, None)
	# Phase 4 DEFECT-P4-4: an outreach record replayed or decided after its author's account was disabled keeps
	# that member as its author (D58), so inside the outreach context their practitioner is still created.
	from onerc_healthcare.outreach import context as outreach_context

	if not frappe.db.get_value("User", user, "enabled") and not outreach_context.allows(user):
		frappe.throw(_("User {0} is disabled.").format(user), frappe.PermissionError)
	doc = frappe.get_doc(
		{
			"doctype": PRACTITIONER_DOCTYPE,
			"first_name": first_name or user,
			"last_name": last_name,
			"status": "Active",
		}
	)
	# Inserted without the user, then linked directly: Marley's on_update would otherwise insert a User
	# Permission as the clinician (who may not create one) only for `practitioner_on_update` to remove it.
	doc.insert(ignore_permissions=True)
	doc.db_set("user_id", user)
	return doc.name


def strip_user_permission(practitioner: str, user: str | None) -> int:
	"""Remove the User Permission Marley adds for the practitioner's user (D22). Returns rows removed."""
	if not user:
		return 0
	names = frappe.get_all(
		"User Permission",
		filters={"user": user, "allow": PRACTITIONER_DOCTYPE, "for_value": practitioner},
		pluck="name",
	)
	for name in names:
		frappe.delete_doc("User Permission", name, ignore_permissions=True, force=True)
	return len(names)


def strip_all_user_permissions() -> int:
	"""Install / migrate: remove Practitioner User Permissions left on existing sites."""
	count = 0
	for row in frappe.get_all(
		PRACTITIONER_DOCTYPE, filters={"user_id": ["is", "set"]}, fields=["name", "user_id"]
	):
		count += strip_user_permission(row.name, row.user_id)
	return count
