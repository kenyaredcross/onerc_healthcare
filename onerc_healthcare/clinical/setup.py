# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""clinical install hook (setup/install.py calls install() on after_install and after_migrate). Idempotent.

- Consultation fields on Marley's Patient Encounter (FR-10, FR-11): visit link, clinical notes, ICD-11
  diagnosis table, addendum and the lab-results / history panels; `krcs_withdrawn` on Lab Prescription.
- Mental Health consultation note (FR-13, provisional D63): `krcs_mh_*` fields shown on Mental Health
  encounters only, between the clinical notes and the diagnoses.
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
from onerc_healthcare.clinical.utils import LAB_WITHDRAWN_FIELD
from onerc_healthcare.constants import (
	DEFAULT_APPOINTMENT_TYPE,
	DEPT_MENTAL_HEALTH,
	ENCOUNTER_DIAGNOSIS_DOCTYPE,
	ENCOUNTER_DOCTYPE,
	ENCOUNTER_VISIT_FIELD,
	VISIT_DOCTYPE,
)

MODULE = "EMR Clinical"

# Mental Health consultation note (FR-13, provisional format D63 pending OI-7): Dadaab Mental Health
# Admission Form headings (SDD §5.12.4) plus a standard outpatient psychiatric assessment. Shown only on a
# Mental Health encounter; rules in clinical/mh_note.py. None is allow_on_submit: corrections are addenda.
MH_DEPENDS_ON = f"eval:doc.krcs_department=='{DEPT_MENTAL_HEALTH}'"
RISK_LEVELS = "\nNone identified\nLow\nModerate\nHigh"
INSIGHT_LEVELS = "\nGood\nPartial\nPoor"


def _mh_fields(*specs: tuple) -> list[dict]:
	"""(fieldname, label, fieldtype, extra) specs chained by insert_after, starting after the exam findings."""
	fields, after = [], "krcs_examination_findings"
	for fieldname, label, fieldtype, extra in specs:
		df = {
			"fieldname": fieldname,
			"fieldtype": fieldtype,
			"insert_after": after,
			"depends_on": MH_DEPENDS_ON,
			"print_hide": 1,  # DEFECT-MH-3: the note stays out of print formats until OI-7 decides (D63)
		}
		if label:
			df["label"] = label
		df.update(extra)
		fields.append(df)
		after = fieldname
	return fields


