# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Whitelisted registration endpoints (FR-1 to FR-3, SDD 4.3). Permissions are checked here, server-side."""

import frappe
from frappe import _
from frappe.utils import cint

from onerc_healthcare.constants import (
	IDENTIFIER_FACILITY,
	IDENTIFIER_UNHCR,
	PATIENT_IDENTIFIER_DOCTYPE,
	PATIENT_IDENTIFIER_TYPE_DOCTYPE,
	PATIENT_IDENTIFIERS_FIELD,
)
from onerc_healthcare.registration import merge as merge_module
from onerc_healthcare.registration.identifiers import (
	MERGED_INTO_FIELD,
	clean_value,
	duplicate_message,
	find_identifier_owner,
	normalise,
)
from onerc_healthcare.registration.similarity import find_similar
from onerc_healthcare.registration.walk_in import get_default_registration_facility


@frappe.whitelist()
def find_similar_patients(
	first_name: str,
	last_name: str | None = None,
	sex: str | None = None,
	dob: str | None = None,
	residence: str | None = None,
	exclude: str | None = None,
	dob_estimated: int | None = None,
) -> list[dict]:
	"""Possible duplicates of a patient about to be registered, best match first (FR-2).

	`dob_estimated`: the DOB was estimated from an age, so births within ±2 years count as similar."""
	frappe.has_permission("Patient", "read", throw=True)
	return find_similar(
		first_name, last_name, sex, dob, residence, exclude, dob_estimated=bool(cint(dob_estimated))
	)


@frappe.whitelist()
def find_patient_by_identifier(identifier_value: str, identifier_type: str | None = None) -> list[dict]:
	"""Patients holding an identifier (any spelling), with the surviving record for merged ones."""
	frappe.has_permission("Patient", "read", throw=True)
	normalised = normalise(identifier_value)
	if not normalised:
		return []
	filters = {
		"normalised_value": normalised,
		"parenttype": "Patient",
		"parentfield": PATIENT_IDENTIFIERS_FIELD,
	}
	if identifier_type:
		filters["identifier_type"] = identifier_type
	rows = frappe.get_all(
		PATIENT_IDENTIFIER_DOCTYPE,
		filters=filters,
		fields=["parent", "identifier_type", "identifier_value"],
	)
	allowed = set(
		frappe.get_list("Patient", filters={"name": ["in", [r.parent for r in rows]]}, pluck="name")
	)
	out = []
	for row in rows:
		if row.parent not in allowed:
			continue
		info = frappe.db.get_value(
			"Patient", row.parent, ["patient_name", "status", MERGED_INTO_FIELD], as_dict=True
		)
		out.append(
			{
				"patient": row.parent,
				"patient_name": info.patient_name,
				"status": info.status,
				"identifier_type": row.identifier_type,
				"identifier_value": row.identifier_value,
				"merged_into": info.get(MERGED_INTO_FIELD),
				**_surviving_details(_surviving(row.parent)),
			}
		)
	return out


def _surviving_details(patient: str) -> dict:
	"""What the reception search dialog shows for the record to open (REG-24)."""
	info = (
		frappe.db.get_value("Patient", patient, ["patient_name", "sex", "dob", "status"], as_dict=True) or {}
	)
	return {
		"surviving_patient": patient,
		"surviving_patient_name": info.get("patient_name"),
		"surviving_sex": info.get("sex"),
		"surviving_dob": info.get("dob"),
		"surviving_status": info.get("status"),
	}


def _surviving(patient: str) -> str:
	"""Follow merged_into to the record that survived (guards against cycles)."""
	seen = {patient}
	while merged_into := frappe.db.get_value("Patient", patient, MERGED_INTO_FIELD):
		if merged_into in seen:
			break
		seen.add(merged_into)
		patient = merged_into
	return patient


@frappe.whitelist()
def get_registration_defaults() -> dict:
	"""Defaults for a new Patient form: the registering facility (see walk_in module docstring) and
	whether to open the "Find patient by identifier" dialog first (REG-24, SDD 4.3 / 5.1)."""
	frappe.has_permission("Patient", "create", throw=True)
	return {
		"registration_facility": get_default_registration_facility(),
		"search_first_on_new_patient": search_first_on_new_patient(),
		"default_identifier_type": IDENTIFIER_UNHCR,
	}


