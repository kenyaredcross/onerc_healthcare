# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Pharmacy task checker for the visit reconciler (`visit.tasks.TASK_CHECKERS["pharmacy"]`)."""

from __future__ import annotations

import frappe

from onerc_healthcare.constants import (
	ENCOUNTER_DOCTYPE,
	ENCOUNTER_VISIT_FIELD,
	VISIT_DOCTYPE,
	VISIT_ENCOUNTER_FIELD,
)
from onerc_healthcare.pharmacy.dispensing import HOLD_FIELD, MR_DOCTYPE, STATUS_FIELD, is_line_resolved


def visit_encounters(visit: str, for_update: bool = False) -> list[str]:
	"""Submitted encounters of a visit: the visit's `encounter` link plus any encounter pointing at it."""
	names = set()
	linked = frappe.db.get_value(VISIT_DOCTYPE, visit, VISIT_ENCOUNTER_FIELD, for_update=for_update)
	if linked:
		names.add(linked)
	if frappe.get_meta(ENCOUNTER_DOCTYPE).has_field(ENCOUNTER_VISIT_FIELD):
		names.update(
			frappe.db.get_values(
				ENCOUNTER_DOCTYPE, {ENCOUNTER_VISIT_FIELD: visit}, "name", pluck=True, for_update=for_update
			)
		)
	if not names:
		return []
	return frappe.db.get_values(
		ENCOUNTER_DOCTYPE,
		{"name": ["in", list(names)], "docstatus": 1},
		"name",
		pluck=True,
		for_update=for_update,
	)


def is_pharmacy_complete(visit: str, for_update: bool = False) -> bool:
	"""True when the visit's consultation is submitted and every submitted prescription line is resolved.

	Resolved = Dispensed, Not Dispensed or Closed, stopped by the prescriber, or ended from outside through
	Marley's status (stopped / cancelled / entered-in-error / ended); a held or on-hold line is open.
	A submitted encounter without prescriptions is complete (nothing to dispense); a visit without a
	submitted encounter is not (prescriptions are not released). `for_update` makes every read a locking
	read; callers that hold the visit lock pass it (fix round M7 / L1).
	"""
	encounters = visit_encounters(visit, for_update=for_update)
	if not encounters:
		return False
	lines = frappe.db.get_values(
		MR_DOCTYPE,
		{"order_group": ["in", encounters], "docstatus": 1},
		[STATUS_FIELD, HOLD_FIELD, "status"],
		as_dict=True,
		for_update=for_update,
	)
	return all(is_line_resolved(r.get(STATUS_FIELD), r.get(HOLD_FIELD), r.get("status")) for r in lines)
