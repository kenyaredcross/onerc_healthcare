# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient Visit permission hooks (wired in hooks.py), SDD §6.1.

DocPerms in the DocType JSON give the matrix (Receptionist creates; Triage Nurse and Clinical/Mental
Health Officer write through transitions; Lab, Pharmacist, HIS Officer, Medical In-Charge read; Head of
Operations reads permlevel 0 only). One site per operation, so there is no company filter. These hooks
add the rules DocPerms cannot express:

- users with none of the visit roles see no visits (the EMR System Administrator in particular);
- read-only visit roles can never write, even if a Custom DocPerm is added later;
- a closed visit (Discharged / Referred) is read-only for everyone; system events still update it
  through the engine.
"""

from __future__ import annotations

import frappe

from onerc_healthcare.constants import (
	ROLE_CLINICIAN,
	ROLE_HEAD_OPS,
	ROLE_HIS,
	ROLE_LAB,
	ROLE_MHO,
	ROLE_MIC,
	ROLE_PHARMACIST,
	ROLE_RECEPTIONIST,
	ROLE_TRIAGE,
	VISIT_CLOSED_STATUSES,
)

VISIT_WRITE_ROLES = frozenset((ROLE_RECEPTIONIST, ROLE_TRIAGE, ROLE_CLINICIAN, ROLE_MHO))
VISIT_READ_ROLES = frozenset((ROLE_LAB, ROLE_PHARMACIST, ROLE_HIS, ROLE_MIC, ROLE_HEAD_OPS))
VISIT_ROLES = VISIT_WRITE_ROLES | VISIT_READ_ROLES
WRITE_PTYPES = frozenset(("write", "create", "submit", "cancel", "amend", "delete", "import"))


def _is_superuser(user: str) -> bool:
	return user == "Administrator"


def patient_visit_query(user: str | None = None, doctype: str | None = None) -> str:
	user = user or frappe.session.user
	if _is_superuser(user):
		return ""
	if VISIT_ROLES & set(frappe.get_roles(user)):
		return ""
	return "1=0"


def patient_visit_has_permission(doc, ptype: str | None = None, user: str | None = None, **kwargs) -> bool:
	user = user or frappe.session.user
	if _is_superuser(user):
		return True
	roles = set(frappe.get_roles(user))
	if not VISIT_ROLES & roles:
		return False
	if ptype in WRITE_PTYPES:
		if not VISIT_WRITE_ROLES & roles:
			return False
		if ptype == "write" and doc.get("status") in VISIT_CLOSED_STATUSES:
			return False
	return True
