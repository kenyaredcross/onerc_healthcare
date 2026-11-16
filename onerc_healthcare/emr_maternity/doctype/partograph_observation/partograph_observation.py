# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""One hourly partograph row (append-only; added through maternity.partograph.add_observation).

The parent's validate guards the rows; these methods run only when a row is saved or deleted on its own
(e.g. `frappe.client.save` / `frappe.client.delete` on the child), which is always refused."""

from frappe.model.document import Document


class PartographObservation(Document):
	def validate(self):
		from onerc_healthcare.maternity.partograph import refuse_row_change

		refuse_row_change(self)

	def on_trash(self):
		from onerc_healthcare.maternity.partograph import refuse_row_change

		refuse_row_change(self)
