# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Outreach Session row access (OUT-03, ROL-22; wired in hooks.py).

DocPerms: HIS Officer and Medical In-Charge read/write/create; Outreach Team Member read. These hooks add:

- managers (`OUTREACH_MANAGER_ROLES`) and Administrator: every session;
- an Outreach Team Member: only the sessions whose team lists them (read only; they never write a session,
  the lead's Mark Team Returned goes through `outreach.session.apply_action`);
- anyone else: no session, whatever DocPerm a later Custom DocPerm might add.

`session_has_permission` must return a truthy value to allow: Frappe treats any falsy return as a denial.
"""

from __future__ import annotations

import frappe

from onerc_healthcare.constants import (
	OUTREACH_MANAGER_ROLES,
	OUTREACH_SESSION_DOCTYPE,
	OUTREACH_TEAM_DOCTYPE,
	ROLE_OUTREACH,
)

READ_PTYPES = frozenset(("read", "select", "report", "print", "email", "export", "share"))


def _is_manager(roles: set[str]) -> bool:
	return bool(set(OUTREACH_MANAGER_ROLES) & roles)


def session_query(user: str | None = None, doctype: str | None = None) -> str:
	user = user or frappe.session.user
	if user == "Administrator":
		return ""
	roles = set(frappe.get_roles(user))
	if _is_manager(roles):
		return ""
	if ROLE_OUTREACH in roles:
		return (
			f"exists (select 1 from `tab{OUTREACH_TEAM_DOCTYPE}` krcs_m"
			f" where krcs_m.parent = `tab{OUTREACH_SESSION_DOCTYPE}`.name"
			f" and krcs_m.parenttype = {frappe.db.escape(OUTREACH_SESSION_DOCTYPE)}"
			f" and krcs_m.user = {frappe.db.escape(user)})"
		)
	return "1=0"


def session_has_permission(doc, ptype: str | None = None, user: str | None = None, **kwargs) -> bool:
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	roles = set(frappe.get_roles(user))
	if _is_manager(roles):
		return True
	if ROLE_OUTREACH not in roles or (ptype and ptype not in READ_PTYPES):
		return False
	# Membership is read from the saved team, never from an unsaved (client-edited) copy of the document.
	name = doc.get("name")
	return bool(
		name
		and frappe.db.exists(
			OUTREACH_TEAM_DOCTYPE, {"parent": name, "parenttype": OUTREACH_SESSION_DOCTYPE, "user": user}
		)
	)
