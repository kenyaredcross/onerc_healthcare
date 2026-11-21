# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Stock balances (W0 read helpers; S1 may add more at the end of the file, signatures stay).

`available_batches` uses ERPNext's own FEFO picker (`get_auto_batch_nos`, picking based on Expiry), so the
pre-checks of dispensing (S3) and S11 issue (S2) see exactly what the submit will pick: expired and disabled
batches are excluded (as of today), earliest expiry first.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt


def available_batches(item: str, warehouse: str) -> list[frappe._dict]:
	"""Unexpired batches of `item` in `warehouse` with stock: [{batch_no, qty, expiry_date}], earliest expiry first."""
	from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import get_auto_batch_nos

	rows = get_auto_batch_nos(
		frappe._dict(
			item_code=item,
			warehouse=warehouse,
			based_on="Expiry",
			ignore_reserved_stock=True,
			for_stock_levels=False,
		)
	)
	expiry = dict(
		frappe.get_all(
			"Batch",
			filters={"name": ("in", [r.batch_no for r in rows] or [""])},
			fields=["name", "expiry_date"],
			as_list=True,
		)
	)
	return [
		frappe._dict(batch_no=r.batch_no, qty=flt(r.qty), expiry_date=expiry.get(r.batch_no))
		for r in rows
		if flt(r.qty) > 0
	]


def available_qty(item: str, warehouse: str) -> float:
	"""Quantity of `item` in `warehouse` that can be issued today (unexpired batches only)."""
	return sum(r.qty for r in available_batches(item, warehouse))


def actual_qty(item: str, warehouse: str) -> float:
	"""Book balance of `item` in `warehouse`, expired batches included (ERPNext Bin)."""
	return flt(frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty"))


def batch_qty(batch: str, warehouse: str) -> float:
	"""Balance of one batch in `warehouse` (expired or not)."""
	from erpnext.stock.doctype.batch.batch import get_batch_qty

	return flt(
		get_batch_qty(batch_no=batch, warehouse=warehouse, ignore_reserved_stock=True, for_stock_levels=True)
		or 0
	)
