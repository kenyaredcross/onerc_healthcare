# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Partograph (INP-15). Rules live in `onerc_healthcare.maternity.partograph` (M2): one per admission, rows
appended only through `add_observation`, computed fields kept by the server."""

from frappe.model.document import Document


class Partograph(Document):
	def validate(self):
		from onerc_healthcare.maternity.partograph import validate_partograph

		validate_partograph(self)
