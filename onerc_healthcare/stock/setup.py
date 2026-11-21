# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 6S install (W0): quantity-only stock on ERPNext. Runs on every migrate; idempotent; never commits.

In order:
1. Stock Settings (`constants.ERPNEXT_STOCK_SETTINGS`): batches on, FEFO picking, no negative stock, no auto-indent.
   Enforced every run: the stock design depends on them.
2. Perpetual inventory OFF on the operation's company, so no GL is ever posted (C-5). ERPNext allows it only while
   the company has no uncancelled Stock Ledger Entry; if one exists while it is still on, migrate stops with an
   error instead of posting accounting entries.
3. A Fiscal Year covering today and a year from today (Stock Entries need one; nothing else is accounted).
4. Custom fields: Warehouse `krcs_facility` / `krcs_store_type` / `krcs_point_of_use`; Stock Entry and Stock
   Reconciliation `krcs_reference_doctype` / `krcs_reference_name` (the EMR document that posted it).
5. The stores of every facility (`constants.FACILITY_STORES`), exists-or-insert, never edited.
6. Every Item of `STOCK_ITEM_GROUPS` becomes a batch stock item with expiry (`STOCK_ITEM_VALUES`), unless it already
   has stock transactions (then it is left as it is and reported).
7. EMR Stock Settings defaults; the supplies / reagents catalogues (`stock.catalogue`, S1).

