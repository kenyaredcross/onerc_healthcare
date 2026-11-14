# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Daily Facility Entry (BR-13, FR-41, FR-42, SDD §5.11): everyone present at a facility on a date.

Rows: patient visits (arrival to closure), visitors (entry to exit) and screenings of anyone not covered
by those two (staff, other persons, patients or visitors screened without a visit or visitor record), each
with the latest screening outcome.

The Head of Operations sees daily counts only (SDD §6.1: no patient-level content). That is enforced here
by role: a user without a full-view role gets the counts view whatever filter is sent.

Every run is written to the access log (FR-40, fix round M4), the counts view included.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, getdate, today

from onerc_healthcare.constants import (
	REPORT_DAILY_ENTRY,
	ROLE_HEAD_OPS,
	ROLE_MIC,
	ROLE_RECEPTIONIST,
	ROLE_TRIAGE,
	SCREENING_DOCTYPE,
	VISIT_DOCTYPE,
	VISITOR_DOCTYPE,
)
from onerc_healthcare.frontdesk.screening import OUTCOME_OUT_OF_RANGE
from onerc_healthcare.privacy.access_log import log_report_run

FULL_VIEW_ROLES = (ROLE_RECEPTIONIST, ROLE_TRIAGE, ROLE_MIC)
COUNTS_ONLY_ROLES = (ROLE_HEAD_OPS,)

PERSON_PATIENT = "Patient"
PERSON_VISITOR = "Visitor"
PERSON_STAFF = "Staff"
PERSON_OTHER = "Other"
COUNT_CATEGORIES = (PERSON_PATIENT, PERSON_VISITOR, PERSON_STAFF, PERSON_OTHER)


def counts_only(user: str | None = None) -> bool:
	"""True when the user may see daily counts but not who was present."""
	user = user or frappe.session.user
	if user == "Administrator":
		return False
	return not set(frappe.get_roles(user)) & set(FULL_VIEW_ROLES)


def execute(filters: dict | None = None):
	filters = frappe._dict(filters or {})
	filters.date = getdate(filters.get("date") or today())
	log_report_run(REPORT_DAILY_ENTRY, filters=filters)  # FR-40 / M4: names of everyone present
	rows = get_entries(filters)
	if counts_only():
		return count_columns(), get_counts(rows)
	return entry_columns(), rows


def entry_columns() -> list[dict]:
	return [
		{"label": _("Time In"), "fieldname": "time_in", "fieldtype": "Datetime", "width": 160},
		{"label": _("Time Out"), "fieldname": "time_out", "fieldtype": "Datetime", "width": 160},
		{"label": _("Person"), "fieldname": "person_type", "fieldtype": "Data", "width": 90},
		{"label": _("Name"), "fieldname": "person_name", "fieldtype": "Data", "width": 180},
		{
			"label": _("Patient"),
			"fieldname": "patient",
			"fieldtype": "Link",
			"options": "Patient",
			"width": 140,
		},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 130},
		{"label": _("Screening"), "fieldname": "screening_outcome", "fieldtype": "Data", "width": 160},
		{
			"label": _("Facility"),
			"fieldname": "facility",
			"fieldtype": "Link",
			"options": "Healthcare Service Unit",
			"width": 160,
		},
		{
			"label": _("Record Type"),
			"fieldname": "reference_doctype",
			"fieldtype": "Data",
			"width": 120,
			"hidden": 1,
		},
		{
			"label": _("Record"),
			"fieldname": "reference_name",
			"fieldtype": "Dynamic Link",
			"options": "reference_doctype",
			"width": 150,
		},
	]


def count_columns() -> list[dict]:
	return [
		{"label": _("Category"), "fieldname": "category", "fieldtype": "Data", "width": 260},
		{"label": _("Count"), "fieldname": "count", "fieldtype": "Int", "width": 100},
	]


def get_counts(rows: list[dict]) -> list[dict]:
	counts = [
		{"category": _(c), "count": sum(1 for r in rows if r["person_type"] == c)} for c in COUNT_CATEGORIES
	]
	counts.append({"category": _("Total present"), "count": len(rows)})
	counts.append(
		{
			"category": _("Screened outside expected range"),
			"count": sum(1 for r in rows if r.get("screening_outcome") == OUTCOME_OUT_OF_RANGE),
		}
	)
	return counts


