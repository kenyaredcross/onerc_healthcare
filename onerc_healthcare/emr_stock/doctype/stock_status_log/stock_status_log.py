# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Append-only status log row shared by Stock Receipt, Stock Requisition and Stock Adjustment (table field `log`,
fields = privacy.lifecycle.LOG_FIELDS). Written only by the lifecycle modules in `onerc_healthcare.stock`."""

from frappe.model.document import Document


class StockStatusLog(Document):
	pass
