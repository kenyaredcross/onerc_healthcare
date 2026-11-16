# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 5M install (W0): maternity custom fields and the Maternity Ward groups. Idempotent, never commits.

- ``Patient.krcs_mother``: the mother of a newborn registered at delivery (M3 sets it; read-only).
- ``Vital Signs.krcs_maternity_admission``: ward observations of an admitted mother. The admitting visit is
  closed (outcome Admitted), so these Vital Signs carry no ``krcs_visit`` and the triage hooks leave them alone.
- One "Maternity Ward - <facility code>" Healthcare Service Unit group under each facility of the operation.
  No beds: the EMR System Administrator adds them (type Bed) under the ward. Marley clears the unit type of a
  group, so the ward is recognised by its name (`ward_for_facility`), not by a "Ward" type.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from onerc_healthcare.constants import (
	MATERNITY_ADMISSION_DOCTYPE,
	PATIENT_MOTHER_FIELD,
	VITAL_SIGNS_ADMISSION_FIELD,
)
from onerc_healthcare.setup.operations import (
	facility_unit_name,
	get_operation,
	get_operation_config,
	maternity_ward_name,
	maternity_ward_unit_name,
)

CUSTOM_FIELDS = {
	"Patient": [
		{
			"fieldname": PATIENT_MOTHER_FIELD,
			"label": "Mother",
			"fieldtype": "Link",
			"options": "Patient",
			"read_only": 1,
			"no_copy": 1,
			"in_standard_filter": 1,
			"search_index": 1,
			"insert_after": "krcs_dob_estimated_on",
			"description": "Set when the baby is registered at delivery (Delivery Record).",
		}
	],
	"Vital Signs": [
		{
			"fieldname": VITAL_SIGNS_ADMISSION_FIELD,
			"label": "Maternity Admission",
			"fieldtype": "Link",
			"options": MATERNITY_ADMISSION_DOCTYPE,
			"no_copy": 1,
			"search_index": 1,
			"insert_after": "krcs_visit",
			"description": "Ward observation of an admitted mother (no visit: the visit closed at admission).",
		}
	],
}


def install() -> None:
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	seed_maternity_wards()


def seed_maternity_wards() -> None:
	"""A "Maternity Ward - <code>" group under each facility that exists. Never edits an existing ward."""
	op = get_operation()
	if not op:
		return
	cfg = get_operation_config(op)
	abbr = cfg["abbr"]
	for facility in cfg["facilities"]:
		parent = facility_unit_name(facility, abbr)
		name = maternity_ward_name(facility, abbr)
		if frappe.db.exists("Healthcare Service Unit", name) or not frappe.db.exists(
			"Healthcare Service Unit", parent
		):
			continue
		frappe.get_doc(
			{
				"doctype": "Healthcare Service Unit",
				"healthcare_service_unit_name": maternity_ward_unit_name(facility),
				"parent_healthcare_service_unit": parent,
				"company": cfg["company_name"],
				"is_group": 1,
			}
		).insert(ignore_permissions=True)


def ward_for_facility(facility: str) -> str | None:
	"""The maternity ward group of facility group `facility` (a Healthcare Service Unit name), or None."""
	op = get_operation()
	if not op:
		return None
	cfg = get_operation_config(op)
	for row in cfg["facilities"]:
		if facility_unit_name(row, cfg["abbr"]) == facility:
			name = maternity_ward_name(row, cfg["abbr"])
			return name if frappe.db.exists("Healthcare Service Unit", name) else None
	return None
