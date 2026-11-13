# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Overrides for Marley endpoints that skip permission checks (hooks.py override_whitelisted_methods).

Owned by B1. Marley's versions insert and submit Service Requests with ``ignore_permissions`` and no
permission check, and would bypass the consultation flow (orders outside `send_lab_orders`, referrals
outside the Referral record, cancels outside `cancel_lab_order`). They are blocked; the EMR equivalents
live in `onerc_healthcare.clinical.consultation` and `onerc_healthcare.clinical.referral`.
"""

import frappe
from frappe import _


def _blocked(use_instead: str):
	frappe.throw(
		_("This action is not available in the EMR. Use {0} instead.").format(frappe.bold(use_instead)),
		frappe.PermissionError,
		title=_("Not Allowed"),
	)


@frappe.whitelist()
def create_service_request(encounter: str):
	_blocked(_("Send Lab Orders on the consultation"))


@frappe.whitelist()
def create_service_request_from_widget(encounter: str, data: str, medication_request: bool | str = False):
	_blocked(_("Send Lab Orders on the consultation"))


@frappe.whitelist()
def create_patient_referral(encounter: str, references: str | dict | list):
	_blocked(_("Refer on the consultation"))


@frappe.whitelist()
def cancel_request(doctype: str, request: str):
	_blocked(_("Cancel Lab Order on the consultation"))


@frappe.whitelist()
def create_medication_request(encounter: str) -> None:
	"""Prescriptions are released when the consultation is completed (integrator: add the override)."""
	_blocked(_("Complete Consultation"))
