# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""clinical install hook (setup/install.py calls install() on after_install and after_migrate). Idempotent.

- Consultation fields on Marley's Patient Encounter (FR-10, FR-11): visit link, clinical notes, ICD-11
  diagnosis table, addendum and the lab-results / history panels.
- Property setters on Patient Encounter, created only when missing (an admin's later change is kept):
  default and hidden appointment type, read-only practitioner, and the Marley parts the EMR does not use
  hidden (orders-on-save, free-text diagnosis, procedures, therapies, insurance, treatment plans).
- The "KRCS Consultation" Appointment Type that satisfies Marley's mandatory field.
- Practitioner User Permissions left on existing sites are removed (D22).

Marley's DocType is never saved; only Custom Field and Property Setter rows are written.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from onerc_healthcare.clinical.practitioner import strip_all_user_permissions
from onerc_healthcare.constants import (
	DEFAULT_APPOINTMENT_TYPE,
	ENCOUNTER_DIAGNOSIS_DOCTYPE,
	ENCOUNTER_DOCTYPE,
	ENCOUNTER_VISIT_FIELD,
	VISIT_DOCTYPE,
)

MODULE = "EMR Clinical"

ENCOUNTER_CUSTOM_FIELDS = {
	ENCOUNTER_DOCTYPE: [
		{
			"fieldname": ENCOUNTER_VISIT_FIELD,
			"label": "Patient Visit",
			"fieldtype": "Link",
			"options": VISIT_DOCTYPE,
			"insert_after": "company",
			"read_only": 1,
			"search_index": 1,
			"no_copy": 1,
			"in_standard_filter": 1,
		},
		{
			"fieldname": "krcs_department",
			"label": "Department",
			"fieldtype": "Link",
			"options": "Medical Department",
			"insert_after": ENCOUNTER_VISIT_FIELD,
			"read_only": 1,
			"in_standard_filter": 1,
		},
		{
			"fieldname": "krcs_clinical_section",
			"label": "Consultation Notes",
			"fieldtype": "Section Break",
			"insert_after": "invoiced",
		},
		{
			"fieldname": "krcs_presenting_complaint",
			"label": "Presenting Complaint",
			"fieldtype": "Small Text",
			"insert_after": "krcs_clinical_section",
			"description": "Pre-filled from the triage chief complaint",
		},
		{
			"fieldname": "krcs_history",
			"label": "History of Presenting Illness",
			"fieldtype": "Text",
			"insert_after": "krcs_presenting_complaint",
		},
		{
			"fieldname": "krcs_clinical_column",
			"fieldtype": "Column Break",
			"insert_after": "krcs_history",
		},
		{
			"fieldname": "krcs_examination_findings",
			"label": "Examination Findings",
			"fieldtype": "Text",
			"insert_after": "krcs_clinical_column",
		},
		{
			"fieldname": "krcs_diagnosis_section",
			"label": "Diagnosis (ICD-11)",
			"fieldtype": "Section Break",
			"insert_after": "krcs_examination_findings",
		},
		{
			"fieldname": "krcs_diagnoses",
			"label": "Diagnoses",
			"fieldtype": "Table",
			"options": ENCOUNTER_DIAGNOSIS_DOCTYPE,
			"insert_after": "krcs_diagnosis_section",
		},
		{
			"fieldname": "krcs_no_diagnosis_reason",
			"label": "Reason for No Diagnosis",
			"fieldtype": "Small Text",
			"insert_after": "krcs_diagnoses",
			"depends_on": "eval:!(doc.krcs_diagnoses || []).length",
			"description": "Required to complete the consultation without a diagnosis",
		},
		{
			"fieldname": "krcs_plan_section",
			"label": "Management Plan",
			"fieldtype": "Section Break",
			"insert_after": "krcs_no_diagnosis_reason",
		},
		{
			"fieldname": "krcs_management_plan",
			"label": "Management Plan",
			"fieldtype": "Text",
			"insert_after": "krcs_plan_section",
		},
		{
			"fieldname": "krcs_lab_results_section",
			"label": "Laboratory Results",
			"fieldtype": "Section Break",
			"insert_after": "lab_test_prescription",
			"depends_on": "eval:!doc.__islocal",
		},
		{
			"fieldname": "krcs_lab_results_html",
			"label": "Laboratory Results",
			"fieldtype": "HTML",
			"insert_after": "krcs_lab_results_section",
		},
		{
			"fieldname": "krcs_addendum_section",
			"label": "Addendum",
			"fieldtype": "Section Break",
			"insert_after": "therapies",
			"depends_on": "eval:doc.docstatus==1",
		},
		{
			"fieldname": "krcs_addendum",
			"label": "Addendum",
			"fieldtype": "Text",
			"insert_after": "krcs_addendum_section",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
			"description": "Corrections after completion; added with Add Addendum and never edited",
		},
		{
			"fieldname": "krcs_history_tab",
			"label": "Patient History",
			"fieldtype": "Tab Break",
			"insert_after": "order_history_html",
		},
		{
			"fieldname": "krcs_history_html",
			"label": "Patient History",
			"fieldtype": "HTML",
			"insert_after": "krcs_history_tab",
		},
	]
}

# (fieldname, property, value, property_type); written only when no setter exists for that pair.
ENCOUNTER_PROPERTIES = (
	("appointment_type", "default", DEFAULT_APPOINTMENT_TYPE, "Text"),
	("appointment_type", "hidden", "1", "Check"),
	("practitioner", "read_only", "1", "Check"),
	("submit_orders_on_save", "hidden", "1", "Check"),
	("diagnosis", "hidden", "1", "Check"),
	("diagnosis_in_print", "hidden", "1", "Check"),
	("codification_table", "read_only", "1", "Check"),
	("sb_procedures", "hidden", "1", "Check"),
	("procedure_prescription", "hidden", "1", "Check"),
	("rehabilitation_section", "hidden", "1", "Check"),
	("therapies", "hidden", "1", "Check"),
	("insurance_section", "hidden", "1", "Check"),
	("insurance_policy", "hidden", "1", "Check"),
	("insurance_payor", "hidden", "1", "Check"),
	("insurance_coverage", "hidden", "1", "Check"),
	("coverage_status", "hidden", "1", "Check"),
	("get_applicable_treatment_plans", "hidden", "1", "Check"),
	("order_history_html", "hidden", "1", "Check"),
)


def install() -> None:
	ensure_appointment_type()
	create_custom_fields(ENCOUNTER_CUSTOM_FIELDS, ignore_validate=True)
	set_encounter_properties()
	strip_all_user_permissions()


def ensure_appointment_type() -> None:
	if frappe.db.exists("Appointment Type", DEFAULT_APPOINTMENT_TYPE):
		return
	frappe.get_doc(
		{
			"doctype": "Appointment Type",
			"appointment_type": DEFAULT_APPOINTMENT_TYPE,
			"allow_booking_for": "Practitioner",
		}
	).insert(ignore_permissions=True)


def _has_property_setter(fieldname: str, prop: str) -> bool:
	return bool(
		frappe.db.exists(
			"Property Setter", {"doc_type": ENCOUNTER_DOCTYPE, "field_name": fieldname, "property": prop}
		)
	)


def set_encounter_properties() -> None:
	meta = frappe.get_meta(ENCOUNTER_DOCTYPE)
	changed = False
	for fieldname, prop, value, prop_type in ENCOUNTER_PROPERTIES:
		if not meta.has_field(fieldname) or _has_property_setter(fieldname, prop):
			continue
		frappe.make_property_setter(
			{
				"doctype": ENCOUNTER_DOCTYPE,
				"doctype_or_field": "DocField",
				"fieldname": fieldname,
				"property": prop,
				"value": value,
				"property_type": prop_type,
			},
			validate_fields_for_doctype=False,
			module=MODULE,
		)
		changed = True
	if changed:
		frappe.clear_cache(doctype=ENCOUNTER_DOCTYPE)
