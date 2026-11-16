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

import json

import frappe

KEY_PREFIX = "krcs-"

# sidebar -> (anchor link_to to insert after, or None for the end, [rows])
# sidebar -> blocks of (anchor link_to to insert after, or None for the end, [rows]). A Section Break row is a
# Marley-style dropdown (icon, indented, starts closed); the links after it fold inside it (`child`).
SIDEBAR_ROWS: dict[str, list[tuple[str | None, list[dict]]]] = {
	"Healthcare": [
		(
			"Patient",
			[
				{
					"key": "krcs-patient-visit",
					"label": "Patient Visit",
					"link_to": "Patient Visit",
					"icon": "clipboard-list",
				},
				{"key": "krcs-referral", "label": "Referral", "link_to": "Referral", "icon": "send"},
			],
		),
		# Dropdowns sit with Marley's own (Masters, Records & History, ...), after its loose links.
		(
			"Practitioner Availability",
			[
				{
					"key": "krcs-frontdesk-section",
					"type": "Section Break",
					"label": "Front Desk",
					"icon": "door-open",
				},
				{"key": "krcs-visitor-record", "label": "Visitor Record", "link_to": "Visitor Record"},
				{"key": "krcs-screening-record", "label": "Screening Record", "link_to": "Screening Record"},
				{
					"key": "krcs-daily-entry",
					"label": "Daily Facility Entry",
					"link_type": "Report",
					"link_to": "Daily Facility Entry",
				},
				{
					"key": "krcs-privacy-section",
					"type": "Section Break",
					"label": "Data Protection",
					"icon": "lock",
				},
				{"key": "krcs-patient-consent", "label": "Patient Consent", "link_to": "Patient Consent"},
				{
					"key": "krcs-consent-exceptions",
					"label": "Consent Exceptions",
					"link_type": "Report",
					"link_to": "Consent Exceptions",
				},
				{
					"key": "krcs-data-access-request",
					"label": "Data Access Request",
					"link_to": "Data Access Request",
				},
				{
					"key": "krcs-access-audit",
					"label": "Access Audit",
					"link_type": "Report",
					"link_to": "Access Audit",
				},
				# Phase 3: services, community work and MOH reporting
				{
					"key": "krcs-services-section",
					"type": "Section Break",
					"label": "Services and Community",
					"icon": "handshake",
				},
				{
					"key": "krcs-service-delivered",
					"label": "Service Delivered",
					"link_to": "Service Delivered",
				},
				{
					"key": "krcs-community-activity",
					"label": "Community Activity Record",
					"link_to": "Community Activity Record",
				},
			],
		),
	],
	# Triage beside Vital Signs: the visits waiting for triage and in triage (filtered Patient Visit lists).
	"Outpatient": [
		(
			"Practitioner Availability",
			[
				{
					"key": "krcs-triage-section",
					"type": "Section Break",
					"label": "Triage",
					"icon": "activity",
					"open": True,
				},
				{
					"key": "krcs-triage-waiting",
					"label": "Waiting for Triage",
					"link_to": "Patient Visit",
					"filters": [["Patient Visit", "status", "=", "Registered", False]],
					"default": False,
				},
				{
					"key": "krcs-triage-in-triage",
					"label": "In Triage",
					"link_to": "Patient Visit",
					"filters": [["Patient Visit", "status", "=", "In Triage", False]],
					"default": False,
				},
				# The clinician's side: routed visits waiting, being seen, and waiting on results.
				{
					"key": "krcs-consult-section",
					"type": "Section Break",
					"label": "Consultation",
					"icon": "stethoscope",
					"open": True,
				},
				{
					"key": "krcs-consult-waiting",
					"label": "Waiting for Consultation",
					"link_to": "Patient Visit",
					"filters": [["Patient Visit", "status", "=", "Routed to Department", False]],
					"default": False,
				},
				{
					"key": "krcs-consult-in-consultation",
					"label": "In Consultation",
					"link_to": "Patient Visit",
					"filters": [["Patient Visit", "status", "=", "In Consultation", False]],
					"default": False,
				},
				{
					"key": "krcs-consult-awaiting-lab",
					"label": "Awaiting Laboratory",
					"link_to": "Patient Visit",
					"filters": [["Patient Visit", "status", "=", "Awaiting Laboratory", False]],
					"default": False,
				},
				# Maternity arrivals (the ward itself is under Inpatient)
				{
					"key": "krcs-op-maternity-section",
					"type": "Section Break",
					"label": "Maternity",
					"icon": "baby",
					"open": True,
				},
				{
					"key": "krcs-op-maternity-waiting",
					"label": "Waiting for Admission",
					"link_to": "Patient Visit",
					"filters": [
						["Patient Visit", "status", "=", "Routed to Department", False],
						["Patient Visit", "current_department", "=", "Maternity", False],
					],
					"default": False,
				},
				{
					"key": "krcs-op-newborns",
					"label": "Newborns",
					"link_to": "Patient",
					"filters": [["Patient", "krcs_mother", "is", "set", False]],
					"default": False,
				},
			],
		),
	],
	"Inpatient": [
		# Phase 5M: the labour ward (Maternity Admission replaces Marley's Inpatient Record, which is hidden).
		(
			"Healthcare Service Unit",
			[
				{
					"key": "krcs-ip-maternity-admission",
					"label": "Maternity Admission",
					"link_to": "Maternity Admission",
					"icon": "bed",
				},
				{
					"key": "krcs-ip-admitted",
					"label": "Admitted",
					"link_to": "Maternity Admission",
					"filters": [["Maternity Admission", "status", "=", "Admitted", False]],
					"default": False,
				},
				{
					"key": "krcs-ip-in-labour",
					"label": "In Labour",
					"link_to": "Maternity Admission",
					"filters": [["Maternity Admission", "status", "=", "In Labour", False]],
					"default": False,
				},
				{
					"key": "krcs-ip-delivered",
					"label": "Delivered",
					"link_to": "Maternity Admission",
					"filters": [["Maternity Admission", "status", "=", "Delivered", False]],
					"default": False,
				},
				{
					"key": "krcs-ip-labour-section",
					"type": "Section Break",
					"label": "Labour and Delivery",
					"icon": "activity",
					"open": True,
				},
				{"key": "krcs-ip-partograph", "label": "Partograph", "link_to": "Partograph"},
				{"key": "krcs-ip-delivery-record", "label": "Delivery Record", "link_to": "Delivery Record"},
				{
					"key": "krcs-ip-records-section",
					"type": "Section Break",
					"label": "Maternity Records",
					"icon": "archive",
				},
				{
					"key": "krcs-ip-discharged",
					"label": "Discharged",
					"link_to": "Maternity Admission",
					"filters": [["Maternity Admission", "status", "=", "Discharged", False]],
					"default": False,
				},
				{
					"key": "krcs-ip-wards",
					"label": "Maternity Wards and Beds",
					"link_to": "Healthcare Service Unit",
					"filters": [["Healthcare Service Unit", "name", "like", "%Maternity Ward%", False]],
					"default": False,
				},
			],
		),
	],
	"Diagnostics": [
		(
			None,
			[
				{
					"key": "krcs-lab-section",
					"type": "Section Break",
					"label": "KRCS Laboratory",
					"icon": "test-tubes",
				},
				{
					"key": "krcs-lab-worklist",
					"label": "Laboratory Worklist",
					"link_type": "Report",
					"link_to": "Laboratory Worklist",
				},
			],
		),
	],
	"Healthcare Setup": [
		(
			None,
			[
				{"key": "krcs-section", "type": "Section Break", "label": "KRCS EMR", "icon": "hospital"},
				{
					"key": "krcs-registration-settings",
					"label": "EMR Registration Settings",
					"link_to": "EMR Registration Settings",
				},
				{
					"key": "krcs-naming-settings",
					"label": "EMR Naming Settings",
					"link_to": "EMR Naming Settings",
				},
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
				{
					"key": "krcs-referral-facility",
					"label": "Referral Facility",
					"link_to": "Referral Facility",
				},
				{
					"key": "krcs-screening-parameter",
					"label": "Screening Parameter",
					"link_to": "Screening Parameter",
				},
				{
					"key": "krcs-consent-statement",
					"label": "Consent Statement",
					"link_to": "Consent Statement",
				},
				{"key": "krcs-access-log-entry", "label": "Access Log Entry", "link_to": "Access Log Entry"},
				# Phase 3 masters
				{
					"key": "krcs-services-setup-section",
					"type": "Section Break",
					"label": "Services and Community",
					"icon": "handshake",
				},
				{"key": "krcs-service-type", "label": "Visit Service Type", "link_to": "Visit Service Type"},
				{
					"key": "krcs-community-activity-type",
					"label": "Community Activity Type",
					"link_to": "Community Activity Type",
				},
			],
		),
	],
}


