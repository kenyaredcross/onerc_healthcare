# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Complete the Frappe/ERPNext setup wizard from code for the site's operation."""

import frappe

from onerc_healthcare.setup.operations import get_operation_config

# KRCS fiscal year: 1 July - 30 June (confirmed by the user 2026-11-13; see docs/DECISIONS.md).
FY_START = "2026-07-01"
FY_END = "2027-06-30"


def ensure_setup_complete(op: str) -> None:
	if frappe.is_setup_complete():
		return

	from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

	cfg = get_operation_config(op)
	setup_complete(
		{
			"language": "English",
			"country": "Kenya",
			"timezone": "Africa/Nairobi",
			"currency": "KES",
			"company_name": cfg["company_name"],
			"company_abbr": cfg["abbr"],
			"chart_of_accounts": "Standard",
			"fy_start_date": FY_START,
			"fy_end_date": FY_END,
			# no "email": the wizard creates no extra user
		}
	)
	# setup_complete() swallows its own exceptions; fail loudly instead.
	if not frappe.is_setup_complete() or not frappe.db.exists("Company", cfg["company_name"]):
		frappe.throw(f"Setup wizard did not complete for operation {op}; see the Error Log")
