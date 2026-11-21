# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""ANC Contact. Rules live in `onerc_healthcare.mch.anc` (see docs/MCH_CONTRACT.md)."""

from frappe.model.document import Document

from onerc_healthcare.mch import anc


class ANCContact(Document):
	def validate(self):
		anc.contact_validate(self)

	def before_submit(self):
		anc.contact_before_submit(self)

	def before_cancel(self):
		anc.contact_before_cancel(self)

	def before_update_after_submit(self):
		anc.contact_before_update_after_submit(self)