def _row_values(row: dict) -> dict:
	"""Stored values of one of our rows. `row["_child"]` is set by `_desired_blocks` on links under a section."""
	is_link = row.get("type", "Link") == "Link"
	is_section = row.get("type") == "Section Break"
	return {
		"type": row.get("type", "Link"),
		"label": row["label"],
		"link_type": row.get("link_type", "DocType"),
		"link_to": row.get("link_to") if is_link else None,
		"icon": row.get("icon"),
		# A filtered link to a DocType another sidebar owns (Patient Visit) must not claim it.
		"is_default_module": 1 if is_link and row.get("default", True) else 0,
		"collapsible": 1,
		"keep_closed": 1 if is_section and not row.get("open") else 0,
		"filters": json.dumps(row["filters"]) if row.get("filters") else None,
		"indent": 1 if is_section else 0,
		"child": 1 if row.get("_child") else 0,
		"open_in_new_tab": 0,
		"key": row["key"],
	}


def _desired_rows(rows: list[dict]) -> list[dict]:
	"""`rows` without links to DocTypes/Reports missing on this site, without section breaks left with
	nothing under them, and with `_child` set on links that follow a section."""
	kept = [
		r
		for r in rows
		if r.get("type") == "Section Break" or frappe.db.exists(r.get("link_type", "DocType"), r["link_to"])
	]
	# Drop a section break followed by another section break or by nothing (its links do not exist yet).
	kept = [
		r
		for i, r in enumerate(kept)
		if r.get("type") != "Section Break"
		or (i + 1 < len(kept) and kept[i + 1].get("type") != "Section Break")
	]
	in_section, out = False, []
	for r in kept:
		in_section = r.get("type") == "Section Break" or in_section
		out.append({**r, "_child": in_section and r.get("type") != "Section Break"})
	return out


