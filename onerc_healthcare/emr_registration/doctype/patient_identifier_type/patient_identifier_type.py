# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient Identifier Type master (FR-1, FR-2, C-6)."""

import re

import frappe
from frappe import _
from frappe.model.document import Document

CHILD = "Patient Identifier"


class PatientIdentifierType(Document):
	def validate(self) -> None:
		if self.value_pattern:
			try:
				re.compile(self.value_pattern)
			except re.error as e:
				frappe.throw(
					_("Value Pattern is not a valid regular expression: {0}").format(e),
					title=_("Invalid Value Pattern"),
				)
		if not self.is_new() and self.has_value_changed("is_unique") and self.is_unique:
			self._check_no_shared_values()

	def on_update(self) -> None:
		if self.has_value_changed("is_unique"):
			self._refresh_unique_keys()

	def _check_no_shared_values(self) -> None:
		shared = frappe.db.sql(
			f"""select normalised_value from `tab{CHILD}`
			where identifier_type = %s and ifnull(normalised_value, '') != ''
			group by normalised_value having count(*) > 1 limit 5""",
			(self.name,),
			pluck=True,
		)
		if shared:
			frappe.throw(
				_(
					"{0} cannot be made unique: these values are held by more than one patient: {1}. Merge "
					"or correct those patients first."
				).format(self.name, ", ".join(shared)),
				title=_("Shared Values Exist"),
			)

	def _refresh_unique_keys(self) -> None:
		"""Keep the child `unique_key` (which carries the DB unique index) in step with Is Unique (M9)."""
		value = "normalised_value" if self.is_unique else "null"
		frappe.db.sql(
			f"update `tab{CHILD}` set unique_key = {value} where identifier_type = %s",
			(self.name,),
		)
