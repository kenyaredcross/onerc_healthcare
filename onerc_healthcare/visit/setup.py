# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""A2 Visit, queue and triage install hook. Called by setup/install.py on after_install and after_migrate.

Idempotent: `create_custom_fields` creates missing fields and updates ours in place. Never commits.
"""

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
			"label": "Triage Level",
			"fieldtype": "Link",
			"options": "Triage Level",
			"insert_after": "krcs_triage_section",
		},
		{
			"fieldname": "krcs_chief_complaint",
			"label": "Chief Complaint",
			"fieldtype": "Small Text",
			"insert_after": "krcs_triage_level",
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
		},
	]
}


def install() -> None:
	create_custom_fields(VITAL_SIGNS_CUSTOM_FIELDS, ignore_validate=True)