ERPNext's own warehouses ("Stores - KLB", "Work In Progress - KLB", ...) are left alone. No
`Company.default_warehouse` is set (Marley's encounter stock query stays unused).
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import add_days, add_years, cint, getdate, today

from onerc_healthcare.constants import (
	ERPNEXT_STOCK_SETTINGS,
	FACILITY_STORES,
	POINTS_OF_USE,
	STOCK_DEFAULTS,
	STOCK_ITEM_GROUPS,
	STOCK_ITEM_SHELF_LIFE_DAYS,
	STOCK_ITEM_VALUES,
	STOCK_REFERENCE_DOCTYPE_FIELD,
	STOCK_REFERENCE_NAME_FIELD,
	STOCK_SETTINGS_DOCTYPE,
	STORE_TYPE_POINT_OF_USE,
	STORE_TYPES,
	WAREHOUSE_FACILITY_FIELD,
	WAREHOUSE_POINT_OF_USE_FIELD,
	WAREHOUSE_STORE_TYPE_FIELD,
)
from onerc_healthcare.setup.operations import facility_unit_name, get_operation, get_operation_config
from onerc_healthcare.stock.stores import group_name, store_name

# D10: the KRCS fiscal year runs July to June.
FISCAL_YEAR_START = (7, 1)


class PerpetualInventoryError(frappe.ValidationError):
	"""Perpetual inventory is on and stock transactions already exist: it can no longer be switched off."""


def _reference_fields(insert_after: str) -> list[dict]:
	return [
		{
			"fieldname": STOCK_REFERENCE_DOCTYPE_FIELD,
			"label": "EMR Document Type",
			"fieldtype": "Link",
			"options": "DocType",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": insert_after,
		},
		{
			"fieldname": STOCK_REFERENCE_NAME_FIELD,
			"label": "EMR Document",
			"fieldtype": "Dynamic Link",
			"options": STOCK_REFERENCE_DOCTYPE_FIELD,
			"read_only": 1,
			"no_copy": 1,
			"search_index": 1,
			"insert_after": STOCK_REFERENCE_DOCTYPE_FIELD,
		},
	]


CUSTOM_FIELDS = {
	"Warehouse": [
		{
			"fieldname": WAREHOUSE_FACILITY_FIELD,
			"label": "Facility (KRCS)",
			"fieldtype": "Link",
			"options": "Healthcare Service Unit",
			"in_standard_filter": 1,
			"search_index": 1,
			"insert_after": "company",
			"description": "The facility group this store belongs to (EMR stores only).",
		},
		{
			"fieldname": WAREHOUSE_STORE_TYPE_FIELD,
			"label": "Store Type (KRCS)",
			"fieldtype": "Select",
			"options": "\n" + "\n".join(STORE_TYPES),
			"in_standard_filter": 1,
			"insert_after": WAREHOUSE_FACILITY_FIELD,
		},
		{
			"fieldname": WAREHOUSE_POINT_OF_USE_FIELD,
			"label": "Point of Use (KRCS)",
			"fieldtype": "Select",
			"options": "\n" + "\n".join(POINTS_OF_USE),
			"depends_on": f'eval:doc.{WAREHOUSE_STORE_TYPE_FIELD}=="{STORE_TYPE_POINT_OF_USE}"',
			"insert_after": WAREHOUSE_STORE_TYPE_FIELD,
		},
	],
	"Stock Entry": _reference_fields("stock_entry_type"),
	"Stock Reconciliation": _reference_fields("purpose"),
}


def install() -> None:
	apply_stock_settings()
	op = get_operation()
	cfg = get_operation_config(op) if op else None
	if cfg and frappe.db.exists("Company", cfg["company_name"]):
		disable_perpetual_inventory(cfg["company_name"])
		ensure_fiscal_years()
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	if cfg and frappe.db.exists("Company", cfg["company_name"]):
		seed_stores(cfg)
	ensure_stock_items()
	apply_settings_defaults()
	from onerc_healthcare.stock import catalogue

	catalogue.seed_catalogues()


# ---------------------------------------------------------------------------------------------- settings


def apply_stock_settings() -> list[str]:
	"""Enforce `ERPNEXT_STOCK_SETTINGS`. Saved through the document (its validate sets the matching defaults).
	Returns the fields changed."""
	settings = frappe.get_single("Stock Settings")
	changed = [k for k, v in ERPNEXT_STOCK_SETTINGS.items() if str(settings.get(k) or 0) != str(v)]
	if changed:
		settings.update({k: ERPNEXT_STOCK_SETTINGS[k] for k in changed})
		settings.save(ignore_permissions=True)
	return changed


def disable_perpetual_inventory(company: str) -> bool:
	"""Switch perpetual inventory off (no GL for stock, C-5). True if it was changed.

	Written directly: saving the Company runs its whole setup (accounts, warehouses). ERPNext's own rule (company.py
	`validate_perpetual_inventory`) is applied here: refuse while an uncancelled Stock Ledger Entry exists.
	"""
	if not cint(frappe.db.get_value("Company", company, "enable_perpetual_inventory")):
		return False
	if frappe.db.get_value("Stock Ledger Entry", {"is_cancelled": 0, "company": company}, "name"):
		frappe.throw(
			f"Company {company!r} has perpetual inventory ON and stock transactions already exist. The EMR keeps "
			"stock by quantity only (no GL, C-5), and ERPNext cannot switch perpetual inventory off once stock "
			"has been posted. Cancel those stock transactions (or restore the site from before them), then run "
			"`bench migrate` again.",
			PerpetualInventoryError,
		)
	frappe.db.set_value("Company", company, "enable_perpetual_inventory", 0)
	frappe.clear_document_cache("Company", company)
	if hasattr(frappe.local, "enable_perpetual_inventory"):
		frappe.local.enable_perpetual_inventory.pop(company, None)
	return True


def ensure_fiscal_years(on: str | None = None) -> list[str]:
	"""Create the July-June Fiscal Year covering `on` (default today) and the one covering a year later, if missing.
	Returns the years created."""
	day = getdate(on or today())
	created = []
	for date in (day, getdate(add_years(day, 1))):
		if frappe.db.get_value(
			"Fiscal Year",
			{"disabled": 0, "year_start_date": ("<=", date), "year_end_date": (">=", date)},
			"name",
		):
			continue
		month, dom = FISCAL_YEAR_START
		start = date.replace(month=month, day=dom)
		if start > date:
			start = start.replace(year=start.year - 1)
		end = getdate(add_days(add_years(start, 1), -1))
		year = f"{start.year}-{end.year}"
		if frappe.db.exists("Fiscal Year", year):
			continue  # disabled, or a different span under this name: an admin decision, leave it
		frappe.get_doc(
			{"doctype": "Fiscal Year", "year": year, "year_start_date": start, "year_end_date": end}
		).insert(ignore_permissions=True)
		created.append(year)
	return created


def apply_settings_defaults() -> None:
	"""EMR Stock Settings: fill empty values from STOCK_DEFAULTS (an admin's value is kept)."""
	if not frappe.db.exists("DocType", STOCK_SETTINGS_DOCTYPE):
		return
	for field, value in STOCK_DEFAULTS.items():
		if not frappe.db.get_single_value(STOCK_SETTINGS_DOCTYPE, field):
			frappe.db.set_single_value(STOCK_SETTINGS_DOCTYPE, field, value)


# ---------------------------------------------------------------------------------------------- stores


def seed_stores(cfg: dict) -> list[str]:
	"""The store group and stores of every facility of `cfg` (an operations.json entry) that exists.

	Exists-or-insert: an existing store is never edited, except that its empty KRCS fields are filled in.
	Returns the Warehouses created."""
	abbr, company = cfg["abbr"], cfg["company_name"]
	root = f"All Warehouses - {abbr}"
	root = root if frappe.db.exists("Warehouse", root) else None
	created = []
	for facility in cfg["facilities"]:
		hsu = facility_unit_name(facility, abbr)
		if not frappe.db.exists("Healthcare Service Unit", hsu):
			continue
		group = group_name(facility, abbr)
		if not frappe.db.exists("Warehouse", group):
			_insert_warehouse(
				f"{facility['short']} Stores",
				company,
				root,
				is_group=1,
				values={WAREHOUSE_FACILITY_FIELD: hsu},
			)
			created.append(group)
		for label, store_type, point_of_use in FACILITY_STORES:
			values = {
				WAREHOUSE_FACILITY_FIELD: hsu,
				WAREHOUSE_STORE_TYPE_FIELD: store_type,
				WAREHOUSE_POINT_OF_USE_FIELD: point_of_use,
			}
			name = store_name(facility, label, abbr)
			if frappe.db.exists("Warehouse", name):
				_fill_empty(name, values)
				continue
			_insert_warehouse(f"{label} - {facility['code']}", company, group, is_group=0, values=values)
			created.append(name)
	return created


def _insert_warehouse(
	warehouse_name: str, company: str, parent: str | None, is_group: int, values: dict
) -> None:
	frappe.get_doc(
		{
			"doctype": "Warehouse",
			"warehouse_name": warehouse_name,
			"company": company,
			"parent_warehouse": parent,
			"is_group": is_group,
			**{k: v for k, v in values.items() if v},
		}
	).insert(ignore_permissions=True)


def _fill_empty(name: str, values: dict) -> None:
	current = frappe.db.get_value("Warehouse", name, list(values), as_dict=True)
	missing = {k: v for k, v in values.items() if v and not current.get(k)}
	if missing:
		frappe.db.set_value("Warehouse", name, missing, update_modified=False)


# ---------------------------------------------------------------------------------------------- items


def stock_item_values(shelf_life_in_days: int | None = None) -> dict:
	"""Item fields of a KRCS stock item: batch, expiry, no automatic batches, a shelf life (mandatory with expiry)."""
	return {**STOCK_ITEM_VALUES, "shelf_life_in_days": cint(shelf_life_in_days) or STOCK_ITEM_SHELF_LIFE_DAYS}


def ensure_stock_items() -> dict[str, list[str]]:
	"""Make every Item of `STOCK_ITEM_GROUPS` a batch stock item with expiry. An Item that already has stock
	transactions but is not set up that way is left alone and reported (ERPNext forbids the change).

	Returns {"converted": [...], "skipped": [...]}."""
	fields = ["name", "shelf_life_in_days", *STOCK_ITEM_VALUES]
	items = frappe.get_all("Item", filters={"item_group": ("in", list(STOCK_ITEM_GROUPS))}, fields=fields)
	converted, skipped = [], []
	for item in items:
		if (
			all(cint(item.get(k)) == v for k, v in STOCK_ITEM_VALUES.items())
			and cint(item.shelf_life_in_days) > 0
		):
			continue
		if frappe.db.exists("Stock Ledger Entry", {"item_code": item.name, "is_cancelled": 0}):
			skipped.append(item.name)
			continue
		doc = frappe.get_doc("Item", item.name)
		doc.update(stock_item_values(item.shelf_life_in_days))
		doc.save(ignore_permissions=True)
		converted.append(item.name)
	if skipped:
		msg = (
			"onerc_healthcare stock: these Items have stock transactions but are not batch stock items with "
			f"expiry, so they were left unchanged: {', '.join(skipped)}"
		)
		print(msg)
		frappe.logger("onerc_healthcare").warning(msg)
	return {"converted": converted, "skipped": skipped}
