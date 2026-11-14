# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 4 install (W0): custom fields and the provisional point-of-care test list. Idempotent."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from onerc_healthcare.constants import PATIENT_OPEN_CONFLICTS_FIELD, POINT_OF_CARE_FIELD

CUSTOM_FIELDS = {
	"Patient": [
		{
			"fieldname": PATIENT_OPEN_CONFLICTS_FIELD,
			"label": "Open Sync Conflicts",
			"fieldtype": "Int",
			"read_only": 1,
			"no_copy": 1,
			"default": "0",
			"insert_after": "status",
			"description": "Outreach records for this patient wait on a Sync Conflict (FR-26).",
		}
	],
	"Observation Template": [
		{
			"fieldname": POINT_OF_CARE_FIELD,
			"label": "Point-of-Care Test (Outreach)",
			"fieldtype": "Check",
			"default": "0",
			"insert_after": "abbr",
			"description": "An outreach team may enter this result in the field (OI-10, D58).",
		}
	],
}

# Provisional (D58): rapid tests an outreach team can run in the field. The HIS Officer / lab lead may tick or
# untick others on the Observation Template; seeding only ever ticks, and only once per template.
POINT_OF_CARE_TESTS = (
	"Malaria Rapid Diagnostic Test",
	"Haemoglobin",
	"Random Blood Sugar",
	"Urine Pregnancy Test",
	"HIV 1/2 Rapid Test",
)
POC_SEED_MARKER = "krcs_outreach_poc_seed_v1"


def install() -> None:
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	seed_point_of_care()


def seed_point_of_care() -> None:
	if frappe.db.get_default(POC_SEED_MARKER):
		return
	for name in POINT_OF_CARE_TESTS:
		if frappe.db.exists("Observation Template", name):
			frappe.db.set_value("Observation Template", name, POINT_OF_CARE_FIELD, 1, update_modified=False)
	frappe.db.set_default(POC_SEED_MARKER, "1")
