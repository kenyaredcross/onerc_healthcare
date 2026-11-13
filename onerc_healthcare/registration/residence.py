# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Residence hierarchy helpers (SDD 4.5). Levels come from operations.json `residence_levels`."""

import frappe

from onerc_healthcare.setup.operations import get_operation, get_operation_config

RESIDENCE_DOCTYPE = "Residence Location"


def get_residence_levels() -> list[str]:
	"""This operation's residence levels, top first (e.g. Village, Neighbourhood, Compound)."""
	if not get_operation():
		return []
	return list(get_operation_config().get("residence_levels") or [])


def get_residence_root_name() -> str | None:
	"""Name of the operation's root node, e.g. "All Kalobeyei Residences"."""
	if not get_operation():
		return None
	return f"All {get_operation_config()['label']} Residences"


def ensure_residence_root() -> None:
	"""Create the operation's root Residence Location if it is missing (idempotent)."""
	root = get_residence_root_name()
	if not root or frappe.db.exists(RESIDENCE_DOCTYPE, root):
		return
	frappe.get_doc({"doctype": RESIDENCE_DOCTYPE, "location_name": root, "is_group": 1}).insert(
		ignore_permissions=True
	)


def ensure_level_options() -> None:
	"""Set the Level select options from the operation's levels; keep any extra options an admin added."""
	levels = get_residence_levels()
	if not levels:
		return
	current = frappe.get_meta(RESIDENCE_DOCTYPE).get_field("level").options or ""
	options = [o for o in current.split("\n") if o]
	missing = [lvl for lvl in levels if lvl not in options]
	if not missing:
		return
	frappe.make_property_setter(
		{
			"doctype": RESIDENCE_DOCTYPE,
			"fieldname": "level",
			"property": "options",
			"value": "\n".join(["", *options, *missing]),
			"property_type": "Text",
		},
		validate_fields_for_doctype=False,
	)