def _desired_blocks(sidebar: str) -> list[tuple[str | None, list[dict]]]:
	return [(anchor, _desired_rows(rows)) for anchor, rows in SIDEBAR_ROWS[sidebar]]


# Areas of their own, like Marley's Outpatient or Rehabilitation: Pharmacy (`emr_pharmacy/sidebar|workspace/pharmacy`),
# Outreach (`emr_outreach/...`) and MOH Reporting (`emr_reporting/.../moh_reporting`). Our app ships a companion Dock
# (`dock/onerc_healthcare`, mount_on "healthcare"), so the desk keeps Marley's rail on screen in them. Companion
# rows are appended at the end of the host's rail; these rows in Marley's dock place each area next to its
# neighbours (Frappe's dedupe drops the companion duplicate). Written directly, like the sidebar rows: Marley's
# Dock is never saved.
MARLEY_DOCK = "healthcare"
DOCK_ROWS = (
	# (title, sidebar it opens, icon, insert after this sidebar)
	("Pharmacy", "Pharmacy", "pill", "Diagnostics"),
	("Outreach", "Outreach", "route", "Rehabilitation"),
	("MOH Reporting", "MOH Reporting", "file-chart-column", "Outreach"),
)

# Marley dock rows we hide (link_type, link_to). None today: the labour ward lives in Marley's Inpatient area, with
# the Inpatient Record parts hidden inside it (HIDDEN_SIDEBAR_ITEMS). UNHIDDEN_DOCK_ROWS puts back a row an
# earlier release hid. RETIRED_DOCK_SIDEBARS are our own rows removed from the rail (the Maternity area, which
# the user split into Inpatient and Outpatient on 2026-11-16).
HIDDEN_DOCK_ROWS: tuple = ()
UNHIDDEN_DOCK_ROWS = (("Sidebar", "Inpatient"),)
RETIRED_DOCK_SIDEBARS = ("Maternity",)
RETIRED_AREAS = ("Maternity",)  # our Sidebar + Workspace of that name (module EMR Maternity) are deleted

