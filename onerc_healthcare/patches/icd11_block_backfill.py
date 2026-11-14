# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 3 (R2): backfill the WHO ICD-11 block structure on sites that imported ICD-11 before it existed.

Creates the Code Value fields ``krcs_blocks`` / ``krcs_primary_tabulation``, then loads the ICD-11 Block
records and each code's block ids from the bundled release with set-based updates
(`coding.icd11.ensure_structure`). Idempotent: `coding.setup.install()` runs the same step on every
migrate and skips it once the release's structure is loaded. Never commits (the patch runner does).
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
	from onerc_healthcare.coding.icd11 import ensure_structure
	from onerc_healthcare.coding.setup import CODE_VALUE_FIELDS

	frappe.reload_doc("emr_reporting", "doctype", "icd_11_block")
	create_custom_fields(CODE_VALUE_FIELDS, ignore_validate=True)
	ensure_structure()
