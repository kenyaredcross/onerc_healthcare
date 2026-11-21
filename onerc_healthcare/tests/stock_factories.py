# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 6S test factories (W0). Builders add helpers only at the end of the file, and say so. Never commit here.

The canonical way to put stock in a test is `stock_store` (a Material Receipt at rate 0 with a batch), never a
Stock Receipt: S1's lifecycle is not needed to test S2 / S3.

    from onerc_healthcare.tests.stock_factories import stock_store, store

    pharmacy = store(0, STORE_TYPE_PHARMACY)            # "Pharmacy - NTK - KLB"
    batch = stock_store(PARA, 20, pharmacy, expiry=add_days(today(), 30))
"""

from __future__ import annotations

import math

import frappe
from frappe.utils import add_days, cint, flt, getdate, random_string, today

from onerc_healthcare.constants import (
	ITEM_GROUP_SUPPLIES,
	ROLE_CLINICIAN,
	ROLE_HEAD_OPS,
	ROLE_HIS,
	ROLE_LAB,
	ROLE_MIC,
	ROLE_MIDWIFE,
	ROLE_PHARMACIST,
	ROLE_RECEPTIONIST,
	ROLE_STORE_KEEPER,
	ROLE_TRIAGE,
	STOCK_REQUISITION_DOCTYPE,
	STORE_TYPE_OUTREACH,
	STORE_TYPE_PHARMACY,
)
from onerc_healthcare.stock import balances, postings
from onerc_healthcare.stock.batches import ensure_batch
from onerc_healthcare.stock.setup import stock_item_values
from onerc_healthcare.stock.stores import store_for
from onerc_healthcare.tests.factories import get_facility, make_user

STOCK_USER_ROLES = {
	"storekeeper": (ROLE_STORE_KEEPER,),
	"pharmacist": (ROLE_PHARMACIST,),
	"lab": (ROLE_LAB,),
	"midwife": (ROLE_MIDWIFE,),
	"triage": (ROLE_TRIAGE,),
	"clinician": (ROLE_CLINICIAN,),
	"mic": (ROLE_MIC,),
	"his": (ROLE_HIS,),
	"headops": (ROLE_HEAD_OPS,),
	"reception": (ROLE_RECEPTIONIST,),  # no stock rights at all
}


def stock_users() -> dict[str, str]:
	"""{key: user} for every stock actor (`t-stk-<key>@test.krcs`), plus a Receptionist (no stock rights)."""
	return {key: make_user(f"stk-{key}", roles) for key, roles in STOCK_USER_ROLES.items()}


def store(facility_index: int = 0, store_type: str = STORE_TYPE_PHARMACY) -> str:
	"""The store of the site's `facility_index`-th facility (`store_type`: a STORE_TYPE_* or a point of use name)."""
	return store_for(get_facility(facility_index), store_type)


def stock_item(name: str | None = None, group: str = ITEM_GROUP_SUPPLIES, uom: str = "Nos") -> str:
	"""A batch stock Item "T-<name>" in `group` (created with the group if missing). Returns the item code."""
	code = f"T-{name or random_string(8)}"
	if frappe.db.exists("Item", code):
		return code
	if not frappe.db.exists("Item Group", group):
		frappe.get_doc(
			{"doctype": "Item Group", "item_group_name": group, "parent_item_group": "All Item Groups"}
		).insert(ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": group,
			"stock_uom": uom,
			"is_sales_item": 0,
			"is_purchase_item": 0,
			"include_item_in_manufacturing": 0,
			**stock_item_values(),
		}
	).insert(ignore_permissions=True)
	return code


def stock_store(item: str, qty: float, store: str, expiry=None, batch_no: str | None = None) -> str:
	"""Put `qty` of `item` into `store`: a Material Receipt at rate 0 of one batch (`batch_no`, default a new
	"T-..." number; `expiry`, default a year from today). Returns the Batch name. No GL is posted (perpetual
	inventory off).

	A past `expiry` makes an expired batch in stock: ERPNext refuses to receive an expired batch
	(BatchExpiredError), so the batch is received unexpired and its expiry date is then moved back, as if time
	had passed."""
	expiry = getdate(expiry or add_days(today(), 365))
	expired = expiry < getdate(today())
	batch = ensure_batch(
		item, batch_no or f"T-{random_string(8)}", add_days(today(), 1) if expired else expiry
	)
	postings.post_stock_entry(
		postings.MATERIAL_RECEIPT,
		[{"item_code": item, "qty": qty, "t_warehouse": store, "batch_no": batch}],
		remarks="T- test stock",
	)
	if expired:
		frappe.db.set_value(
			"Batch", batch, {"expiry_date": expiry, "manufacturing_date": add_days(expiry, -1)}
		)
	return batch