def search_first_on_new_patient() -> bool:
	"""EMR Registration Settings flag; on (its default) until an admin saves it off."""
	raw = frappe.db.sql(
		"select value from `tabSingles` where doctype = %s and field = %s",
		("EMR Registration Settings", "search_first_on_new_patient"),
	)
	return True if not raw or raw[0][0] is None else bool(cint(raw[0][0]))


@frappe.whitelist(methods=["POST"])
def link_identifier(
	patient: str,
	identifier_type: str,
	identifier_value: str,
	verification_status: str | None = None,
	verification_source: str | None = None,
) -> dict:
	"""Link Identifier action (FR-3): add an identifier, typically a primary one, to an existing patient.

	Returns `{"status": "linked"}`, `{"status": "exists"}` when the patient already holds it, or
	`{"status": "conflict", "existing_patient": ..., "merge_target": ..., "can_merge": ...}` when another
	patient holds it, so the UI can offer a merge.
	"""
	frappe.has_permission("Patient", "write", patient, throw=True)
	doc = frappe.get_doc("Patient", patient)
	if doc.get(MERGED_INTO_FIELD):
		frappe.throw(
			_("Patient {0} was merged into {1}. Link the identifier there.").format(
				patient, doc.get(MERGED_INTO_FIELD)
			),
			title=_("Merged Patient"),
		)
	if identifier_type == IDENTIFIER_FACILITY:
		frappe.throw(
			_("{0} identifiers are assigned by the system and cannot be linked by hand.").format(
				IDENTIFIER_FACILITY
			)
		)
	if not frappe.db.get_value(PATIENT_IDENTIFIER_TYPE_DOCTYPE, identifier_type, "enabled"):
		frappe.throw(
			_("{0} is not an enabled identifier type.").format(frappe.bold(identifier_type)),
			title=_("Invalid Identifier Type"),
		)
	value = clean_value(identifier_value)
	if not normalise(value):
		frappe.throw(_("Enter the identifier value."), title=_("Missing Value"))

	owner = find_identifier_owner(identifier_type, value)
	if owner == patient:
		return {"status": "exists", "patient": patient}
	if owner:
		owner_info = frappe.db.get_value("Patient", owner, ["patient_name", MERGED_INTO_FIELD], as_dict=True)
		return {
			"status": "conflict",
			"patient": patient,
			"existing_patient": owner,
			"existing_patient_name": owner_info.patient_name,
			"existing_merged_into": owner_info.get(MERGED_INTO_FIELD),
			"merge_target": _surviving(owner),
			"can_merge": merge_module.can_merge(),
			"message": duplicate_message(identifier_type, value, owner),
		}

	for row in doc.get(PATIENT_IDENTIFIERS_FIELD) or []:
		if row.identifier_type == identifier_type:
			frappe.throw(
				_("{0} already has {1} {2}. Correct it in the Identifiers table instead.").format(
					doc.patient_name, identifier_type, frappe.bold(row.identifier_value)
				),
				title=_("Identifier Type Already Present"),
			)

	doc.append(
		PATIENT_IDENTIFIERS_FIELD,
		{
			"identifier_type": identifier_type,
			"identifier_value": value,
			"verification_status": verification_status or "Unverified",
			"verification_source": verification_source,
		},
	)
	doc.save()
	return {
		"status": "linked",
		"patient": patient,
		"identifier_type": identifier_type,
		"identifier_value": value,
	}


@frappe.whitelist(methods=["POST"])
def merge_patients(source: str, target: str, reason: str) -> dict:
	"""Non-destructive merge of `source` into `target` (SDD 4.3, NFR-9). Medical In-Charge, HIS Officer,
	System Manager only."""
	if not merge_module.can_merge():
		frappe.throw(
			_("Only the Medical In-Charge, HIS Officer or System Manager can merge patient records."),
			frappe.PermissionError,
		)
	return merge_module.merge_patients(source, target, reason)
