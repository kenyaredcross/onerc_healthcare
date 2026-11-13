# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Test data factories. Everything is prefixed ``T-`` / ``@test.krcs``. Never commit here.

Usage::

    from onerc_healthcare.tests.factories import get_facility, make_patient, make_user

    nurse = make_user("triage", [ROLE_TRIAGE])  # -> "t-triage@test.krcs"
    patient = make_patient(first_name="Amina")
    facility = get_facility()  # first facility group of the site's operation
"""

from __future__ import annotations

import frappe
from frappe.utils import add_years, random_string, today

from onerc_healthcare.constants import IDENTIFIER_UNHCR, PATIENT_IDENTIFIERS_FIELD
from onerc_healthcare.setup.operations import get_company, get_facilities

DOMAIN = "test.krcs"


def test_email(key: str) -> str:
	return key if "@" in key else f"t-{key.lower()}@{DOMAIN}"


def make_user(key: str, roles: list[str] | tuple[str, ...] = ()) -> str:
	"""Create (or reuse) a desk user `t-<key>@test.krcs` holding exactly `roles`. Returns the user name."""
	email = test_email(key)
	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
	else:
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": f"T-{key}",
				"user_type": "System User",
				"send_welcome_email": 0,
			}
		)
		user.flags.no_welcome_mail = True
		user.insert(ignore_permissions=True)
	user.set("roles", [])
	for role in roles:
		user.append("roles", {"role": role})
	user.save(ignore_permissions=True)
	return user.name


def get_facility(i: int = 0) -> str:
	"""Healthcare Service Unit name of the site operation's i-th facility group."""
	return get_facilities()[i]


def get_company_name() -> str:
	return get_company()


def make_patient(**kw) -> frappe.model.document.Document:
	"""Insert a Patient prefixed ``T-``.

	Adds a UNHCR ID identifier row when the Patient has the `krcs_identifiers` field (A1), unless the
	caller passes its own identifiers or `krcs_is_walk_in=1`.
	"""
	values = {
		"doctype": "Patient",
		"first_name": f"T-{random_string(6)}",
		"last_name": "Test",
		"sex": "Female",
		"dob": add_years(today(), -30),
	}
	values.update(kw)
	meta = frappe.get_meta("Patient")
	if (
		meta.has_field(PATIENT_IDENTIFIERS_FIELD)
		and PATIENT_IDENTIFIERS_FIELD not in kw
		and not kw.get("krcs_is_walk_in")
	):
		values[PATIENT_IDENTIFIERS_FIELD] = [
			{"identifier_type": IDENTIFIER_UNHCR, "identifier_value": f"T-UNHCR-{random_string(8)}"}
		]
	return frappe.get_doc(values).insert(ignore_permissions=True)
