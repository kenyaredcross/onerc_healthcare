# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Read-only helpers for the site's operation (kalobeyei | dadaab). Shared; builders import only."""

import json
import os

import frappe

from onerc_healthcare.constants import OPERATIONS

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def load_data(filename: str) -> dict:
	with open(os.path.join(_DATA_DIR, filename), encoding="utf-8") as f:
		return json.load(f)


def get_operation() -> str | None:
	"""The site's operation from site config `krcs_operation`, or None if unset or unknown."""
	op = (frappe.conf.get("krcs_operation") or "").strip().lower()
	return op if op in OPERATIONS else None


def get_operation_config(op: str | None = None) -> dict:
	"""The operations.json entry for `op` (default: this site's operation)."""
	op = op or get_operation()
	if not op:
		frappe.throw(f"Site config krcs_operation must be one of {', '.join(OPERATIONS)}")
	return load_data("operations.json")[op]


def get_company() -> str:
	return get_operation_config()["company_name"]


def get_company_abbr() -> str:
	return get_operation_config()["abbr"]


def facility_unit_name(facility: dict, abbr: str) -> str:
	"""Healthcare Service Unit name of a facility group (Marley appends ` - <ABBR>`)."""
	return f"{facility['name']} - {abbr}"


def lab_unit_name(facility: dict, abbr: str) -> str:
	return f"{facility['short']} Laboratory - {abbr}"


def get_facilities() -> list[str]:
	"""Healthcare Service Unit names of this operation's facility groups, in operations.json order."""
	cfg = get_operation_config()
	return [facility_unit_name(f, cfg["abbr"]) for f in cfg["facilities"]]
