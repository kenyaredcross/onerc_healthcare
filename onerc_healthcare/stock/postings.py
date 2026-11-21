# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Quantity-only Stock Entries (W0; integrator-owned, builders call these and never build Stock Entries themselves).

Every row has rate 0 and `allow_zero_valuation_rate` (a blank rate would pull an Item Price, C-5). Perpetual
inventory is off (stock.setup), so ERPNext posts no GL. Rows:

    {"item_code": str, "qty": float, "s_warehouse": str | None, "t_warehouse": str | None,
     "batch_no": str | None}

- Inward rows (no `s_warehouse`) need `batch_no`: an existing Batch (create it first with stock.batches).
- Outward rows without `batch_no` are picked by ERPNext at submit, earliest expiry first (FEFO), skipping expired
  batches; with `batch_no` that batch is used. Short stock raises ERPNext's insufficient-stock error
  (`erpnext.stock.stock_ledger.NegativeStockError`); callers pre-check with stock.balances for a friendly message.
- The Stock Entry carries the EMR document in `krcs_reference_doctype` / `krcs_reference_name` (Stock Card).

Inserted and submitted with ignore_permissions: no EMR role has create/submit on Stock Entry; the caller checks its
own role and lifecycle rules first. Lock order: the caller's locks (Visit -> Medication Request, or the EMR stock
document) come first; ERPNext then locks the Bins.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt

from onerc_healthcare.constants import STOCK_REFERENCE_DOCTYPE_FIELD, STOCK_REFERENCE_NAME_FIELD
from onerc_healthcare.setup.operations import get_company

MATERIAL_RECEIPT = "Material Receipt"
MATERIAL_ISSUE = "Material Issue"
MATERIAL_TRANSFER = "Material Transfer"
PURPOSES = (MATERIAL_RECEIPT, MATERIAL_ISSUE, MATERIAL_TRANSFER)


def make_stock_entry(
	purpose: str,
	items: list[dict],
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	posting_date=None,
	posting_time=None,
	remarks: str | None = None,
	company: str | None = None,
):
	"""Build (not insert) a quantity-only Stock Entry. See the module docstring for the row format."""
	if purpose not in PURPOSES:
		frappe.throw(f"Unsupported stock entry purpose {purpose!r}")
	if not items:
		frappe.throw("A stock entry needs at least one row.")
	se = frappe.new_doc("Stock Entry")
	se.update(
		{
			"stock_entry_type": purpose,
			"purpose": purpose,
			"company": company or get_company(),
			"remarks": remarks,
			STOCK_REFERENCE_DOCTYPE_FIELD: reference_doctype,
			STOCK_REFERENCE_NAME_FIELD: reference_name,
		}
	)
	if posting_date:
		se.set_posting_time = 1
		se.posting_date = posting_date
		if posting_time:
			se.posting_time = posting_time
	for row in items:
		qty = flt(row.get("qty"))
		if qty <= 0:
			frappe.throw(f"Stock entry row for {row.get('item_code')!r} needs a quantity above 0.")
		batch = row.get("batch_no")
		se.append(
			"items",
			{
				"item_code": row["item_code"],
				"qty": qty,
				"transfer_qty": qty,
				"conversion_factor": 1,
				"s_warehouse": row.get("s_warehouse"),
				"t_warehouse": row.get("t_warehouse"),
				"batch_no": batch,
				"use_serial_batch_fields": 1 if batch else 0,
				"basic_rate": 0,
				"valuation_rate": 0,
				"allow_zero_valuation_rate": 1,
				"set_basic_rate_manually": 1,
			},
		)
	return se


def post_stock_entry(purpose: str, items: list[dict], **kw) -> str:
	"""Insert and submit a quantity-only Stock Entry (`make_stock_entry` arguments). Returns its name."""
	se = make_stock_entry(purpose, items, **kw)
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	return se.name


def cancel_stock_entry(name: str) -> None:
	"""Cancel a Stock Entry posted by the EMR (reversal). ERPNext refuses when the stock has moved on (the
	cancellation would make a balance negative)."""
	se = frappe.get_doc("Stock Entry", name)
	if se.docstatus != 1:
		return
	se.flags.ignore_permissions = True
	se.cancel()
