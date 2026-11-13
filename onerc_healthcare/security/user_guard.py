# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""User-management guard for the EMR System Administrator (FR-39, SDD §6.1).

The EMR System Administrator manages users without being a System Manager, so it must not be able to
escalate or take over accounts. For any actor who is neither Administrator nor System Manager:

- only EMR roles (plus the automatic All / Guest / Desk User) may be granted or revoked, directly or
  through a Role Profile;
- nobody changes their own access (roles, profiles, modules, enabled, user type);
- a *protected* account, one holding any role outside that set (System Manager, Healthcare
  Administrator, Physician, Accounts Manager…), may not have its access, email or password changed.

The guard only looks at those sensitive fields, so framework flows that save a User for other reasons
(the Guest password-reset and expiry flows clear ``reset_password_key``; the password itself is
written separately) pass. Hooks: User ``validate`` and Role Profile ``validate``.
"""

import frappe
from frappe import _
from frappe.permissions import AUTOMATIC_ROLES
from frappe.utils import cint

from onerc_healthcare.constants import ALL_ROLES

PRIVILEGED_ROLES = ("Administrator", "System Manager")
# Roles a non-privileged actor may hand out. Administrator is an automatic role in Frappe but is never
# assignable here.
ASSIGNABLE_ROLES = frozenset(ALL_ROLES) | (frozenset(AUTOMATIC_ROLES) - {"Administrator"})


def _is_privileged(user: str) -> bool:
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def _is_protected(roles) -> bool:
	return bool(set(roles) - ASSIGNABLE_ROLES)


def _access_snapshot(doc) -> dict:
	return {
		"roles": frozenset(r.role for r in doc.get("roles") or []),
		"role_profiles": frozenset(
			[r.role_profile for r in doc.get("role_profiles") or []]
			+ ([doc.role_profile_name] if doc.get("role_profile_name") else [])
		),
		"module_profile": doc.get("module_profile") or "",
		"block_modules": frozenset(r.module for r in doc.get("block_modules") or []),
		"enabled": cint(doc.get("enabled")),
		"user_type": doc.get("user_type") or "",
	}


def user_validate(doc, method=None) -> None:
	actor = frappe.session.user
	if _is_privileged(actor):
		return

	before = None
	if not doc.is_new():
		before = doc.get_doc_before_save() or frappe.get_doc("User", doc.name)
	old_roles = {r.role for r in before.roles} if before else set()
	new_roles = {r.role for r in doc.roles}

	if before is None:
		access_changed = True
		email_changed = False
	else:
		old, new = _access_snapshot(before), _access_snapshot(doc)
		access_changed = old != new
		email_changed = (before.email or "") != (doc.email or "")
	# A password typed into the User form by someone else. Guest flows (reset link, expiry) and a user's
	# own change do not set it on someone else's account.
	password_set = bool(doc.get("new_password")) and actor not in ("Guest", doc.name)

	if not (access_changed or email_changed or password_set):
		return

	if actor == doc.name:
		if access_changed:
			frappe.throw(_("You cannot change your own access."), frappe.PermissionError)
		return

	if _is_protected(old_roles):
		frappe.throw(
			_("Only a System Manager can change the account {0}.").format(frappe.bold(doc.name)),
			frappe.PermissionError,
		)
	not_allowed = sorted(old_roles.symmetric_difference(new_roles) - ASSIGNABLE_ROLES)
	if not_allowed:
		frappe.throw(
			_("Only a System Manager can grant or revoke these roles: {0}").format(", ".join(not_allowed)),
			frappe.PermissionError,
		)


def role_profile_validate(doc, method=None) -> None:
	"""A non-privileged actor may only build profiles from EMR roles, and may not edit a profile that
	already carries any other role (it would reach users the next time they are saved)."""
	if _is_privileged(frappe.session.user):
		return
	before = None
	if not doc.is_new():
		before = doc.get_doc_before_save() or frappe.get_doc("Role Profile", doc.name)
	old_roles = {r.role for r in before.roles} if before else set()
	if _is_protected(old_roles):
		frappe.throw(
			_("Only a System Manager can change the role profile {0}.").format(frappe.bold(doc.name)),
			frappe.PermissionError,
		)
	not_allowed = sorted({r.role for r in doc.roles} - ASSIGNABLE_ROLES)
	if not_allowed:
		frappe.throw(
			_("Only a System Manager can put these roles in a role profile: {0}").format(
				", ".join(not_allowed)
			),
			frappe.PermissionError,
		)
