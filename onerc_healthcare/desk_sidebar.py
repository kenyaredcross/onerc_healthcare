# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Place the EMR's own DocTypes in Marley's sidebars (D25).

Frappe 16 shows a DocType's pages under the sidebar that claims it with an ``is_default_module`` item;
with no claim it falls back to the DocType's own module. Ours live in EMR modules that have no pages of
their own (D24), so Patient Visit opened under a bare generated "EMR Visit" sidebar. Here Marley's
"Healthcare" sidebar claims Patient Visit (next to Patient), and "Healthcare Setup" claims the EMR
settings and masters in a "KRCS EMR" section.

Like ``visit/workspaces.py``: rows are written directly and Marley's Sidebar documents are never saved,
so nothing is exported into Marley's files in developer mode. Our rows carry a ``key`` starting
``krcs-``; every migrate puts them back in place (idempotent, and restored after a Marley upgrade
reloads its sidebars).
"""

import frappe

KEY_PREFIX = "krcs-"

# sidebar -> (anchor link_to to insert after, or None for the end, [rows])
SIDEBAR_ROWS: dict[str, tuple[str | None, list[dict]]] = {
	"Healthcare": (
		"Patient",
		[
			{
				"key": "krcs-patient-visit",
				"label": "Patient Visit",
				"link_to": "Patient Visit",
				"icon": "clipboard-list",
			},
		],
	),
	"Healthcare Setup": (
		None,
		[
			{"key": "krcs-section", "type": "Section Break", "label": "KRCS EMR"},
			{
				"key": "krcs-registration-settings",
				"label": "EMR Registration Settings",
				"link_to": "EMR Registration Settings",
			},
			{"key": "krcs-naming-settings", "label": "EMR Naming Settings", "link_to": "EMR Naming Settings"},
			{
				"key": "krcs-identifier-type",
				"label": "Patient Identifier Type",
				"link_to": "Patient Identifier Type",
			},
			{
				"key": "krcs-residence-location",
				"label": "Residence Location",
				"link_to": "Residence Location",
			},
		],
	),
}


def _row_values(row: dict) -> dict:
	is_link = row.get("type", "Link") == "Link"
	return {
		"type": row.get("type", "Link"),
		"label": row["label"],
		"link_type": "DocType",
		"link_to": row.get("link_to") if is_link else None,
		"icon": row.get("icon"),
		"is_default_module": 1 if is_link else 0,
		"collapsible": 1,
		"open_in_new_tab": 0,
		"key": row["key"],
	}


def _desired_rows(sidebar: str) -> list[dict]:
	"""Our rows for `sidebar`, skipping links to DocTypes that do not exist on this site."""
	_anchor, rows = SIDEBAR_ROWS[sidebar]
	return [r for r in rows if r.get("type") == "Section Break" or frappe.db.exists("DocType", r["link_to"])]


def install() -> bool:
	"""Apply SIDEBAR_ROWS. Returns True when anything changed."""
	changed = False
	for sidebar, (anchor, _rows) in SIDEBAR_ROWS.items():
		if frappe.db.exists("Sidebar", sidebar):
			changed |= _apply(sidebar, anchor, _desired_rows(sidebar))
	if changed:
		frappe.clear_cache()
	return changed


def _apply(sidebar: str, anchor: str | None, desired: list[dict]) -> bool:
	current = frappe.get_all(
		"Sidebar Item",
		filters={"parent": sidebar, "parenttype": "Sidebar", "parentfield": "items"},
		fields=["name", "idx", "key", "link_to", "type", "label", "icon", "is_default_module"],
		order_by="idx asc",
	)
	ours = {r.key: r for r in current if (r.key or "").startswith(KEY_PREFIX)}
	theirs = [r for r in current if r.key not in ours]

	position = len(theirs)
	if anchor:
		position = next((i + 1 for i, r in enumerate(theirs) if r.link_to == anchor), position)
	# Target order as ("ours", definition) / ("theirs", existing row).
	target = (
		[("theirs", r) for r in theirs[:position]]
		+ [("ours", d) for d in desired]
		+ [("theirs", r) for r in theirs[position:]]
	)

	in_place = [r.key if r.key in ours else r.name for r in current] == [
		item["key"] if kind == "ours" else item.name for kind, item in target
	]
	rows_match = set(ours) == {d["key"] for d in desired} and all(
		_matches(ours[d["key"]], _row_values(d)) for d in desired
	)
	if in_place and rows_match:
		return False

	for row in ours.values():
		frappe.db.delete("Sidebar Item", {"name": row.name})
	for idx, (kind, item) in enumerate(target, start=1):
		if kind == "ours":
			frappe.get_doc(
				{
					"doctype": "Sidebar Item",
					"parent": sidebar,
					"parenttype": "Sidebar",
					"parentfield": "items",
					"idx": idx,
					**_row_values(item),
				}
			).db_insert()
		elif item.idx != idx:
			frappe.db.set_value("Sidebar Item", item.name, "idx", idx, update_modified=False)
	return True


def _matches(row, values: dict) -> bool:
	return all((row.get(k) or None) == (v or None) for k, v in values.items() if k in row)
