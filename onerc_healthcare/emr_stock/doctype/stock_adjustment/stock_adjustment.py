# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Stock Adjustment: counts and write-offs with Medical In-Charge approval. Lifecycle and posting:
`onerc_healthcare.stock.adjustment` (S1). Status changes only through its actions (append-only `log`)."""

from frappe.model.document import Document


class StockAdjustment(Document):
	def validate(self):
		# Builder stub: field rules and the status guard (privacy.lifecycle.guard_fields / guard_log).
		pass
