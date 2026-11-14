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
	VISIT_DOCTYPE,
)

# Clinical Officers and Mental Health Officers consult in any department (decision 2026-11-13; OI-7 may
# restrict Mental Health later).
CONSULTING_ROLES = (ROLE_CLINICIAN, ROLE_MHO)
# Lab Prescription rows withdrawn when the visit was closed before they were sent (never deleted).
LAB_WITHDRAWN_FIELD = "krcs_withdrawn"


def has_any_role(roles: tuple[str, ...] | list[str], user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	# Phase 4 (D58): an outreach team member's synced record passes the clinical role gates while
	# outreach.sync applies it (any team member may consult and prescribe; see outreach/context.py).
	from onerc_healthcare.outreach import context as outreach_context

	if outreach_context.allows(user):
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


def active_encounter_for_visit(
	visit: str, exclude: str | None = None, for_update: bool = False
) -> str | None:
	"""The visit's draft or submitted Patient Encounter (one per visit), else None.

	With `for_update` the row is read with a locking read (``SELECT ... FOR UPDATE``), so under REPEATABLE
	READ a transaction waiting on the visit lock sees an encounter inserted by the one before it. Lock
	order: Patient Visit, then Patient Encounter, then child rows.
	"""
	filters = {ENCOUNTER_VISIT_FIELD: visit, "docstatus": ["<", 2]}
	if exclude:
		filters["name"] = ["!=", exclude]
	return frappe.db.get_value(
		ENCOUNTER_DOCTYPE, filters, "name", order_by="creation asc", for_update=for_update
	)


def lock_visit_and_encounter(encounter: str):
	"""Lock the encounter's visit, then the encounter (the agreed order: Patient Visit -> Patient Encounter
	-> child records such as Service Request, Medication Request, Observation). Returns (visit_doc, doc).

	Every entry point of the clinical package that changes an encounter goes through this. `visit_doc` is
	None when the encounter has no visit (`krcs_visit` never changes after insert, so reading it before
	the lock is safe).
	"""
	visit = frappe.db.get_value(ENCOUNTER_DOCTYPE, encounter, ENCOUNTER_VISIT_FIELD)
	visit_doc = frappe.get_doc(VISIT_DOCTYPE, visit, for_update=True) if visit else None
	doc = frappe.get_doc(ENCOUNTER_DOCTYPE, encounter, for_update=True)
	return visit_doc, doc


def unsent_lab_rows(doc) -> list:
	"""Lab rows of an encounter that have no Service Request yet and were not withdrawn."""
	return [
		row
		for row in doc.get("lab_test_prescription") or []
		if (row.observation_template or row.lab_test_code)
		and not row.service_request
		and not row.get(LAB_WITHDRAWN_FIELD)
	]
