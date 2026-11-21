# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Stock Requisition (S11). Lifecycle and postings: `onerc_healthcare.stock.requisition` (S2). Status changes only
through its actions (append-only `log`)."""

from frappe.model.document import Document


class StockRequisition(Document):
	def validate(self):
		# Builder stub: field rules and the status guard (privacy.lifecycle.guard_fields / guard_log).
		pass
