# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Pharmacy Worklist (FR-20, BR-6): open prescription lines for the Pharmacist.

Open line = a submitted Medication Request whose dispense status is Pending or Partly Dispensed and that
the clinician has not stopped, cancelled or entered in error. Lines of a visit closed in the meantime stay
on the list (the visit status column shows it). A line the prescriber has put on hold is listed with its
hold and reason but without the Dispense action; a stopped line leaves the list. Filters: facility (the
visit's) and order date. Each run is written to the access log (FR-40, fix round M4).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from onerc_healthcare.constants import (
	ENCOUNTER_DOCTYPE,
	ENCOUNTER_VISIT_FIELD,
	REPORT_PHARMACY_WORKLIST,
	VISIT_DOCTYPE,
	VISIT_ENCOUNTER_FIELD,
)
from onerc_healthcare.pharmacy.dispensing import (
	DISPENSABLE_MR_STATUSES,
	HOLD_FIELD,
	HOLD_REASON_FIELD,
	LINE_PENDING,
	MR_DOCTYPE,
	OPEN_LINE_STATES,
	QTY_FIELD,
	STATUS_FIELD,
	dispensable_quantity,
)
from onerc_healthcare.privacy.access_log import log_report_run

# Marley statuses that take a line off the worklist even when nothing was dispensed.
EXCLUDED_MR_STATUSES = (
	"cancelled-Medication Request Status",
	"entered-in-error-Medication Request Status",
	"ended-Medication Request Status",
	"stopped-Medication Request Status",
)


def execute(filters: dict | None = None):
	filters = frappe._dict(filters or {})
	log_report_run(REPORT_PHARMACY_WORKLIST, filters=filters)  # FR-40 / M4: patient-level report
	return get_columns(), get_data(filters)


def get_columns() -> list[dict]:
	return [
		{
			"label": _("Prescription"),
			"fieldname": "medication_request",
			"fieldtype": "Link",
			"options": MR_DOCTYPE,
			"width": 150,
		},
		{"label": _("Ordered"), "fieldname": "order_date", "fieldtype": "Date", "width": 95},
		{
			"label": _("Patient"),
			"fieldname": "patient",
			"fieldtype": "Link",
			"options": "Patient",
			"width": 140,
		},
		{"label": _("Patient Name"), "fieldname": "patient_name", "fieldtype": "Data", "width": 160},
		{
			"label": _("Visit"),
			"fieldname": "visit",
			"fieldtype": "Link",
			"options": VISIT_DOCTYPE,
			"width": 140,
		},
		{"label": _("Visit Status"), "fieldname": "visit_status", "fieldtype": "Data", "width": 130},
		{
			"label": _("Facility"),
			"fieldname": "facility",
			"fieldtype": "Link",
			"options": "Healthcare Service Unit",
			"width": 160,
		},
		{
			"label": _("Medicine"),
			"fieldname": "medication",
			"fieldtype": "Link",
			"options": "Medication",
			"width": 200,
		},
		{"label": _("Item"), "fieldname": "item", "fieldtype": "Link", "options": "Item", "width": 160},
		{"label": _("Dose"), "fieldname": "dose", "fieldtype": "Data", "width": 90},
		{
			"label": _("Frequency"),
			"fieldname": "frequency",
			"fieldtype": "Link",
			"options": "Prescription Dosage",
			"width": 100,
		},
		{
			"label": _("Duration"),
			"fieldname": "duration",
			"fieldtype": "Link",
			"options": "Prescription Duration",
			"width": 90,
		},
		{"label": _("Prescribed Qty"), "fieldname": "prescribed_qty", "fieldtype": "Float", "width": 110},
		{"label": _("Dispensed Qty"), "fieldname": "dispensed_qty", "fieldtype": "Float", "width": 110},
		{"label": _("Remaining Qty"), "fieldname": "remaining_qty", "fieldtype": "Float", "width": 110},
		{"label": _("Dispense Status"), "fieldname": "dispense_status", "fieldtype": "Data", "width": 120},
		{"label": _("Prescriber Hold"), "fieldname": "hold_status", "fieldtype": "Data", "width": 110},
		{"label": _("Comment"), "fieldname": "comment", "fieldtype": "Data", "width": 160},
		{"label": _("Action"), "fieldname": "action", "fieldtype": "Data", "width": 100},
	]


