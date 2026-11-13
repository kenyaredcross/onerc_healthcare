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
from onerc_healthcare.pharmacy.dispensing import MR_DOCTYPE, RESOLVED_LINE_STATES, STATUS_FIELD


def visit_encounters(visit: str) -> list[str]:
	"""Submitted encounters of a visit: the visit's `encounter` link plus any encounter pointing at it."""
	names = set()
	linked = frappe.db.get_value(VISIT_DOCTYPE, visit, VISIT_ENCOUNTER_FIELD)
	if linked:
		names.add(linked)
	if frappe.get_meta(ENCOUNTER_DOCTYPE).has_field(ENCOUNTER_VISIT_FIELD):
		names.update(frappe.get_all(ENCOUNTER_DOCTYPE, filters={ENCOUNTER_VISIT_FIELD: visit}, pluck="name"))
	if not names:
		return []
	return frappe.get_all(
		ENCOUNTER_DOCTYPE, filters={"name": ["in", list(names)], "docstatus": 1}, pluck="name"
	)


def is_pharmacy_complete(visit: str) -> bool:
	"""True when the visit's consultation is submitted and every submitted prescription line is resolved.

	Resolved = Dispensed, Not Dispensed or Closed. A submitted encounter without prescriptions is complete
	(nothing to dispense); a visit without a submitted encounter is not (prescriptions are not released).
	"""
	encounters = visit_encounters(visit)
	if not encounters:
		return False
	statuses = frappe.get_all(
		MR_DOCTYPE,
		filters={"order_group": ["in", encounters], "docstatus": 1},
		pluck=STATUS_FIELD,
	)
	return all(s in RESOLVED_LINE_STATES for s in statuses)
