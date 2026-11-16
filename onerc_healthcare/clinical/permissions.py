# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient Encounter permission hooks (wired in hooks.py). Owned by B1. Mental-health visibility pending OI-7.

No encounter cancel or amend in Phase 2 (corrections are addenda). Refusing here stops a cancel before
Marley's `before_cancel` runs (it cancels the encounter's Service Requests first);
`events.encounter_before_cancel` is the second line.

Phase 5M fix round: the Midwife reads encounters for the antenatal history (D60), not Mental Health
consultations. A user whose only encounter-reading role is Midwife (`midwife_only_reader`: none of Clinical
Officer, Mental Health Officer, Medical In-Charge, HIS Officer) does not see encounters of the Mental Health
department (`krcs_department` or `medical_department`), in lists or one by one.
"""

import frappe

from onerc_healthcare.constants import (
	DEPT_MENTAL_HEALTH,
	ROLE_CLINICIAN,
	ROLE_HIS,
	ROLE_MHO,
	ROLE_MIC,
	ROLE_MIDWIFE,
)

NO_PTYPES = ("cancel", "amend")
# Roles that read every encounter (security.matrix "Patient Encounter"), apart from the Midwife.
FULL_ENCOUNTER_READERS = (ROLE_CLINICIAN, ROLE_MHO, ROLE_MIC, ROLE_HIS)
DEPARTMENT_FIELDS = ("krcs_department", "medical_department")


def midwife_only_reader(user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user == "Administrator":
		return False
	roles = set(frappe.get_roles(user))
	return ROLE_MIDWIFE in roles and not roles.intersection(FULL_ENCOUNTER_READERS)


def encounter_query(user: str | None = None, doctype: str | None = None) -> str:
	if not midwife_only_reader(user):
		return ""
	dept = frappe.db.escape(DEPT_MENTAL_HEALTH)
	return " AND ".join(
		f"IFNULL(`tabPatient Encounter`.`{f}`, '') != {dept}"
		for f in DEPARTMENT_FIELDS
		if frappe.get_meta("Patient Encounter").has_field(f)
	)


def encounter_has_permission(doc, ptype: str | None = None, user: str | None = None, **kwargs) -> bool:
	if ptype in NO_PTYPES and (user or frappe.session.user) != "Administrator":
		return False
	if midwife_only_reader(user) and is_mental_health_encounter(doc):
		return False
	return True


def is_mental_health_encounter(doc) -> bool:
	if isinstance(doc, str):
		values = frappe.db.get_value("Patient Encounter", doc, list(DEPARTMENT_FIELDS), as_dict=True) or {}
	else:
		values = {f: doc.get(f) for f in DEPARTMENT_FIELDS}
	return DEPT_MENTAL_HEALTH in values.values()