def get_data(filters: frappe._dict) -> list[dict]:
	conditions = {
		"docstatus": 1,
		"status": ["not in", list(EXCLUDED_MR_STATUSES)],
		STATUS_FIELD: ["in", list(OPEN_LINE_STATES)],
	}
	if filters.get("date"):
		conditions["order_date"] = filters.date
	lines = frappe.get_all(
		MR_DOCTYPE,
		filters=conditions,
		fields=[
			"name",
			"status",
			"order_date",
			"order_time",
			"patient",
			"patient_name",
			"order_group",
			"medication",
			"medication_item",
			"dosage",
			"period",
			"quantity",
			"total_dispensable_quantity",
			"comment",
			STATUS_FIELD,
			QTY_FIELD,
			HOLD_FIELD,
			HOLD_REASON_FIELD,
		],
		order_by="order_date asc, order_time asc, name asc",
	)
	if not lines:
		return []
	visits = _visits_by_encounter({ln.order_group for ln in lines if ln.order_group})
	doses = _doses({ln.medication for ln in lines if ln.medication})
	rows = []
	for ln in lines:
		visit = visits.get(ln.order_group) or {}
		if filters.get("facility") and visit.get("facility") != filters.facility:
			continue
		prescribed = dispensable_quantity(ln)
		dispensed = flt(ln.get(QTY_FIELD))
		rows.append(
			{
				"medication_request": ln.name,
				"order_date": ln.order_date,
				"patient": ln.patient,
				"patient_name": ln.patient_name,
				"visit": visit.get("name"),
				"visit_status": visit.get("status"),
				"facility": visit.get("facility"),
				"medication": ln.medication,
				"item": ln.medication_item,
				"dose": doses.get(ln.medication),
				"frequency": ln.dosage,
				"duration": ln.period,
				"prescribed_qty": prescribed,
				"dispensed_qty": dispensed,
				"remaining_qty": max(prescribed - dispensed, 0),
				"dispense_status": ln.get(STATUS_FIELD) or LINE_PENDING,
				"hold_status": ln.get(HOLD_FIELD) or None,
				"comment": ln.comment
				if not ln.get(HOLD_FIELD)
				else f"{_(ln.get(HOLD_FIELD))}: {ln.get(HOLD_REASON_FIELD) or ''}",
				# A held line stays visible (the Pharmacist knows to wait) but cannot be dispensed.
				"action": None
				if ln.get(HOLD_FIELD) or ln.status not in DISPENSABLE_MR_STATUSES
				else "Dispense",
			}
		)
	return rows


def _visits_by_encounter(encounters: set[str]) -> dict[str, dict]:
	"""encounter -> {name, status, facility} of its Patient Visit."""
	if not encounters:
		return {}
	out = {}
	for v in frappe.get_all(
		VISIT_DOCTYPE,
		filters={VISIT_ENCOUNTER_FIELD: ["in", list(encounters)]},
		fields=["name", "status", "facility", VISIT_ENCOUNTER_FIELD],
	):
		out[v.get(VISIT_ENCOUNTER_FIELD)] = v
	missing = encounters - set(out)
	if missing and frappe.get_meta(ENCOUNTER_DOCTYPE).has_field(ENCOUNTER_VISIT_FIELD):
		pairs = frappe.get_all(
			ENCOUNTER_DOCTYPE,
			filters={"name": ["in", list(missing)], ENCOUNTER_VISIT_FIELD: ["is", "set"]},
			fields=["name", ENCOUNTER_VISIT_FIELD],
		)
		by_visit = {p.get(ENCOUNTER_VISIT_FIELD): p.name for p in pairs}
		if by_visit:
			for v in frappe.get_all(
				VISIT_DOCTYPE,
				filters={"name": ["in", list(by_visit)]},
				fields=["name", "status", "facility"],
			):
				out[by_visit[v.name]] = v
	return out


def _doses(medications: set[str]) -> dict[str, str]:
	"""medication -> its strength, e.g. "500 Milligram"."""
	if not medications:
		return {}
	return {
		m.name: f"{flt(m.strength):g} {m.strength_uom or ''}".strip()
		for m in frappe.get_all(
			"Medication",
			filters={"name": ["in", list(medications)]},
			fields=["name", "strength", "strength_uom"],
		)
	}
