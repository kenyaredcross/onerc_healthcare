# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""A2 test helpers: visit-role users, visits and triage vital signs. Never commit."""

from __future__ import annotations

import frappe
from frappe.utils import nowdate, nowtime

from onerc_healthcare.constants import (
	ROLE_CLINICIAN,
	ROLE_HEAD_OPS,
	ROLE_HIS,
	ROLE_LAB,
	ROLE_MHO,
	ROLE_MIC,
	ROLE_PHARMACIST,
	ROLE_RECEPTIONIST,
	ROLE_SYSADMIN,
	ROLE_TRIAGE,
	VISIT_DOCTYPE,
)
from onerc_healthcare.tests.factories import get_facility, make_patient, make_user

# key -> role; users are t-pv-<key>@test.krcs holding exactly that role.
VISIT_USERS = {
	"reception": ROLE_RECEPTIONIST,
	"triage": ROLE_TRIAGE,
	"clinician": ROLE_CLINICIAN,
	"mho": ROLE_MHO,
	"lab": ROLE_LAB,
	"pharmacist": ROLE_PHARMACIST,
	"his": ROLE_HIS,
	"mic": ROLE_MIC,
	"hoo": ROLE_HEAD_OPS,
	"sysadmin": ROLE_SYSADMIN,
}


def make_visit_users() -> dict[str, str]:
	"""Create one user per visit-relevant role. Returns {key: user name}."""
	return {key: make_user(f"pv-{key}", [role]) for key, role in VISIT_USERS.items()}


def make_visit(
	patient: str | None = None,
	facility: str | None = None,
	ignore_permissions: bool = True,
	system: bool = False,
	**kw,
):
	"""Insert a Patient Visit (Registered) for a new T- patient unless `patient` is given.

	`system=True` inserts as server code (``flags.from_workflow``), which may keep a supplied
	`visit_date` / `arrived_at`; otherwise the server overwrites them.
	"""
	values = {
		"doctype": VISIT_DOCTYPE,
		"patient": patient or make_patient().name,
		"facility": facility or get_facility(0),
	}
	values.update(kw)
	doc = frappe.get_doc(values)
	doc.flags.from_workflow = system
	doc.insert(ignore_permissions=ignore_permissions)
	doc.flags.from_workflow = False
	return doc


def make_vital_signs(patient: str, visit: str | None = None, submit: bool = False, **kw):
	values = {
		"doctype": "Vital Signs",
		"patient": patient,
		"signs_date": nowdate(),
		"signs_time": nowtime(),
		"temperature": "36.8",
		"pulse": "72",
		"respiratory_rate": "16",
		"bp_systolic": "120",
		"bp_diastolic": "80",
	}
	if visit:
		values["krcs_visit"] = visit
	values.update(kw)
	doc = frappe.get_doc(values).insert(ignore_permissions=True)
	if submit:
		doc.submit()
	return doc
