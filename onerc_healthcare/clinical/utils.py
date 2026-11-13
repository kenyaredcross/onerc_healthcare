# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Small helpers shared by the clinical package (B1)."""

from __future__ import annotations

import frappe
from frappe import _

from onerc_healthcare.constants import (
	ENCOUNTER_DOCTYPE,
	ENCOUNTER_VISIT_FIELD,
	ROLE_CLINICIAN,
	ROLE_MHO,
)

# Clinical Officers and Mental Health Officers consult in any department (decision 2026-11-13; OI-7 may
# restrict Mental Health later).
CONSULTING_ROLES = (ROLE_CLINICIAN, ROLE_MHO)


def has_any_role(roles: tuple[str, ...] | list[str], user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	return bool(set(frappe.get_roles(user)) & set(roles))


def require_roles(roles: tuple[str, ...] | list[str], label: str) -> None:
	"""PermissionError unless the session user holds one of `roles` (Administrator always passes)."""
	if not has_any_role(roles):
		frappe.throw(
			_("You are not allowed to {0}. Required role: {1}.").format(
				label, ", ".join(_(r) for r in roles)
			),
			frappe.PermissionError,
		)


def active_encounter_for_visit(visit: str, exclude: str | None = None) -> str | None:
	"""The visit's draft or submitted Patient Encounter (one per visit), else None."""
	filters = {ENCOUNTER_VISIT_FIELD: visit, "docstatus": ["<", 2]}
	if exclude:
		filters["name"] = ["!=", exclude]
	rows = frappe.get_all(ENCOUNTER_DOCTYPE, filters=filters, pluck="name", order_by="creation asc", limit=1)
	return rows[0] if rows else None


def unsent_lab_rows(doc) -> list:
	"""Lab rows of an encounter that have no Service Request yet."""
	return [
		row
		for row in doc.get("lab_test_prescription") or []
		if (row.observation_template or row.lab_test_code) and not row.service_request
	]
