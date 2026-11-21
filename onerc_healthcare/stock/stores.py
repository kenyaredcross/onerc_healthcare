# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""The stores of each facility (W0; integrator-owned, builders import only).

Each facility group (a Healthcare Service Unit) has a Warehouse group "<facility short> Stores - <abbr>" under
"All Warehouses - <abbr>", holding the leaves of `constants.FACILITY_STORES`, named "<label> - <code> - <abbr>"
(e.g. "Pharmacy - NTK - KLB"). Stores are found by their custom fields (`krcs_facility`, `krcs_store_type`,
`krcs_point_of_use`), never by name, so an admin rename does not break them.
"""

from __future__ import annotations

import frappe
from frappe import _

from onerc_healthcare.constants import (
	POINTS_OF_USE,
	STORE_GROUP_SUFFIX,
	STORE_TYPE_POINT_OF_USE,
	STORE_TYPES,
	WAREHOUSE_FACILITY_FIELD,
	WAREHOUSE_POINT_OF_USE_FIELD,
	WAREHOUSE_STORE_TYPE_FIELD,
)


class StoreNotFoundError(frappe.ValidationError):
	"""The facility has no store of the requested kind."""


def group_name(facility: dict, abbr: str) -> str:
	"""Warehouse name of a facility's store group, e.g. "Natukobenyo Stores - KLB" (`facility`: operations.json row)."""
	return f"{facility['short']} {STORE_GROUP_SUFFIX} - {abbr}"


def store_name(facility: dict, label: str, abbr: str) -> str:
	"""Warehouse name of one store of a facility, e.g. "Main Store - NTK - KLB"."""
	return f"{label} - {facility['code']} - {abbr}"


def _filters(facility: str, store_type: str) -> dict:
	if store_type in POINTS_OF_USE:
		return {
			WAREHOUSE_FACILITY_FIELD: facility,
			WAREHOUSE_STORE_TYPE_FIELD: STORE_TYPE_POINT_OF_USE,
			WAREHOUSE_POINT_OF_USE_FIELD: store_type,
		}
	if store_type == STORE_TYPE_POINT_OF_USE:
		frappe.throw(_("Name the point of use ({0}).").format(", ".join(POINTS_OF_USE)))
	if store_type not in STORE_TYPES:
		frappe.throw(_("Unknown store type {0}.").format(frappe.bold(store_type)))
	return {WAREHOUSE_FACILITY_FIELD: facility, WAREHOUSE_STORE_TYPE_FIELD: store_type}


def store_for(facility: str, store_type: str, required: bool = True) -> str | None:
	"""The Warehouse of `facility` (a facility group Healthcare Service Unit name) for `store_type`.

	`store_type` is a STORE_TYPE_* other than Point of Use (Main Store, Pharmacy, In Transit, Outreach) or a point
	of use name (Laboratory, Maternity Ward, Outpatient), so every S11 destination resolves directly. Disabled
	warehouses are ignored. Missing: StoreNotFoundError when `required`, else None.
	"""
	name = frappe.db.get_value(
		"Warehouse",
		{**_filters(facility, store_type), "disabled": 0, "is_group": 0},
		"name",
		order_by="creation",
	)
	if not name and required:
		frappe.throw(
			_(
				"Facility {0} has no {1} store. Run bench migrate, or ask the EMR System Administrator."
			).format(frappe.bold(facility), frappe.bold(_(store_type))),
			StoreNotFoundError,
			title=_("Store Missing"),
		)
	return name


def facility_of_store(warehouse: str) -> str | None:
	"""The facility (Healthcare Service Unit) a store belongs to, or None for a warehouse outside the EMR stores."""
	return frappe.db.get_value("Warehouse", warehouse, WAREHOUSE_FACILITY_FIELD) if warehouse else None


def store_info(warehouse: str) -> frappe._dict | None:
	"""{facility, store_type, point_of_use, disabled} of a store, or None if it does not exist."""
	return frappe.db.get_value(
		"Warehouse",
		warehouse,
		[
			f"{WAREHOUSE_FACILITY_FIELD} as facility",
			f"{WAREHOUSE_STORE_TYPE_FIELD} as store_type",
			f"{WAREHOUSE_POINT_OF_USE_FIELD} as point_of_use",
			"disabled",
		],
		as_dict=True,
	)


def facility_stores(facility: str) -> list[frappe._dict]:
	"""Every enabled store of `facility`: [{name, store_type, point_of_use}] in tree order."""
	return frappe.get_all(
		"Warehouse",
		filters={WAREHOUSE_FACILITY_FIELD: facility, "disabled": 0, "is_group": 0},
		fields=[
			"name",
			f"{WAREHOUSE_STORE_TYPE_FIELD} as store_type",
			f"{WAREHOUSE_POINT_OF_USE_FIELD} as point_of_use",
		],
		order_by="lft asc",
	)
