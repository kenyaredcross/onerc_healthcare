# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""A2 Visit, queue and triage install hook. Called by setup/install.py on after_install and after_migrate.

Idempotent: `create_custom_fields` creates missing fields and updates ours in place, and composite
indexes are added only when missing. Never commits explicitly (adding a missing index is DDL, which
MariaDB commits implicitly; on an up-to-date site nothing is altered).
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from onerc_healthcare.constants import VISIT_DOCTYPE

# Triage findings on Marley's Vital Signs (FR-9).
VITAL_SIGNS_CUSTOM_FIELDS = {
	"Vital Signs": [
		{
			"fieldname": "krcs_visit",
			"label": "Patient Visit",
			"fieldtype": "Link",
			"options": VISIT_DOCTYPE,
			"insert_after": "encounter",
			"search_index": 1,
			"no_copy": 1,
			"description": "Submitting starts triage on a Registered visit",
		},
		{
			"fieldname": "krcs_triage_section",
			"label": "Triage Assessment",
			"fieldtype": "Section Break",
			"insert_after": "vital_signs_note",
		},
		{
			"fieldname": "krcs_triage_level",
			"mandatory_depends_on": "eval:doc.krcs_visit",
			"label": "Triage Level",
			"fieldtype": "Link",
			"options": "Triage Level",
			"insert_after": "krcs_triage_section",
			"description": "Required to submit triage vital signs for a visit",
		},
		{
			"fieldname": "krcs_chief_complaint",
			"mandatory_depends_on": "eval:doc.krcs_visit",
			"label": "Chief Complaint",
			"fieldtype": "Small Text",
			"insert_after": "krcs_triage_level",
			"description": "Required to submit triage vital signs for a visit (max 500 characters)",
		},
		{
			"fieldname": "krcs_triage_column",
			"fieldtype": "Column Break",
			"insert_after": "krcs_chief_complaint",
		},
		{
			"fieldname": "krcs_initial_assessment",
			"label": "Initial Assessment",
			"fieldtype": "Small Text",
			"insert_after": "krcs_triage_column",
			"description": "Max 5000 characters",
		},
	]
}


# Composite indexes for the queue, workspace and KPI queries (NFR-01). Single-column indexes on
# patient, facility, visit_date, status and current_department come from `search_index` in the JSON.
VISIT_INDEXES = {
	# get_queue: facility = ? AND status IN (...) ORDER BY visit_date, queue_number
	"krcs_pv_facility_status_date": ("facility", "status", "visit_date", "queue_number"),
	# one open visit per patient per day: patient = ? AND visit_date = ? AND status NOT IN (...)
	"krcs_pv_patient_date_status": ("patient", "visit_date", "status"),
	# longest_wait_card and number cards: status IN (...) AND visit_date = ? ORDER BY arrived_at
	"krcs_pv_date_status_arrived": ("visit_date", "status", "arrived_at"),
	# department workspaces: current_department = ? AND status IN (...)
	"krcs_pv_department_status": ("current_department", "status"),
}


def install() -> None:
	from onerc_healthcare.visit import workspaces

	create_custom_fields(VITAL_SIGNS_CUSTOM_FIELDS, ignore_validate=True)
	ensure_visit_indexes()
	workspaces.install()


def ensure_visit_indexes() -> None:
	"""Add the composite Patient Visit indexes that are missing (NFR-01)."""
	if not frappe.db.table_exists(VISIT_DOCTYPE):
		return
	table = f"tab{VISIT_DOCTYPE}"
	for index_name, columns in VISIT_INDEXES.items():
		if not frappe.db.has_index(table, index_name):
			frappe.db.add_index(VISIT_DOCTYPE, list(columns), index_name)