MH_NOTE_FIELDS = _mh_fields(
	("krcs_mh_section", "Mental Health Assessment", "Section Break", {}),
	(
		"krcs_mh_visit_type",
		"Visit Type",
		"Select",
		{"options": "\nNew assessment\nFollow-up review"},
	),
	(
		"krcs_mh_referral_source",
		"Referral Source",
		"Select",
		{
			"options": "\nSelf\nFamily\nCommunity health worker\nClinician\nSchool\nPolice or court\nOther",
		},
	),
	("krcs_mh_onset_duration", "Onset and Duration", "Data", {}),
	("krcs_mh_assessment_column", None, "Column Break", {}),
	(
		"krcs_mh_past_psychiatric",
		"Past Psychiatric and Medical History",
		"Small Text",
		{"description": "Previous episodes, admissions and treatment; medical history"},
	),
	("krcs_mh_substance_use", "Substance Use", "Small Text", {}),
	(
		"krcs_mh_personal_social",
		"Personal, Family and Social History",
		"Small Text",
		{"description": "Including displacement and trauma"},
	),
	("krcs_mh_mse_section", "Mental State Examination", "Section Break", {}),
	("krcs_mh_appearance_behaviour", "Appearance and Behaviour", "Small Text", {}),
	("krcs_mh_speech", "Speech", "Small Text", {}),
	(
		"krcs_mh_mood",
		"Mood",
		"Data",
		{"description": "Required to complete the consultation"},
	),
	("krcs_mh_affect", "Affect", "Data", {}),
	("krcs_mh_thought_form", "Thought Form", "Small Text", {}),
	("krcs_mh_mse_column", None, "Column Break", {}),
	("krcs_mh_thought_content", "Thought Content", "Small Text", {}),
	("krcs_mh_perception", "Perception", "Small Text", {}),
	("krcs_mh_cognition", "Cognition", "Small Text", {}),
	("krcs_mh_insight", "Insight", "Select", {"options": INSIGHT_LEVELS}),
	("krcs_mh_judgement", "Judgement", "Select", {"options": INSIGHT_LEVELS}),
	("krcs_mh_risk_section", "Risk Assessment", "Section Break", {}),
	(
		"krcs_mh_suicide_risk",
		"Suicide Risk",
		"Select",
		{"options": RISK_LEVELS, "description": "Required to complete the consultation"},
	),
	("krcs_mh_recent_self_harm", "Recent Self-Harm", "Check", {}),
	(
		"krcs_mh_harm_to_others",
		"Risk of Harm to Others",
		"Select",
		{"options": RISK_LEVELS, "description": "Required to complete the consultation"},
	),
	("krcs_mh_risk_column", None, "Column Break", {}),
	("krcs_mh_risk_notes", "Risk Notes", "Small Text", {}),
	(
		"krcs_mh_safety_plan",
		"Safety Plan",
		"Small Text",
		{"description": "Required when a risk is High or there is recent self-harm"},
	),
	("krcs_mh_screening_section", "Screening Scores (optional)", "Section Break", {}),
	(
		"krcs_mh_phq9",
		"PHQ-9 Score",
		"Data",
		{
			"description": "0 to 27; leave blank if not done. 0-4 minimal, 5-9 mild, 10-14 moderate, "
			"15-19 moderately severe, 20-27 severe",
		},
	),
	("krcs_mh_screening_column", None, "Column Break", {}),
	(
		"krcs_mh_gad7",
		"GAD-7 Score",
		"Data",
		{
			"description": "0 to 21; leave blank if not done. 0-4 minimal, 5-9 mild, 10-14 moderate, "
			"15-21 severe",
		},
	),
	("krcs_mh_plan_section", "Formulation and Plan", "Section Break", {}),
	("krcs_mh_formulation", "Formulation", "Small Text", {}),
	(
		"krcs_mh_interventions",
		"Interventions",
		"Small Text",
		{"description": "Psychoeducation, counselling, medication, referral"},
	),
	("krcs_mh_plan_column", None, "Column Break", {}),
	("krcs_mh_follow_up_date", "Follow-up Date", "Date", {}),
)

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
		*MH_NOTE_FIELDS,
		{
			"fieldname": "krcs_diagnosis_section",
			"label": "Diagnosis (ICD-11)",
			"fieldtype": "Section Break",
			"insert_after": MH_NOTE_FIELDS[-1]["fieldname"],
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

# Unsent lab rows of a visit closed before they were sent are flagged, never deleted (L4).
LAB_PRESCRIPTION_CUSTOM_FIELDS = {
	"Lab Prescription": [
		{
			"fieldname": LAB_WITHDRAWN_FIELD,
			"label": "Withdrawn",
			"fieldtype": "Check",
			"insert_after": "service_request",
			"read_only": 1,
			"no_copy": 1,
			"in_list_view": 0,
			"description": "Not sent: the visit was closed first",
		}
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
	# Not used by KRCS (walk-in visits, no inpatient or emergency module yet): the links would only open
	# searches the clinical roles cannot run.
	("appointment", "hidden", "1", "Check"),
	("inpatient_record", "hidden", "1", "Check"),
	("emergency_record", "hidden", "1", "Check"),
)

# Lab Prescription rows: tests are ordered by Observation Template; Marley's legacy Lab Test template and
# care type columns are not used (D5).
LAB_PRESCRIPTION_PROPERTIES = (
	("lab_test_code", "hidden", "1", "Check"),
	("patient_care_type", "hidden", "1", "Check"),
)


def install() -> None:
	ensure_appointment_type()
	create_custom_fields(ENCOUNTER_CUSTOM_FIELDS, ignore_validate=True)
	create_custom_fields(LAB_PRESCRIPTION_CUSTOM_FIELDS, ignore_validate=True)
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


def _has_property_setter(fieldname: str, prop: str, doctype: str = ENCOUNTER_DOCTYPE) -> bool:
	return bool(
		frappe.db.exists("Property Setter", {"doc_type": doctype, "field_name": fieldname, "property": prop})
	)


def set_encounter_properties() -> None:
	changed = False
	for doctype, properties in (
		(ENCOUNTER_DOCTYPE, ENCOUNTER_PROPERTIES),
		("Lab Prescription", LAB_PRESCRIPTION_PROPERTIES),
	):
		changed |= _set_properties(doctype, properties)
	if changed:
		frappe.clear_cache(doctype=ENCOUNTER_DOCTYPE)


def _set_properties(doctype: str, properties) -> bool:
	meta = frappe.get_meta(doctype)
	changed = False
	for fieldname, prop, value, prop_type in properties:
		if not meta.has_field(fieldname) or _has_property_setter(fieldname, prop, doctype):
			continue
		frappe.make_property_setter(
			{
				"doctype": doctype,
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
	return changed
