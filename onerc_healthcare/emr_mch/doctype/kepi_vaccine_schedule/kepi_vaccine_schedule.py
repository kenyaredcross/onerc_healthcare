# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""KEPI Vaccine Schedule. Rules live in `onerc_healthcare.mch.immunisation` (see docs/MCH_CONTRACT.md)."""

from frappe.model.document import Document

from onerc_healthcare.mch import immunisation


class KEPIVaccineSchedule(Document):
	def validate(self):
		immunisation.schedule_row_validate(self)
