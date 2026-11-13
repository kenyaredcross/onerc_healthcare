# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Laboratory Worklist (FR-16, SDD §5.4): every pending lab request, for the Laboratory Technician.

A row is an active lab Service Request (submitted, Observation Template, not revoked) that is not done
(`lab.tasks.EncounterLab.is_done`: its root Observation is not submitted). Sample and result status come
from the Sample Collection rows and the Observations, never from the Service Request status. Filters:
facility (the visit's) and order date range.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, get_datetime, getdate, today

from onerc_healthcare.constants import ENCOUNTER_DOCTYPE, ENCOUNTER_VISIT_FIELD, VISIT_DOCTYPE
from onerc_healthcare.lab.tasks import LAB_TEMPLATE_DT, SR_REVOKED, EncounterLab

DEFAULT_DAYS_BACK = 30


def execute(filters: dict | None = None):
	filters = frappe._dict(filters or {})
	return get_columns(), get_data(filters)


def get_columns() -> list[dict]:
	return [
		{"label": _("Queue No."), "fieldname": "queue_number", "fieldtype": "Int", "width": 80},
		{
			"label": _("Visit"),
			"fieldname": "visit",
			"fieldtype": "Link",
			"options": VISIT_DOCTYPE,
			"width": 140,
		},
		{
			"label": _("Patient"),
			"fieldname": "patient",
			"fieldtype": "Link",
			"options": "Patient",
			"width": 130,
		},
		{"label": _("Patient Name"), "fieldname": "patient_name", "fieldtype": "Data", "width": 170},
		{
			"label": _("Test"),
			"fieldname": "test",
			"fieldtype": "Link",
			"options": "Observation Template",
			"width": 200,
		},
		{"label": _("Ordered"), "fieldname": "ordered_at", "fieldtype": "Datetime", "width": 160},
		{"label": _("Sample Status"), "fieldname": "sample_status", "fieldtype": "Data", "width": 150},
		{
			"label": _("Sample Collection"),
			"fieldname": "sample_collection",
			"fieldtype": "Link",
			"options": "Sample Collection",
			"width": 160,
		},
		{"label": _("Result Status"), "fieldname": "result_status", "fieldtype": "Data", "width": 170},
		{
			"label": _("Observation"),
			"fieldname": "observation",
			"fieldtype": "Link",
			"options": "Observation",
			"width": 160,
		},
		{
			"label": _("Lab Order"),
			"fieldname": "service_request",
			"fieldtype": "Link",
			"options": "Service Request",
			"width": 150,
		},
		{"label": _("Visit Status"), "fieldname": "visit_status", "fieldtype": "Data", "width": 140},
		{
			"label": _("Facility"),
			"fieldname": "facility",
			"fieldtype": "Link",
			"options": "Healthcare Service Unit",
			"width": 200,
		},
	]


def get_data(filters: frappe._dict) -> list[dict]:
	from_date = getdate(filters.from_date or add_days(today(), -DEFAULT_DAYS_BACK))
	to_date = getdate(filters.to_date or today())
	requests = frappe.get_all(
		"Service Request",
		filters={
			"template_dt": LAB_TEMPLATE_DT,
			"docstatus": 1,
			"status": ["!=", SR_REVOKED],
			"source_doc": ENCOUNTER_DOCTYPE,
			"order_date": ["between", [from_date, to_date]],
		},
		fields=["name", "order_group"],
	)
	encounters = sorted({r.order_group for r in requests if r.order_group})
	visits = _visits_by_encounter(encounters)

	rows = []
	for encounter in encounters:
		visit = visits.get(encounter) or frappe._dict()
		if filters.facility and visit.get("facility") != filters.facility:
			continue
		lab = EncounterLab(encounter)
		for request in lab.requests:
			if lab.is_done(request.name):
				continue
			sample_status, sample_collection = lab.sample_status(request.name, request.template_dn)
			result_status, observation = lab.result_status(request.name, request.template_dn)
			rows.append(
				{
					"queue_number": visit.get("queue_number"),
					"visit": visit.get("name"),
					"patient": request.patient,
					"patient_name": request.patient_name,
					"test": request.template_dn,
					"ordered_at": _ordered_at(request),
					"sample_status": sample_status,
					"sample_collection": sample_collection,
					"result_status": result_status,
					"observation": observation,
					"service_request": request.name,
					"visit_status": visit.get("status"),
					"facility": visit.get("facility"),
				}
			)
	rows.sort(key=lambda r: (r["ordered_at"] or get_datetime("1900-01-01"), r["service_request"]))
	return rows


def _ordered_at(request):
	if request.order_date:
		return get_datetime(f"{request.order_date} {request.order_time or '00:00:00'}")
	return request.creation


def _visits_by_encounter(encounters: list[str]) -> dict[str, frappe._dict]:
	if not encounters:
		return {}
	fields = ["name", "encounter", "queue_number", "status", "facility"]
	out = {
		v.encounter: v
		for v in frappe.get_all(VISIT_DOCTYPE, filters={"encounter": ["in", encounters]}, fields=fields)
	}
	missing = [e for e in encounters if e not in out]
	if missing and frappe.get_meta(ENCOUNTER_DOCTYPE).has_field(ENCOUNTER_VISIT_FIELD):
		links = frappe.get_all(
			ENCOUNTER_DOCTYPE,
			filters={"name": ["in", missing], ENCOUNTER_VISIT_FIELD: ["is", "set"]},
			fields=["name", ENCOUNTER_VISIT_FIELD],
		)
		by_visit = {link.get(ENCOUNTER_VISIT_FIELD): link.name for link in links}
		if by_visit:
			for v in frappe.get_all(VISIT_DOCTYPE, filters={"name": ["in", list(by_visit)]}, fields=fields):
				out[by_visit[v.name]] = v
	return out
