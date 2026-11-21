# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""ANC Pregnancy. Rules live in `onerc_healthcare.mch.anc` (see docs/MCH_CONTRACT.md)."""

from frappe.model.document import Document

from onerc_healthcare.mch import anc


class ANCPregnancy(Document):
	def onload(self):
		anc.pregnancy_onload(self)

	def validate(self):
		anc.pregnancy_validate(self)

	def on_update(self):
		anc.pregnancy_on_update(self)
