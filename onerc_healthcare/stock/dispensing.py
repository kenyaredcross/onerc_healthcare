# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Dispensing deduction (facility Pharmacy, and the Outreach store for field dispensing). Owned by S3.

Hook points in `emr_pharmacy/doctype/dispensing_record/dispensing_record.py` (S3 edits them):
- `validate` after `lock_line` (lock order Visit -> Medication Request -> stock): for outcome Dispensed of a batch stock
  item, the unexpired balance (`stock.balances.available_qty`) of `dispensed_item` in the visit facility's Pharmacy
  (`stock.stores.store_for(visit.facility, STORE_TYPE_PHARMACY)`) must cover `quantity`, else `StockShortError`:
  "Only N in stock (Pharmacy - NTK - KLB). Dispense N or record Not Dispensed: out of stock."
- `on_submit`: a Material Issue (FEFO, `stock.postings.post_stock_entry`, reference the Dispensing Record); sets
  `warehouse`, `stock_entry` and `stock_status` (DISPENSING_STOCK_STATUSES; Not applicable for Not Dispensed or a
  non-stock Item).
- `on_cancel`: cancels that Stock Entry; records without one are skipped.
- Outreach sync (`outreach/apply.py` `_dispense`) never fails on stock: it deducts from the session facility's
  Outreach store and records STOCK_NOT_DEDUCTED_SHORT when short (listed on Stock Alerts).
- NOT_DISPENSED_OUT_OF_STOCK is offered as a Not Dispensed reason.
"""

import frappe


class StockShortError(frappe.ValidationError):
	"""Not enough unexpired stock in the store for the quantity dispensed."""
