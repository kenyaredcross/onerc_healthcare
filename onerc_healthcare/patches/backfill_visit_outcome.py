# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 3 (R3): give visits closed before outcomes existed their outcome (data migration).

The rules are in `onerc_healthcare.visit.outcome.backfill_outcomes`. Direct writes, no status-log rows,
`outcome_at` from `closed_at`, `outcome_by` empty. Idempotent: only visits without an outcome change.
"""

import frappe

from onerc_healthcare.visit.outcome import backfill_outcomes


def execute():
	if not frappe.get_meta("Patient Visit").has_field("outcome"):
		return
	counts = backfill_outcomes()
	print(
		f"onerc_healthcare: visit outcome backfill: {counts['updated']} set, {counts['skipped']} left empty"
	)