def is_batch_stock_item(item: str | None) -> bool:
	if not item:
		return False
	values = frappe.db.get_value("Item", item, ["is_stock_item", "has_batch_no"], as_dict=True)
	return bool(values and cint(values.is_stock_item) and cint(values.has_batch_no))


def top_up(item: str | None, qty, store: str) -> str | None:
	"""Make sure at least `qty` unexpired units of a batch stock `item` are in `store` (adds the shortfall plus a
	margin as a new batch). Non-stock Items and empty quantities are ignored. Returns the new Batch, if any."""
	if not is_batch_stock_item(item) or flt(qty) <= 0:
		return None
	short = flt(qty) - balances.available_qty(item, store)
	if short <= 0:
		return None
	return stock_store(item, math.ceil(short) + 50, store)


def stock_for_dispensing(
	medication_request: str, qty, item: str | None = None, store_type: str = STORE_TYPE_PHARMACY
) -> str | None:
	"""Top up the store a Dispensing Record of `medication_request` will deduct from (the visit facility's Pharmacy,
	or `store_type`) with `qty` of `item` (default the prescribed Item). Used by the existing dispense helpers so
	they keep working once S3 blocks short dispensing."""
	from onerc_healthcare.visit.tasks import visit_for_encounter

	mr = frappe.db.get_value(
		"Medication Request",
		medication_request,
		["order_group", "medication_item", "medication"],
		as_dict=True,
	)
	if not mr:
		return None
	item = item or mr.medication_item
	if not item and mr.medication:
		from onerc_healthcare.pharmacy.catalogue import items_for_medication

		item = next(iter(items_for_medication(mr.medication)), None)
	visit = visit_for_encounter(mr.order_group) if mr.order_group else None
	facility = frappe.db.get_value("Patient Visit", visit, "facility") if visit else None
	if not facility:
		return None
	return top_up(item, qty, store_for(facility, store_type))


def dispensed_item(values: dict) -> str | None:
	"""The Item a Dispensing Record built from `values` dispenses when it is not the prescribed one
	(`dispensed_item`, or the first Item of `dispensed_medication`); None for the prescribed Item."""
	if values.get("dispensed_item"):
		return values["dispensed_item"]
	if values.get("dispensed_medication"):
		from onerc_healthcare.pharmacy.catalogue import items_for_medication

		return next(iter(items_for_medication(values["dispensed_medication"])), None)
	return None


def stock_outreach(facility: str, medication: str, qty) -> str | None:
	"""Top up `facility`'s Outreach store with `qty` of `medication`'s first Item (field dispensing)."""
	from onerc_healthcare.pharmacy.catalogue import items_for_medication

	item = next(iter(items_for_medication(medication)), None)
	return top_up(item, qty, store_for(facility, STORE_TYPE_OUTREACH))


def make_s11(
	items=(),
	point_of_use: str = STORE_TYPE_PHARMACY,
	facility_index: int = 0,
	user: str | None = None,
	**values,
):
	"""A Draft Stock Requisition (S11) with `items` = ((item, qty_requested), ...), inserted as `user` (default
	Administrator) with `flags.from_factory` (S2's controller may accept a factory insert, as maternity does).
	S2 owns the lifecycle; move it on with S2's actions."""
	doc = frappe.get_doc(
		{
			"doctype": STOCK_REQUISITION_DOCTYPE,
			"facility": get_facility(facility_index),
			"point_of_use": point_of_use,
			"items": [{"item": item, "qty_requested": qty} for item, qty in items],
			**values,
		}
	)
	doc.flags.from_factory = True
	previous = frappe.session.user
	if user:
		frappe.set_user(user)
	try:
		doc.insert(ignore_permissions=True)
	finally:
		frappe.set_user(previous)
	return doc
