# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""ICD-11 code, prefix or block counted under an MOH Disease Category (PROVISIONAL — OI-5).

Precedence and seeds: `reporting.mapping`. The name is ``<EXACT|PREFIX|BLOCK>-<value>``, so a value
has at most one mapping per match type; match type and value are set once (untick Enabled instead of
deleting).
"""

import frappe
from frappe import _
from frappe.model.document import Document

from onerc_healthcare.constants import MOH_MAPPING_DOCTYPE
from onerc_healthcare.reporting import mapping


class MOHDiagnosisMapping(Document):
	def autoname(self) -> None:
		self.value = mapping.normalise_value(self.match_type, self.value)
		self.name = mapping.mapping_name(self.match_type, self.value)
		existing = frappe.db.get_value(MOH_MAPPING_DOCTYPE, self.name, "category")
		if existing:
			frappe.throw(
				_("{0} {1} is already mapped to {2}.").format(
					self.match_type, self.value, frappe.bold(existing)
				),
				frappe.DuplicateEntryError,
			)

	def validate(self) -> None:
		if self.match_type not in mapping.MATCH_TYPES:
			frappe.throw(_("Unknown match type {0}.").format(self.match_type))
		self.value = mapping.normalise_value(self.match_type, self.value)
		count, title = mapping.value_info(self.match_type, self.value)
		if not count and self.match_type != mapping.MATCH_BLOCK:
			frappe.throw(self._missing_message())
		if title is None:
			frappe.throw(self._missing_message())
		self.value_title = (title or "")[:255]
		self.matching_codes = count
		mapping.mark_edited(self, mapping.MAPPING_SEED_FIELDS)

	def _missing_message(self) -> str:
		if self.match_type == mapping.MATCH_EXACT:
			return _("{0} is not an ICD-11 code.").format(frappe.bold(self.value))
		if self.match_type == mapping.MATCH_PREFIX:
			return _("No ICD-11 code starts with {0}.").format(frappe.bold(self.value))
		return _("{0} is not an ICD-11 Block ID (for example BlockL2-1F4).").format(frappe.bold(self.value))
