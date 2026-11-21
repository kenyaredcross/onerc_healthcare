# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""PNC Visit. Rules live in `onerc_healthcare.mch.pnc` (see docs/MCH_CONTRACT.md)."""

from frappe.model.document import Document

from onerc_healthcare.mch import pnc


class PNCVisit(Document):
	def validate(self):
		pnc.visit_validate(self)

	def before_submit(self):
		pnc.visit_before_submit(self)

	def before_cancel(self):
		pnc.visit_before_cancel(self)

	def before_update_after_submit(self):
		pnc.visit_before_update_after_submit(self)
