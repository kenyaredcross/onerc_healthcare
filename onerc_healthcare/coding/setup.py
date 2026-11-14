# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""A3 ICD-11 coding install hook (FR-11, BR-4, SDD §4.5).

Called by setup/install.py on after_install and after_migrate. Idempotent, never commits.
Seeds the ICD-11 Code System, the ICD-11 hierarchy fields on Code Value, and the Healthcare Settings
default code system (only while it is empty), then imports the bundled WHO release once per site and
release (coding.icd11.import_bundled). Phase 3 (R2): the WHO block structure (ICD-11 Block records and
Code Value ``krcs_blocks``) is backfilled once per release on sites imported before it existed
(coding.icd11.ensure_structure; also run by the patch ``patches.icd11_block_backfill``).

Code search (FR-11): Code Value's link search already matches ``name`` (``<code>-ICD-11``),
``code_value`` and the title field ``display``, so keyword and code-number search need no extra hook.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

ICD11_CODE_SYSTEM = "ICD-11"
ICD11_URI = "http://id.who.int/icd/release/11/mms"

CODE_VALUE_FIELDS = {
	"Code Value": [
		{
			"fieldname": "krcs_icd_section",
			"label": "Classification Hierarchy",
			"fieldtype": "Section Break",
			"insert_after": "display",
			"collapsible": 1,
		},
		{
			"fieldname": "krcs_parent_code",
			"label": "Parent Code or Block",
			"fieldtype": "Data",
			"insert_after": "krcs_icd_section",
			"read_only": 1,
			"search_index": 1,
		},
		{
			"fieldname": "krcs_chapter",
			"label": "Chapter",
			"fieldtype": "Data",
			"insert_after": "krcs_parent_code",
			"read_only": 1,
			"in_standard_filter": 1,
		},
		{
			"fieldname": "krcs_is_leaf",
			"label": "Leaf Code",
			"fieldtype": "Check",
			"insert_after": "krcs_chapter",
			"read_only": 1,
		},
		{
			"fieldname": "krcs_blocks",
			"label": "ICD-11 Blocks",
			"fieldtype": "Data",
			"insert_after": "krcs_is_leaf",
			"read_only": 1,
			"description": "WHO block ids of this code's ancestors (Grouping1-5), outermost first, comma-separated. "
			"MOH Diagnosis Mappings of type Block match these.",
		},
		{
			"fieldname": "krcs_primary_tabulation",
			"label": "Primary Tabulation (information only)",
			"fieldtype": "Check",
			"insert_after": "krcs_blocks",
			"read_only": 1,
			"description": "WHO 'Primary tabulation' flag, kept for information. Not used for counting.",
		},
	]
}


def install() -> None:
	create_custom_fields(CODE_VALUE_FIELDS, ignore_validate=True)
	ensure_code_system()
	set_default_code_system()

	from onerc_healthcare.coding.icd11 import ensure_structure, import_bundled

	import_bundled()
	ensure_structure()


def ensure_code_system() -> None:
	if frappe.db.exists("Code System", ICD11_CODE_SYSTEM):
		return
	frappe.get_doc(
		{
			"doctype": "Code System",
			"code_system": ICD11_CODE_SYSTEM,
			"uri": ICD11_URI,
			"description": "WHO ICD-11 for Mortality and Morbidity Statistics (MMS)",
			"is_fhir_defined": 0,
		}
	).insert(ignore_permissions=True)


def set_default_code_system() -> None:
	if not frappe.db.get_single_value("Healthcare Settings", "default_code_system"):
		frappe.db.set_single_value("Healthcare Settings", "default_code_system", ICD11_CODE_SYSTEM)
