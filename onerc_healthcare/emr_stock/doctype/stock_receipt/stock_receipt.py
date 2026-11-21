# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Stock Receipt (GRN): goods received into the Main Store. Lifecycle and posting: `onerc_healthcare.stock.receipt`
(S1). Status changes only through its actions (append-only `log`)."""

from frappe.model.document import Document


class StockReceipt(Document):
	def validate(self):
		# Builder stub: field rules and the status guard (privacy.lifecycle.guard_fields / guard_log).
		pass
