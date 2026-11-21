# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Batch records (W0 helper; S1 owns the file afterwards and keeps these signatures).

A Batch is identified by (Item, batch number as printed). ERPNext names a Batch by its `batch_id`, unique across all
Items, so two Items can not both have a Batch named by the same printed number:
- the Batch is named by the printed number when that name is free or already that Item's;
- otherwise "<printed number> (<item code>)".
One printed number of one Item has one expiry date; a different expiry is refused (BatchExpiryMismatchError).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, getdate, today


class BatchExpiryMismatchError(frappe.ValidationError):
	"""The batch already exists with another expiry date."""


def batch_name(item: str, batch_no: str) -> str:
	"""The Batch name (Item, printed number) has or would get. Never creates anything."""
	batch_no = (batch_no or "").strip()
	if not batch_no:
		frappe.throw(_("Enter the batch number."))
	owner = frappe.db.get_value("Batch", batch_no, "item")
	if not owner or owner == item:
		return batch_no
	return f"{batch_no} ({item})"


def find_batch(item: str, batch_no: str) -> str | None:
	"""The existing Batch of `item` with printed number `batch_no`, or None."""
	name = batch_name(item, batch_no)
	return name if frappe.db.get_value("Batch", name, "item") == item else None


def ensure_batch(
	item: str,
	batch_no: str,
	expiry_date,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
) -> str:
	"""The Batch of (`item`, `batch_no`), created with `expiry_date` if missing. Returns its name.

	The manufacturing date is unknown: it is set to today, or the day before the expiry for an already expired batch
	(ERPNext requires manufacturing <= expiry).
	"""
	if not expiry_date:
		frappe.throw(_("Enter the expiry date of batch {0}.").format(frappe.bold(batch_no)))
	expiry = getdate(expiry_date)
	name = batch_name(item, batch_no)
	existing = frappe.db.get_value("Batch", name, ["item", "expiry_date"], as_dict=True)
	if existing:
		if existing.expiry_date and getdate(existing.expiry_date) != expiry:
			frappe.throw(
				_("Batch {0} of {1} is already recorded with expiry date {2}, not {3}.").format(
					frappe.bold(batch_no),
					frappe.bold(item),
					frappe.format(existing.expiry_date, "Date"),
					frappe.format(expiry, "Date"),
				),
				BatchExpiryMismatchError,
				title=_("Expiry Date Differs"),
			)
		return name
	manufactured = min(getdate(today()), getdate(add_days(expiry, -1)))
	frappe.get_doc(
		{
			"doctype": "Batch",
			"batch_id": name,
			"item": item,
			"expiry_date": expiry,
			"manufacturing_date": manufactured,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"description": _("Batch number as printed: {0}").format(batch_no.strip()),
		}
	).insert(ignore_permissions=True)
	return name
