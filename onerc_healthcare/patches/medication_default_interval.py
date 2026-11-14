# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""DEFECT-RX-1 (found by the Phase 4 smoke test): the seeded medications had no dose interval, so Marley's
Drug Prescription quantity was one day's dose (BID for 5 days = 2, not 10) and a pharmacist could not
dispense a full course. Set the daily interval on every Medication without one. Existing Medication Requests
keep their quantity; only new prescriptions change. Idempotent.
"""

import frappe

from onerc_healthcare.pharmacy.catalogue import set_default_intervals


def execute():
	if not frappe.db.table_exists("Medication"):
		return
	print(f"onerc_healthcare: medication dose interval set on {set_default_intervals()} medication(s)")
