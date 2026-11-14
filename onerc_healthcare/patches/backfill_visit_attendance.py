# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 3 (R3, L10): visits registered before attendance existed get New / Re-attendance by the
calendar-year rule (`onerc_healthcare.visit.attendance.backfill_attendance`), in arrival order, per
patient and facility, ignoring visits closed as Registered in error or Superseded. Basis "Calendar-year
rule". Direct writes, no log rows; only visits without a value change, so it is idempotent.
"""

import frappe

from onerc_healthcare.visit.attendance import backfill_attendance


def execute():
	if not frappe.get_meta("Patient Visit").has_field("attendance_type"):
		return
	counts = backfill_attendance()
	print(f"onerc_healthcare: visit attendance backfill: {counts['updated']} set")
