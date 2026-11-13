# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Reception queue, queue numbering and waiting times (FR-6, BR-2, SDD §5.2)."""

from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, getdate, now_datetime, today

from onerc_healthcare.constants import (
	PATIENT_IDENTIFIER_DOCTYPE,
	PATIENT_IDENTIFIERS_FIELD,
	PRIMARY_IDENTIFIER_TYPES,
	VISIT_DOCTYPE,
	VISIT_OPEN_STATUSES,
	VISIT_STATUSES,
)

# Fields at permlevel 1 that identify the patient; Head of Operations (status only) does not get them.
PATIENT_FIELDS = ("patient", "patient_name", "patient_identifier", "triage_level")


def next_queue_number(facility: str, visit_date) -> int:
	"""Next queue number for `facility` on `visit_date`, safe under concurrent registrations.

	Uses a row in `tabSeries` keyed by facility and date: the row is created with INSERT IGNORE (so two
	first-of-day registrations cannot collide) and then locked with SELECT ... FOR UPDATE until commit.
	"""
	key = _series_key(facility, visit_date)
	frappe.db.sql("INSERT IGNORE INTO `tabSeries` (`name`, `current`) VALUES (%s, 0)", (key,))
	current = frappe.db.sql("SELECT `current` FROM `tabSeries` WHERE `name`=%s FOR UPDATE", (key,))[0][0]
	frappe.db.sql("UPDATE `tabSeries` SET `current` = `current` + 1 WHERE `name`=%s", (key,))
	return cint(current) + 1


def _series_key(facility: str, visit_date) -> str:
	code = frappe.db.get_value("Healthcare Service Unit", facility, "krcs_facility_code") or ""
	if not code:
		code = hashlib.sha1(facility.encode()).hexdigest()[:10]
	return f"KRCS-PVQ-{code}-{getdate(visit_date).strftime('%Y%m%d')}"


def get_primary_identifier(patient: str) -> str | None:
	"""Display identifier of a patient: the first primary identifier, else the first identifier.

	Reads the A1 `krcs_identifiers` table when it exists, otherwise falls back to Marley's `uid`.
	"""
	if frappe.get_meta("Patient").has_field(PATIENT_IDENTIFIERS_FIELD):
		rows = frappe.get_all(
			PATIENT_IDENTIFIER_DOCTYPE,
			filters={"parent": patient, "parenttype": "Patient", "parentfield": PATIENT_IDENTIFIERS_FIELD},
			fields=["identifier_type", "identifier_value"],
			order_by="idx asc",
		)
		primary = [r for r in rows if r.identifier_type in PRIMARY_IDENTIFIER_TYPES]
		chosen = (primary or rows or [None])[0]
		if chosen:
			return f"{chosen.identifier_type}: {chosen.identifier_value}"
	return frappe.db.get_value("Patient", patient, "uid") or None


def waiting_minutes(since, until=None) -> int:
	"""Whole minutes from `since` to `until` (default now)."""
	if not since:
		return 0
	delta = get_datetime(until or now_datetime()) - get_datetime(since)
	return max(int(delta.total_seconds() // 60), 0)


@frappe.whitelist()
def get_queue(facility: str, status: str | None = None) -> list[dict]:
	"""Open visits at `facility` ordered by visit date and queue number (FR-6).

	Each row has the patient name and primary identifier (omitted for users without patient-level
	access, i.e. Head of Operations), queue number, status, department, waiting minutes since arrival,
	minutes in the current status, and ``stale`` (1 for an open visit from an earlier day).
	"""
	frappe.has_permission(VISIT_DOCTYPE, "read", throw=True)
	if status and status not in VISIT_STATUSES:
		frappe.throw(_("Unknown visit status: {0}").format(status))
	statuses = [status] if status else list(VISIT_OPEN_STATUSES)

	fields = [
		"name",
		"facility",
		"visit_date",
		"queue_number",
		"status",
		"current_department",
		"arrived_at",
		"status_changed_at",
		"lab_open",
		"pharmacy_open",
	]
	can_see_patient = 1 in frappe.get_meta(VISIT_DOCTYPE).get_permlevel_access("read")
	if can_see_patient:
		fields += list(PATIENT_FIELDS)

	rows = frappe.get_list(
		VISIT_DOCTYPE,
		filters={"facility": facility, "status": ["in", statuses]},
		fields=fields,
		order_by="visit_date asc, queue_number asc",
		limit_page_length=0,
	)
	now, current_day = now_datetime(), getdate(today())
	for row in rows:
		row["waiting_minutes"] = waiting_minutes(row.arrived_at, now)
		row["minutes_in_status"] = waiting_minutes(row.status_changed_at or row.arrived_at, now)
		# Open visits from earlier days are kept (closing them is a clinical decision) but flagged.
		row["stale"] = 1 if getdate(row.visit_date) < current_day else 0
	return rows


@frappe.whitelist()
def longest_wait_card(filters: str | dict | list | None = None) -> dict:
	"""Custom Number Card: longest current wait (minutes since arrival) among today's open visits.

	Open visits left over from earlier days are excluded so they do not inflate the KPI (SDD §5.2).
	"""
	frappe.has_permission(VISIT_DOCTYPE, "read", throw=True)
	oldest = frappe.get_list(
		VISIT_DOCTYPE,
		filters={"status": ["in", list(VISIT_OPEN_STATUSES)], "visit_date": today()},
		fields=["arrived_at"],
		order_by="arrived_at asc",
		limit_page_length=1,
	)
	minutes = waiting_minutes(oldest[0].arrived_at) if oldest else 0
	return {"value": minutes, "fieldtype": "Int"}
