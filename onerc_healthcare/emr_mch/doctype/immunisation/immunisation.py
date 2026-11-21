# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Immunisation. Rules live in `onerc_healthcare.mch.immunisation` (see docs/MCH_CONTRACT.md)."""

from frappe.model.document import Document

from onerc_healthcare.mch import immunisation


class Immunisation(Document):
	def validate(self):
		immunisation.immunisation_validate(self)

	def before_submit(self):
		immunisation.immunisation_before_submit(self)

	def before_cancel(self):
		immunisation.immunisation_before_cancel(self)

	def before_update_after_submit(self):
		immunisation.immunisation_before_update_after_submit(self)
