# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Laboratory structure and the PROVISIONAL starter catalogue (Marley Observation Template route).

No reference ranges and nothing billable (C-5). Every template is flagged for Lab Technician
review under SDD open item OI-9. Idempotent: creates what is missing, never edits existing rows.
"""

import frappe

from onerc_healthcare.setup.operations import lab_unit_name, load_data


def _ensure(doctype: str, name: str, values: dict, set_name: str | None = None) -> bool:
	if frappe.db.exists(doctype, name):
		return False
	doc = frappe.get_doc({"doctype": doctype, **values})
	if set_name:
		doc.insert(ignore_permissions=True, set_name=set_name)
	else:
		doc.insert(ignore_permissions=True)
	return True


def seed_lab(cfg: dict) -> None:
	"""Seed lab structure for the operation in `cfg` (an operations.json entry) and the catalogue."""
	data = load_data("lab_catalogue.json")
	_structure(cfg, data)
	_catalogue(data)


def _structure(cfg: dict, data: dict) -> None:
	dept, unit_type = data["department"], data["service_unit_type"]
	_ensure("Medical Department", dept, {"department": dept})
	# Marley requires allow_appointments or inpatient_occupancy on a unit type.
	_ensure(
		"Healthcare Service Unit Type",
		unit_type,
		{
			"service_unit_type": unit_type,
			"medical_department": dept,
			"is_billable": 0,
			"allow_appointments": 1,
		},
	)
	abbr = cfg["abbr"]
	for facility in cfg["facilities"]:
		_ensure(
			"Healthcare Service Unit",
			lab_unit_name(facility, abbr),
			{
				"healthcare_service_unit_name": f"{facility['short']} Laboratory",
				"parent_healthcare_service_unit": f"{facility['name']} - {abbr}",
				"company": cfg["company_name"],
				"is_group": 0,
				"service_unit_type": unit_type,
			},
		)
	for row in data["uoms"]:
		_ensure(
			"Lab Test UOM", row["uom"], {"lab_test_uom": row["uom"], "uom_description": row["description"]}
		)
	for row in data["colors"]:
		_ensure("Color", row["name"], {"color": row["hex"]}, set_name=row["name"])
	for sample_type in data["sample_types"]:
		_ensure("Sample Type", sample_type, {"sample_type": sample_type})
	for row in data["samples"]:
		_ensure(
			"Lab Test Sample",
			row["sample"],
			{
				"sample": row["sample"],
				"sample_type": row["sample_type"],
				"container_closure_color": row["color"],
				"sample_uom": "mL",
			},
		)


def _template(data: dict, row: dict, sample: str) -> None:
	s = frappe.db.get_value(
		"Lab Test Sample", sample, ["sample_type", "container_closure_color"], as_dict=True
	)
	_ensure(
		"Observation Template",
		row["name"],
		{
			"observation": row["name"],
			"abbr": row["abbr"],
			"observation_category": "Laboratory",
			"medical_department": data["department"],
			"service_unit": data["service_unit_type"],
			"permitted_data_type": row["data_type"],
			"permitted_unit": row["unit"],
			"options": row["options"],
			"sample_collection_required": 1,
			"sample": sample,
			"sample_type": s.sample_type,
			"container_closure_color": s.container_closure_color,
			"is_billable": 0,
			"description": data["provisional_description"],
		},
	)


def _catalogue(data: dict) -> None:
	for row in data["single_tests"]:
		_template(data, row, row["sample"])
	for panel in data["panels"]:
		for row in panel["new_components"]:
			_template(data, row, panel["sample"])
		_ensure(
			"Observation Template",
			panel["name"],
			{
				"observation": panel["name"],
				"observation_category": "Laboratory",
				"medical_department": data["department"],
				"service_unit": data["service_unit_type"],
				"has_component": 1,
				"is_billable": 0,
				"description": data["provisional_description"],
				"observation_component": [{"observation_template": c} for c in panel["components"]],
			},
		)
