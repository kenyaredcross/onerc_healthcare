# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Override for Marley's make_observation (hooks.py override_whitelisted_methods). Owned by B2.

W0 stub passes through; B2 adds the permission check and the 2nd-order workaround (ensure_observations).
"""

import frappe


@frappe.whitelist()
def make_observation(service_request: str, appointment: str | None = None):
	from healthcare.healthcare.doctype.service_request.service_request import make_observation as _orig

	return _orig(service_request, appointment)
