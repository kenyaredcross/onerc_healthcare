# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Partograph (INP-15). Rules live in `onerc_healthcare.maternity.partograph` (M2): one per admission, rows
appended only through `add_observation`, computed fields kept by the server."""

import frappe
from frappe.model.document import Document


class Partograph(Document):
	def validate(self):
		from onerc_healthcare.maternity.partograph import validate_partograph

		validate_partograph(self)


def on_doctype_update():
	"""One Partograph per Maternity Admission, enforced by a unique index on `admission` (the search index
	stays). Frappe's own `unique` flag would be skipped here: an index named `admission` already exists."""
	if not frappe.db.get_column_index("tabPartograph", "admission", unique=True):
		frappe.db.add_unique("Partograph", ["admission"], constraint_name="unique_partograph_admission")