# Marley sidebar items hidden (hidden = 1, direct write, re-applied every migrate). Inpatient: Marley's Inpatient
# Record crashes on every save without an enabled selling price list (D18) and carries bed-rent billing and stock
# (C-5); its medication order/entry, treatment counselling and discharge summary hang off it; billing links go.
HIDDEN_SIDEBAR_ITEMS = {
	"Inpatient": (
		"Inpatient Record",
		"Inpatient Medication Order",
		"Inpatient Medication Entry",
		"Clinical Procedure",
		"Treatment Counselling",
		"Discharge Summary",
		"Sales Invoice",
		"Payment Entry",
		"Billing & Payments",  # the section break (by label)
	),
}


def install() -> bool:
	"""Apply SIDEBAR_ROWS and DOCK_ROWS. Returns True when anything changed."""
	changed = False
	for sidebar in SIDEBAR_ROWS:
		if frappe.db.exists("Sidebar", sidebar):
			changed |= _apply(sidebar, _desired_blocks(sidebar))
	changed |= apply_dock()
	changed |= hide_dock_rows()
	changed |= retire_areas()
	changed |= hide_sidebar_items()
	if changed:
		frappe.clear_cache()
	return changed


def _apply(sidebar: str, blocks: list[tuple[str | None, list[dict]]]) -> bool:
	current = frappe.get_all(
		"Sidebar Item",
		filters={"parent": sidebar, "parenttype": "Sidebar", "parentfield": "items"},
		fields=[
			"name",
			"idx",
			"key",
			"link_to",
			"type",
			"label",
			"icon",
			"is_default_module",
			"child",
			"indent",
			"keep_closed",
			"filters",
		],
		order_by="idx asc",
	)
	ours = {r.key: r for r in current if (r.key or "").startswith(KEY_PREFIX)}
	theirs = [r for r in current if r.key not in ours]
	desired = [d for _anchor, rows in blocks for d in rows]

	# Each block goes after its anchor (one of Marley's rows), or at the end; blocks keep their order.
	after: dict[int, list[dict]] = {}
	for anchor, rows in blocks:
		position = len(theirs)
		if anchor:
			position = next((i + 1 for i, r in enumerate(theirs) if r.link_to == anchor), position)
		after.setdefault(position, []).extend(rows)
	# Target order as ("ours", definition) / ("theirs", existing row).
	target = []
	for i in range(len(theirs) + 1):
		target += [("ours", d) for d in after.get(i, [])]
		if i < len(theirs):
			target.append(("theirs", theirs[i]))

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


def apply_dock() -> bool:
	"""Put each DOCK_ROWS entry in Marley's dock after its anchor (idempotent). Returns True when changed."""
	if not frappe.db.exists("Dock", MARLEY_DOCK):
		return False
	changed = False
	for title, sidebar, icon, after in DOCK_ROWS:
		if not frappe.db.exists("Sidebar", sidebar):
			continue
		rows = frappe.get_all(
			"Dock Item",
			filters={"parent": MARLEY_DOCK, "parenttype": "Dock", "parentfield": "items"},
			fields=["name", "idx", "link_type", "link_to", "title", "icon"],
			order_by="idx asc",
		)
		mine = [r for r in rows if r.link_type == "Sidebar" and r.link_to == sidebar]
		others = [r for r in rows if r not in mine]
		position = next((i + 1 for i, r in enumerate(others) if r.link_to == after), len(others))
		order = [r.name for r in others[:position]] + ["__mine__"] + [r.name for r in others[position:]]
		current = [("__mine__" if r in mine else r.name) for r in rows]
		if len(mine) == 1 and current == order and (mine[0].title, mine[0].icon) == (title, icon):
			continue
		for r in mine[1:]:
			frappe.db.delete("Dock Item", {"name": r.name})
		if mine:
			name = mine[0].name
			frappe.db.set_value(
				"Dock Item", name, {"title": title, "icon": icon, "hidden": 0}, update_modified=False
			)
		else:
			doc = frappe.get_doc(
				{
					"doctype": "Dock Item",
					"name": frappe.generate_hash(length=10),
					"parent": MARLEY_DOCK,
					"parenttype": "Dock",
					"parentfield": "items",
					"title": title,
					"link_type": "Sidebar",
					"link_to": sidebar,
					"icon": icon,
					"added": 0,
					"hidden": 0,
				}
			)
			doc.db_insert()
			name = doc.name
		for idx, row_name in enumerate(order, start=1):
			frappe.db.set_value(
				"Dock Item", name if row_name == "__mine__" else row_name, "idx", idx, update_modified=False
			)
		changed = True
	return changed


