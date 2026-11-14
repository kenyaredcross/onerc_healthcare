# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient Encounter permission hooks (wired in hooks.py). Owned by B1. Mental-health visibility pending OI-7.

No encounter cancel or amend in Phase 2 (corrections are addenda). Refusing here stops a cancel before
Marley's `before_cancel` runs (it cancels the encounter's Service Requests first);
`events.encounter_before_cancel` is the second line.
"""

import frappe

NO_PTYPES = ("cancel", "amend")


def encounter_query(user: str | None = None, doctype: str | None = None) -> str:
	return ""


def encounter_has_permission(doc, ptype: str | None = None, user: str | None = None, **kwargs) -> bool:
	if ptype in NO_PTYPES and (user or frappe.session.user) != "Administrator":
		return False
	return True
