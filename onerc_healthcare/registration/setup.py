# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""A1 Registration and identity install hook. Called by setup/install.py on after_install and after_migrate.

Idempotent: creates what is missing, never overwrites admin edits to seeded records, never commits.
(`frappe.db.add_unique` is DDL, which MariaDB commits implicitly; it runs once, during install/migrate.)
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from onerc_healthcare.constants import (
	IDENTIFIER_ALIEN,
	IDENTIFIER_NATIONAL,
	IDENTIFIER_POL,
	IDENTIFIER_RATION,
	IDENTIFIER_TYPES,
	IDENTIFIER_UNHCR,
	PATIENT_IDENTIFIER_DOCTYPE,
	PATIENT_IDENTIFIER_TYPE_DOCTYPE,
	PATIENT_IDENTIFIERS_FIELD,
	PATIENT_WALK_IN_FIELD,
)
from onerc_healthcare.registration.identifiers import (
	MERGE_LOG_FIELD,
	MERGED_INTO_FIELD,
	OVERRIDE_REASON_FIELD,
	POPULATION_GROUP_FIELD,
	REGISTRATION_FACILITY_FIELD,
	RESIDENCE_FIELD,
	SEARCH_FIELD,
	UNIQUE_CONSTRAINT,
)
from onerc_healthcare.registration.names import FIRST_NAME_KEY_FIELD, LAST_NAME_KEY_FIELD, name_key
from onerc_healthcare.registration.residence import ensure_level_options, ensure_residence_root

MODULE = "EMR Registration"
POPULATION_GROUPS = "\nRefugee\nNational\nOther"

SETTINGS_DOCTYPE = "EMR Registration Settings"
UNIQUE_INDEX_COLUMNS = ["identifier_type", "unique_key"]
# Bump when a one-off update of already-seeded data is added to _apply_seed_updates().
SEED_VERSION = 2

# Shared identifiers (M9): household ration cards are held by every family member.
NON_UNIQUE_IDENTIFIER_TYPES = (IDENTIFIER_RATION,)

# Default population group per identifier type (FR-1, C-6). Facility-generated has none.
IDENTIFIER_POPULATION_GROUP = {
	IDENTIFIER_UNHCR: "Refugee",
	IDENTIFIER_POL: "Refugee",
	IDENTIFIER_ALIEN: "Refugee",
	IDENTIFIER_RATION: "Refugee",
	IDENTIFIER_NATIONAL: "National",
}