def hide_dock_rows() -> bool:
	"""Set ``hidden = 1`` on each HIDDEN_DOCK_ROWS row of Marley's dock (idempotent). Returns True when changed."""
	if not frappe.db.exists("Dock", MARLEY_DOCK):
		return False
	changed = False
	for link_type, link_to in HIDDEN_DOCK_ROWS:
		names = frappe.get_all(
			"Dock Item",
			filters={
				"parent": MARLEY_DOCK,
				"parenttype": "Dock",
				"parentfield": "items",
				"link_type": link_type,
				"link_to": link_to,
				"hidden": 0,
			},
			pluck="name",
		)
		for name in names:
			frappe.db.set_value("Dock Item", name, "hidden", 1, update_modified=False)
			changed = True
	if changed:
		frappe.clear_document_cache("Dock", MARLEY_DOCK)
	return changed


def retire_areas() -> bool:
	"""Remove our retired areas: their dock rows, Sidebar and Workspace; put back Marley rows we used to hide."""
	changed = False
	if frappe.db.exists("Dock", MARLEY_DOCK):
		for link_to in RETIRED_DOCK_SIDEBARS:
			rows = frappe.get_all(
				"Dock Item",
				filters={
					"parent": MARLEY_DOCK,
					"parenttype": "Dock",
					"link_type": "Sidebar",
					"link_to": link_to,
				},
				pluck="name",
			)
			for name in rows:
				frappe.db.delete("Dock Item", {"name": name})
				changed = True
		for link_type, link_to in UNHIDDEN_DOCK_ROWS:
			rows = frappe.get_all(
				"Dock Item",
				filters={"parent": MARLEY_DOCK, "link_type": link_type, "link_to": link_to, "hidden": 1},
				pluck="name",
			)
			for name in rows:
				frappe.db.set_value("Dock Item", name, "hidden", 0, update_modified=False)
				changed = True
		if changed:
			frappe.clear_document_cache("Dock", MARLEY_DOCK)
	for name in RETIRED_AREAS:
		for doctype in ("Workspace", "Sidebar"):
			if (
				frappe.db.exists(doctype, name)
				and frappe.db.get_value(doctype, name, "module") == "EMR Maternity"
			):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
				changed = True
	return changed


def hide_sidebar_items() -> bool:
	"""Set ``hidden = 1`` on Marley's HIDDEN_SIDEBAR_ITEMS (matched by link_to, or label for a section)."""
	changed = False
	for sidebar, targets in HIDDEN_SIDEBAR_ITEMS.items():
		if not frappe.db.exists("Sidebar", sidebar):
			continue
		rows = frappe.get_all(
			"Sidebar Item",
			filters={"parent": sidebar, "parenttype": "Sidebar", "hidden": 0},
			fields=["name", "link_to", "label", "key"],
		)
		for row in rows:
			if (row.key or "").startswith(KEY_PREFIX):
				continue
			if row.link_to in targets or row.label in targets:
				frappe.db.set_value("Sidebar Item", row.name, "hidden", 1, update_modified=False)
				changed = True
	return changed
