# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""PROVISIONAL medication catalogue (OI-9), seeded without stock, prices or billing (C-5, SDD §5.5).

Marley's own path is unusable here: `Medication.validate` needs a price list, and `insert_item` creates
*stock* Items and an Item Price. So each entry of `data/medication_catalogue.json` is seeded as:

1. an Item: non-stock, not sold or purchased, enabled, in the dedicated item group;
2. a Medication with no linked rows (Marley's `after_insert` / `on_update` then create nothing);
3. one `Medication Linked Item` row written directly with `db_insert`: `is_billable=1` (Marley's hidden
   internal flag, which keeps the Item enabled when an admin later saves the Medication), `change_in_item=0`
   (so that save never rewrites the Item or creates an Item Price) and the Medication's price list set to
   the disabled selling list (D18), so a later save passes `Medication.validate`.

Idempotent: an existing Medication is never touched, an existing Item is reused as is, and nothing is
committed here.
"""

from __future__ import annotations

import json
import os

import frappe

from onerc_healthcare.constants import DISPENSING_DOCTYPE

DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "medication_catalogue.json")
LINKED_ITEM_DOCTYPE = "Medication Linked Item"


def load_catalogue() -> dict:
	with open(DATA_FILE, encoding="utf-8") as f:
		return json.load(f)


def seed_catalogue(data: dict | None = None) -> list[str]:
	"""Create the missing catalogue Medications (and their Items). Returns the Medications created."""
	data = data or load_catalogue()
	_ensure_masters(data)
	created = []
	for row in data["medications"]:
		if frappe.db.exists("Medication", row["name"]):
			continue
		item = _ensure_item(data, row)
		_insert_medication(data, row, item)
		created.append(row["name"])
	return created


def _ensure_masters(data: dict) -> None:
	if not frappe.db.exists("Item Group", data["item_group"]):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": data["item_group"],
				"parent_item_group": _root_item_group(),
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
	for uom in data["uoms"]:
		if not frappe.db.exists("UOM", uom):
			frappe.get_doc({"doctype": "UOM", "uom_name": uom}).insert(ignore_permissions=True)
	for form in data["dosage_forms"]:
		if not frappe.db.exists("Dosage Form", form):
			frappe.get_doc({"doctype": "Dosage Form", "dosage_form": form}).insert(ignore_permissions=True)
	for row in data["medications"]:
		cls = row["medication_class"]
		if not frappe.db.exists("Medication Class", cls):
			frappe.get_doc({"doctype": "Medication Class", "medication_class": cls}).insert(
				ignore_permissions=True
			)


def _root_item_group() -> str:
	if frappe.db.exists("Item Group", "All Item Groups"):
		return "All Item Groups"
	return frappe.db.get_value("Item Group", {"is_group": 1, "parent_item_group": ""})


def _ensure_item(data: dict, row: dict) -> str:
	code = row["name"]
	if frappe.db.exists("Item", code):
		return code
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": data["item_group"],
			"stock_uom": data["stock_uom"],
			"description": f"{code}. {data['provisional_flag']}",
			"is_stock_item": 0,
			"include_item_in_manufacturing": 0,
			"is_sales_item": 0,
			"is_purchase_item": 0,
			"is_fixed_asset": 0,
			"disabled": 0,
		}
	).insert(ignore_permissions=True)
	return code


def _insert_medication(data: dict, row: dict, item: str) -> None:
	med = frappe.get_doc(
		{
			"doctype": "Medication",
			"generic_name": row["generic_name"],
			"medication_class": row["medication_class"],
			"strength": row["strength"],
			"strength_uom": row["strength_uom"],
			"dosage_form": row["dosage_form"],
			"default_prescription_dosage": _if_exists("Prescription Dosage", row.get("dosage")),
			"default_prescription_duration": _if_exists("Prescription Duration", row.get("duration")),
			"is_orderable": 1,
			"disabled": 0,
		}
	)
	med.insert(ignore_permissions=True, set_name=row["name"])
	price_list = data.get("price_list")
	if price_list and frappe.db.exists("Price List", price_list):
		frappe.db.set_value("Medication", med.name, "price_list", price_list, update_modified=False)
	frappe.get_doc(
		{
			"doctype": LINKED_ITEM_DOCTYPE,
			"parent": med.name,
			"parenttype": "Medication",
			"parentfield": "linked_items",
			"idx": 1,
			"item_code": item,
			"item": item,
			"item_group": data["item_group"],
			"stock_uom": data["stock_uom"],
			"description": data["provisional_flag"],
			"is_billable": 1,
			"change_in_item": 0,
			"rate": 0,
		}
	).db_insert()


def _if_exists(doctype: str, name: str | None) -> str | None:
	return name if name and frappe.db.exists(doctype, name) else None


def medication_for_item(item: str) -> str | None:
	"""The Medication an Item is linked to (via Medication Linked Item)."""
	return frappe.db.get_value(
		LINKED_ITEM_DOCTYPE, {"item": item, "parenttype": "Medication"}, "parent", order_by="idx asc"
	)


def items_for_medication(medication: str) -> list[str]:
	"""Enabled Items linked to `medication`, in row order."""
	rows = frappe.get_all(
		LINKED_ITEM_DOCTYPE,
		filters={"parent": medication, "parenttype": "Medication"},
		fields=["item"],
		order_by="idx asc",
	)
	items = [r.item for r in rows if r.item]
	if not items:
		return []
	enabled = set(frappe.get_all("Item", filters={"name": ["in", items], "disabled": 0}, pluck="name"))
	return [i for i in items if i in enabled]


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def dispensable_item_query(
	doctype: str, txt: str, searchfield: str, start: int, page_len: int, filters: dict | None
) -> list:
	"""Link search for the dispensed Item: enabled Items linked to a Medication, no stock columns.

	Restricted to a Medication when `filters.medication` is given. Readers of Dispensing Record only.
	"""
	frappe.has_permission(DISPENSING_DOCTYPE, "read", throw=True)
	filters = filters or {}
	linked = frappe.qb.DocType(LINKED_ITEM_DOCTYPE)
	item = frappe.qb.DocType("Item")
	query = (
		frappe.qb.from_(linked)
		.inner_join(item)
		.on(item.name == linked.item)
		.select(linked.item, linked.parent)
		.where(item.disabled == 0)
		.where(linked.parenttype == "Medication")
		.orderby(linked.item)
		.limit(page_len)
		.offset(start)
	)
	if filters.get("medication"):
		query = query.where(linked.parent == filters["medication"])
	if txt:
		query = query.where(linked.item.like(f"%{txt}%") | linked.parent.like(f"%{txt}%"))
	return query.run()
