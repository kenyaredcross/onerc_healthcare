# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""WHO ICD-11 MMS block (a grouping of codes), loaded from the bundled release by `coding.icd11`.

Read-only reference data: rows are written set-based by the importer and the backfill, never in the desk.
MOH Diagnosis Mappings of type Block target these ids; Code Value ``krcs_blocks`` lists each code's blocks.
"""

from frappe.model.document import Document


class ICD11Block(Document):
	pass