PATIENT_CUSTOM_FIELDS = {
	"Patient": [
		{
			"fieldname": "krcs_identification_section",
			"label": "Identification",
			"fieldtype": "Section Break",
			"insert_after": "user_id",
		},
		{
			"fieldname": PATIENT_WALK_IN_FIELD,
			"label": "Walk-in (no identifier)",
			"fieldtype": "Check",
			"insert_after": "krcs_identification_section",
			"description": "Tick when the patient holds no UNHCR ID, Proof of Life Number or Alien ID. A "
			"facility identifier is generated on save (FR-3).",
		},
		{
			"fieldname": REGISTRATION_FACILITY_FIELD,
			"label": "Registration Facility",
			"fieldtype": "Link",
			"options": "Healthcare Service Unit",
			"insert_after": PATIENT_WALK_IN_FIELD,
			"ignore_user_permissions": 1,
			"description": "Facility where the patient was registered; its code prefixes walk-in identifiers.",
		},
		{
			"fieldname": "krcs_identification_column",
			"fieldtype": "Column Break",
			"insert_after": REGISTRATION_FACILITY_FIELD,
		},
		{
			"fieldname": POPULATION_GROUP_FIELD,
			"label": "Population Group",
			"fieldtype": "Select",
			"options": POPULATION_GROUPS,
			"insert_after": "krcs_identification_column",
			"in_standard_filter": 1,
			"description": "Defaults from the identifier type when left empty.",
		},
		{
			"fieldname": RESIDENCE_FIELD,
			"label": "Residence",
			"fieldtype": "Link",
			"options": "Residence Location",
			"insert_after": POPULATION_GROUP_FIELD,
			"ignore_user_permissions": 1,
		},
		{
			"fieldname": "krcs_identifiers_section",
			"fieldtype": "Section Break",
			"insert_after": RESIDENCE_FIELD,
		},
		{
			"fieldname": PATIENT_IDENTIFIERS_FIELD,
			"label": "Identifiers",
			"fieldtype": "Table",
			"options": PATIENT_IDENTIFIER_DOCTYPE,
			"insert_after": "krcs_identifiers_section",
			"no_copy": 1,
			"description": "At least one of UNHCR ID, Proof of Life Number or Alien ID unless walk-in (FR-1).",
		},
		{
			"fieldname": SEARCH_FIELD,
			"label": "Identifier Search",
			"fieldtype": "Small Text",
			"insert_after": PATIENT_IDENTIFIERS_FIELD,
			"hidden": 1,
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			"report_hide": 1,
		},
		{
			"fieldname": FIRST_NAME_KEY_FIELD,
			"label": "First Name Key",
			"fieldtype": "Data",
			"insert_after": SEARCH_FIELD,
			"hidden": 1,
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			"report_hide": 1,
			"search_index": 1,
			"description": "Phonetic key for the duplicate check (FR-2).",
		},
		{
			"fieldname": LAST_NAME_KEY_FIELD,
			"label": "Last Name Key",
			"fieldtype": "Data",
			"insert_after": FIRST_NAME_KEY_FIELD,
			"hidden": 1,
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			"report_hide": 1,
			"search_index": 1,
			"description": "Phonetic key for the duplicate check (FR-2).",
		},
		{
			"fieldname": "krcs_merge_section",
			"label": "Duplicate Check and Merge",
			"fieldtype": "Section Break",
			"insert_after": LAST_NAME_KEY_FIELD,
			"collapsible": 1,
			"depends_on": f"eval:doc.{MERGED_INTO_FIELD} || doc.{OVERRIDE_REASON_FIELD}",
		},
		{
			"fieldname": MERGED_INTO_FIELD,
			"label": "Merged Into",
			"fieldtype": "Link",
			"options": "Patient",
			"insert_after": "krcs_merge_section",
			"read_only": 1,
			"no_copy": 1,
			"search_index": 1,
			"ignore_user_permissions": 1,
			"description": "Set by the Merge Patients action. This record is kept, disabled (NFR-9).",
		},
		{
			"fieldname": OVERRIDE_REASON_FIELD,
			"label": "Reason for Registering Despite Possible Matches",
			"fieldtype": "Small Text",
			"insert_after": MERGED_INTO_FIELD,
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": MERGE_LOG_FIELD,
			"label": "Merge Log",
			"fieldtype": "Long Text",
			"insert_after": OVERRIDE_REASON_FIELD,
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
			"depends_on": f"eval:doc.{MERGED_INTO_FIELD}",
			"description": "JSON list of every record moved by the merge: doctype, name and field (NFR-9).",
		},
	]
}


def install() -> None:
	seed_identifier_types()
	_apply_seed_updates()
	create_custom_fields(PATIENT_CUSTOM_FIELDS, ignore_validate=True)
	set_patient_properties()
	backfill_name_keys()
	ensure_identifier_unique_index()
	ensure_level_options()
	ensure_residence_root()


def seed_identifier_types() -> None:
	for itype in IDENTIFIER_TYPES:
		if frappe.db.exists(PATIENT_IDENTIFIER_TYPE_DOCTYPE, itype["name"]):
			continue
		frappe.get_doc(
			{
				"doctype": PATIENT_IDENTIFIER_TYPE_DOCTYPE,
				"identifier_type_name": itype["name"],
				"is_primary": itype["is_primary"],
				"is_unique": 0 if itype["name"] in NON_UNIQUE_IDENTIFIER_TYPES else 1,
				"population_group": IDENTIFIER_POPULATION_GROUP.get(itype["name"]),
				"enabled": 1,
			}
		).insert(ignore_permissions=True)


def _apply_seed_updates() -> None:
	"""One-off updates of data seeded by earlier versions, applied once per site (tracked in settings)."""
	version = frappe.utils.cint(frappe.db.get_single_value(SETTINGS_DOCTYPE, "seed_version", cache=False))
	if version >= SEED_VERSION:
		return
	if version < 2:
		# M9: Is Unique was added with default 1; ration cards are household identifiers.
		for name in NON_UNIQUE_IDENTIFIER_TYPES:
			if frappe.db.exists(PATIENT_IDENTIFIER_TYPE_DOCTYPE, name):
				frappe.db.set_value(
					PATIENT_IDENTIFIER_TYPE_DOCTYPE, name, "is_unique", 0, update_modified=False
				)
	frappe.db.set_single_value(SETTINGS_DOCTYPE, "seed_version", SEED_VERSION)