def _day_range(date) -> tuple[str, str]:
	return f"{date} 00:00:00", f"{add_days(date, 1)} 00:00:00"


def get_entries(filters: frappe._dict) -> list[dict]:
	start, end = _day_range(filters.date)
	facility = filters.get("facility")
	screenings = _screenings(start, end, facility)
	by_visit = {s.visit: s for s in screenings if s.visit}
	by_visitor = {s.visitor_record: s for s in screenings if s.visitor_record}
	rows = []

	visit_filters = {"visit_date": filters.date}
	if facility:
		visit_filters["facility"] = facility
	for v in frappe.get_all(
		VISIT_DOCTYPE,
		filters=visit_filters,
		fields=["name", "patient", "patient_name", "facility", "arrived_at", "closed_at", "status"],
	):
		s = by_visit.pop(v.name, None) or _latest_screening({"visit": v.name})
		rows.append(
			_row(
				PERSON_PATIENT,
				v.patient_name,
				v.arrived_at,
				v.closed_at,
				v.status,
				v.facility,
				VISIT_DOCTYPE,
				v.name,
				s,
				patient=v.patient,
			)
		)

	visitor_filters = {"entry_time": ["between", [start, end]]}
	if facility:
		visitor_filters["facility"] = facility
	for vr in frappe.get_all(
		VISITOR_DOCTYPE,
		filters=visitor_filters,
		fields=["name", "visitor_name", "patient", "facility", "entry_time", "exit_time", "status"],
	):
		s = by_visitor.pop(vr.name, None) or _latest_screening({"visitor_record": vr.name})
		rows.append(
			_row(
				PERSON_VISITOR,
				vr.visitor_name,
				vr.entry_time,
				vr.exit_time,
				vr.status,
				vr.facility,
				VISITOR_DOCTYPE,
				vr.name,
				s,
				patient=vr.patient,
			)
		)

	# Screenings left in by_visit / by_visitor belong to a visit or visitor record not listed above.
	unmatched = {s.name for s in by_visit.values()} | {s.name for s in by_visitor.values()}
	for s in screenings:
		if (s.visit or s.visitor_record) and s.name not in unmatched:
			continue  # already shown with its visit / visitor record
		rows.append(
			_row(
				s.person_type,
				s.person_name,
				s.screened_at,
				None,
				None,
				s.facility,
				SCREENING_DOCTYPE,
				s.name,
				s,
				patient=s.patient,
			)
		)
	rows.sort(key=lambda r: (r["time_in"] is None, str(r["time_in"] or "")))
	return rows


def _screenings(start: str, end: str, facility: str | None) -> list:
	"""Submitted screenings of the day, latest first (so the first one seen per person is the latest)."""
	filters = {"docstatus": 1, "screened_at": ["between", [start, end]]}
	if facility:
		filters["facility"] = facility
	rows = frappe.get_all(
		SCREENING_DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"person_type",
			"person_name",
			"patient",
			"visit",
			"visitor_record",
			"facility",
			"screened_at",
			"outcome",
		],
		order_by="screened_at desc",
	)
	# Keep only the latest screening per visit / visitor record; repeat screenings of one person are not new entries.
	seen, out = set(), []
	for s in rows:
		key = (
			("visit", s.visit)
			if s.visit
			else ("visitor", s.visitor_record)
			if s.visitor_record
			else ("own", s.name)
		)
		if key in seen:
			continue
		seen.add(key)
		out.append(s)
	return out


def _latest_screening(filters: dict):
	rows = frappe.get_all(
		SCREENING_DOCTYPE,
		filters={**filters, "docstatus": 1},
		fields=["name", "outcome"],
		order_by="screened_at desc",
		limit=1,
	)
	return rows[0] if rows else None


def _row(
	person_type, name, time_in, time_out, status, facility, ref_doctype, ref_name, screening, patient=None
) -> dict:
	return {
		"time_in": time_in,
		"time_out": time_out,
		"person_type": person_type,
		"person_name": name,
		"patient": patient,
		"status": status,
		"screening_outcome": screening.outcome if screening else None,
		"facility": facility,
		"reference_doctype": ref_doctype,
		"reference_name": ref_name,
	}
