# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Overrides for Marley endpoints that skip permission checks (hooks.py override_whitelisted_methods).

Owned by B1. W0 stubs pass straight through to Marley; B1 blocks or permission-checks them.
"""

import frappe


@frappe.whitelist()
def create_service_request(encounter: str):
	from healthcare.healthcare.doctype.patient_encounter.patient_encounter import (
		create_service_request as _orig,
	)

	return _orig(encounter)


@frappe.whitelist()
def create_service_request_from_widget(encounter: str, data: str, medication_request: bool | str = False):
	from healthcare.healthcare.doctype.patient_encounter.patient_encounter import (
		create_service_request_from_widget as _orig,
	)

	return _orig(encounter, data, medication_request)


@frappe.whitelist()
def create_patient_referral(encounter: str, references: str | dict | list):
	from healthcare.healthcare.doctype.patient_encounter.patient_encounter import (
		create_patient_referral as _orig,
	)

	return _orig(encounter, references)


@frappe.whitelist()
def cancel_request(doctype: str, request: str):
	from healthcare.healthcare.doctype.patient_encounter.patient_encounter import cancel_request as _orig

	return _orig(doctype, request)
