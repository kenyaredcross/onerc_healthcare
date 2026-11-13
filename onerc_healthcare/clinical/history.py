# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient history panel on the consultation (FR-10, SDD §5.3).

`get_patient_history(patient)` returns the patient's completed consultations with their diagnoses,
approved laboratory results (B2's `lab.api.get_encounter_results`), prescriptions with their dispensing
status (B3's `krcs_dispense_status` on Medication Request) and referrals. The B2 and B3 parts are
optional: until they exist the panel shows the rest.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from onerc_healthcare.clinical.diagnosis import DIAGNOSES_FIELD
from onerc_healthcare.constants import (
	ENCOUNTER_DIAGNOSIS_DOCTYPE,
	ENCOUNTER_DOCTYPE,
	ENCOUNTER_VISIT_FIELD,
	REFERRAL_DOCTYPE,
)

LAB_RESULTS_API = "onerc_healthcare.lab.api.get_encounter_results"
DISPENSE_STATUS_FIELD = "krcs_dispense_status"
DEFAULT_LIMIT = 20


def _lab_results_fn():
	try:
		return frappe.get_attr(LAB_RESULTS_API)
	except (ImportError, AttributeError):
		return None


@frappe.whitelist(methods=["GET"])
def get_patient_history(patient: str, exclude: str | None = None, limit: int = DEFAULT_LIMIT) -> dict:
	"""Completed consultations of `patient`, newest first (at most `limit`), excluding `exclude`."""
	frappe.has_permission("Patient", "read", doc=patient, throw=True)
	frappe.has_permission(ENCOUNTER_DOCTYPE, "read", throw=True)
	filters = {"patient": patient, "docstatus": 1}
	if exclude:
		filters["name"] = ["!=", exclude]
	encounters = frappe.get_list(
		ENCOUNTER_DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"encounter_date",
			"encounter_time",
			"practitioner_name",
			"krcs_department as department",
			f"{ENCOUNTER_VISIT_FIELD} as visit",
			"krcs_presenting_complaint as presenting_complaint",
			"krcs_management_plan as management_plan",
			"krcs_no_diagnosis_reason as no_diagnosis_reason",
			"krcs_addendum as addendum",
		],
		order_by="encounter_date desc, encounter_time desc, creation desc",
		limit_page_length=max(1, min(cint(limit) or DEFAULT_LIMIT, 100)),
	)
	names = [e.name for e in encounters]
	diagnoses = _group(
		frappe.get_all(
			ENCOUNTER_DIAGNOSIS_DOCTYPE,
			filters={
				"parent": ["in", names or [""]],
				"parenttype": ENCOUNTER_DOCTYPE,
				"parentfield": DIAGNOSES_FIELD,
			},
			fields=["parent", "code_value", "title", "case_type", "is_primary"],
			order_by="is_primary desc, idx asc",
		)
	)
	prescriptions = _group(_prescriptions(names))
	referrals = _group(
		frappe.get_all(
			REFERRAL_DOCTYPE,
			filters={"source_encounter": ["in", names or [""]]},
			fields=[
				"source_encounter as parent",
				"name",
				"referral_type",
				"to_department",
				"to_facility",
				"to_facility_name",
				"urgency",
				"status",
				"outcome",
			],
		)
	)
	lab_fn = _lab_results_fn()
	for enc in encounters:
		enc.diagnoses = diagnoses.get(enc.name, [])
		enc.prescriptions = prescriptions.get(enc.name, [])
		enc.referrals = referrals.get(enc.name, [])
		enc.lab_results = _lab_results(lab_fn, enc.name)
	return {"encounters": encounters, "lab_results_available": bool(lab_fn)}


@frappe.whitelist(methods=["GET"])
def get_encounter_lab_results(encounter: str) -> dict:
	"""Approved results of one consultation's lab orders (B2), plus the order list for the panel."""
	frappe.has_permission(ENCOUNTER_DOCTYPE, "read", doc=encounter, throw=True)
	lab_fn = _lab_results_fn()
	orders = frappe.get_all(
		"Service Request",
		filters={"order_group": encounter, "source_doc": ENCOUNTER_DOCTYPE},
		# Service Request status is deliberately left out: Marley sets "completed" at sample collection,
		# before any result exists. Only docstatus (sent / cancelled) is shown.
		fields=["name", "template_dn as test", "docstatus", "order_date"],
		order_by="creation asc",
	)
	return {"available": bool(lab_fn), "results": _lab_results(lab_fn, encounter), "orders": orders}


def _lab_results(lab_fn, encounter: str):
	if not lab_fn:
		return None
	try:
		return lab_fn(encounter)
	except frappe.PermissionError:
		frappe.clear_last_message()
		return None


def _prescriptions(encounters: list[str]) -> list[dict]:
	if not encounters:
		return []
	rows = frappe.get_all(
		"Drug Prescription",
		filters={"parent": ["in", encounters], "parenttype": ENCOUNTER_DOCTYPE},
		fields=[
			"parent",
			"medication",
			"drug_code",
			"drug_name",
			"dosage",
			"period",
			"dosage_form",
			"medication_request",
		],
		order_by="idx asc",
	)
	requests = [r.medication_request for r in rows if r.medication_request]
	status = {}
	if requests and frappe.get_meta("Medication Request").has_field(DISPENSE_STATUS_FIELD):
		status = dict(
			frappe.get_all(
				"Medication Request",
				filters={"name": ["in", requests]},
				fields=["name", DISPENSE_STATUS_FIELD],
				as_list=True,
			)
		)
	for row in rows:
		row.dispense_status = status.get(row.medication_request) or (
			_("Pending") if row.medication_request else _("Not released")
		)
	return rows


def _group(rows) -> dict[str, list]:
	out: dict[str, list] = {}
	for row in rows:
		out.setdefault(row.pop("parent"), []).append(row)
	return out