def backfill_name_keys() -> None:
	"""Phonetic name keys for patients saved before the keys existed (or changed by direct DB work)."""
	rows = frappe.db.sql(
		f"""select name, first_name, last_name, `{FIRST_NAME_KEY_FIELD}` as fk, `{LAST_NAME_KEY_FIELD}` as lk
		from `tabPatient` where `{FIRST_NAME_KEY_FIELD}` is null or `{LAST_NAME_KEY_FIELD}` is null""",
		as_dict=True,
	)
	for row in rows:
		frappe.db.set_value(
			"Patient",
			row.name,
			{
				FIRST_NAME_KEY_FIELD: name_key(row.first_name),
				LAST_NAME_KEY_FIELD: name_key(row.last_name),
			},
			update_modified=False,
		)


def backfill_unique_keys() -> None:
	"""`unique_key` = normalised value for unique identifier types, NULL for shared ones (M9)."""
	frappe.db.sql(
		f"""update `tab{PATIENT_IDENTIFIER_DOCTYPE}` pi
		left join `tab{PATIENT_IDENTIFIER_TYPE_DOCTYPE}` t on t.name = pi.identifier_type
		set pi.unique_key = if(ifnull(t.is_unique, 1) = 1 and ifnull(pi.normalised_value, '') != '',
			pi.normalised_value, null)
		where not (pi.unique_key <=> if(ifnull(t.is_unique, 1) = 1 and ifnull(pi.normalised_value, '') != '',
			pi.normalised_value, null))"""
	)


def _has_property_setter(doctype: str, prop: str, fieldname: str | None = None) -> bool:
	filters = {"doc_type": doctype, "property": prop}
	if fieldname:
		filters["field_name"] = fieldname
	return bool(frappe.db.exists("Property Setter", filters))


def set_patient_properties() -> None:
	"""Full form instead of quick entry (identifiers + duplicate check); identifiers searchable (FR-2)."""
	if not _has_property_setter("Patient", "quick_entry"):
		frappe.make_property_setter(
			{
				"doctype": "Patient",
				"doctype_or_field": "DocType",
				"property": "quick_entry",
				"value": "0",
				"property_type": "Check",
			},
			validate_fields_for_doctype=False,
			module=MODULE,
		)
	search_fields = [
		f.strip() for f in (frappe.get_meta("Patient").search_fields or "").split(",") if f.strip()
	]
	if SEARCH_FIELD not in search_fields:
		frappe.make_property_setter(
			{
				"doctype": "Patient",
				"doctype_or_field": "DocType",
				"property": "search_fields",
				"value": ",".join([*search_fields, SEARCH_FIELD]),
				"property_type": "Data",
			},
			validate_fields_for_doctype=False,
			module=MODULE,
		)


def _index_columns(table: str, index: str) -> list[str]:
	rows = frappe.db.sql(f"show index from `{table}` where Key_name = %s", (index,), as_dict=True)
	return [r.Column_name for r in sorted(rows, key=lambda r: r.Seq_in_index)]


def ensure_identifier_unique_index() -> None:
	"""DB unique index on (identifier_type, unique_key) of Patient Identifier (FR-2, SDD 4.3, M9).

	Replaces the earlier (identifier_type, normalised_value) index of the same name, but only once the
	new one can be created: if duplicates exist the old index is kept and a warning is logged.
	"""
	table = f"tab{PATIENT_IDENTIFIER_DOCTYPE}"
	backfill_unique_keys()
	columns = _index_columns(table, UNIQUE_CONSTRAINT)
	if columns == UNIQUE_INDEX_COLUMNS:
		return
	duplicates = frappe.db.sql(
		f"""select identifier_type, unique_key, count(*) from `{table}`
		where unique_key is not null
		group by identifier_type, unique_key having count(*) > 1 limit 5"""
	)
	if duplicates:
		message = (
			f"onerc_healthcare: unique index {UNIQUE_CONSTRAINT} on {UNIQUE_INDEX_COLUMNS} NOT created; "
			f"resolve duplicate patient identifiers first (merge the patients), then run bench migrate. "
			f"Examples: {duplicates}"
		)
		print(message)
		frappe.log_error("Patient identifier unique index not created", message)
		return
	if columns:
		frappe.db.sql_ddl(f"alter table `{table}` drop index `{UNIQUE_CONSTRAINT}`")
	frappe.db.add_unique(PATIENT_IDENTIFIER_DOCTYPE, UNIQUE_INDEX_COLUMNS, constraint_name=UNIQUE_CONSTRAINT)
