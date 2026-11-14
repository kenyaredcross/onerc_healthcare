# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Department queues on Marley's existing workspaces (FR-8, BR-3, SDD §5.2) and billing clean-up (C-5).

Each department gets a labelled block (header, shortcuts, quick list of Patient Visits) on one of Marley's
standard pages: Healthcare, Outpatient or Diagnostics. The manager overview number cards go on Healthcare.

The Workspace documents are never saved: in developer mode a save exports into Marley's JSON files. Child
rows are written directly and `content` is set with `db.set_value`. The step is idempotent and
upgrade-safe:

- our rows are recognised by their labels (every label is department-prefixed or one of our Number Card
  names, so it cannot collide with a Marley row); they are rewritten only when they differ from the
  definition, so a re-run changes nothing;
- our content blocks have ids starting with ``krcs-``, are removed and re-inserted after Marley's
  onboarding block, and the content is written only when it changes;
- a Marley update reloads its JSON and drops our rows; the next ``after_migrate`` puts them back.

Billing items (C-5) are removed by rule: shortcuts, links, number cards and charts whose target or source
DocType is a billing DocType, or whose filters use a billing field (for example "Appointments To Bill").

If a site admin customises one of these pages in the desk (a Custom Workspace content snapshot), Frappe
shows that snapshot instead of the base content; that is the admin's choice and is left alone.
"""

from __future__ import annotations

import json

import frappe

from onerc_healthcare.constants import (
	ACCESS_LOG_DOCTYPE,
	CONSENT_DOCTYPE,
	DATA_ACCESS_REQUEST_DOCTYPE,
	DEPT_HIS,
	DEPT_LABORATORY,
	DEPT_MCH,
	DEPT_MENTAL_HEALTH,
	DEPT_NUTRITION,
	DEPT_OUTPATIENT,
	DEPT_PHARMACY,
	DEPT_RECEPTION,
	DEPT_TRIAGE,
	DISPENSING_DOCTYPE,
	ENCOUNTER_DOCTYPE,
	MOH_RUN_DOCTYPE,
	REFERRAL_DOCTYPE,
	REPORT_ACCESS_AUDIT,
	REPORT_CONSENT_EXCEPTIONS,
	REPORT_DAILY_ENTRY,
	REPORT_LAB_WORKLIST,
	REPORT_MOH_UNMAPPED,
	REPORT_PHARMACY_WORKLIST,
	SCREENING_DOCTYPE,
	SYNC_CONFLICT_DOCTYPE,
	VISIT_CLOSED_STATUSES,
	VISIT_DOCTYPE,
	VISIT_OPEN_STATUSES,
	VISIT_STATUS_IN_CONSULTATION,
	VISIT_STATUS_IN_TRIAGE,
	VISIT_STATUS_REGISTERED,
	VISIT_STATUS_ROUTED,
	VISIT_STATUSES,
	VISITOR_DOCTYPE,
)
from onerc_healthcare.security.matrix import BILLING_DOCTYPES

WS_HEALTHCARE = "Healthcare"
WS_OUTPATIENT = "Outpatient"
WS_DIAGNOSTICS = "Diagnostics"

# Department -> Marley workspace that carries its queue block.
DEPARTMENT_WORKSPACE = {
	DEPT_RECEPTION: WS_HEALTHCARE,
	DEPT_TRIAGE: WS_HEALTHCARE,
	DEPT_PHARMACY: WS_HEALTHCARE,
	DEPT_HIS: WS_HEALTHCARE,
	DEPT_OUTPATIENT: WS_OUTPATIENT,
	DEPT_MCH: WS_OUTPATIENT,
	DEPT_MENTAL_HEALTH: WS_OUTPATIENT,
	DEPT_NUTRITION: WS_OUTPATIENT,
	DEPT_LABORATORY: WS_DIAGNOSTICS,
}

# Our old standalone workspaces (removed); deleted here if migrate left any behind.
RETIRED_WORKSPACES = (*(f"{d} Queue" for d in DEPARTMENT_WORKSPACE), "Visit Overview")

# Manager overview number cards (shipped in emr_visit/number_card/), shown on Healthcare.
OVERVIEW_CARDS = (
	*(f"Visits {s}" + (" Today" if s in VISIT_CLOSED_STATUSES else "") for s in VISIT_STATUSES),
	"Longest Current Wait",
)

BLOCK_PREFIX = "krcs-"
# Filters on these fields mark a number card or chart as billing (C-5).
BILLING_FIELDS = frozenset(
	("invoiced", "ref_sales_invoice", "sales_invoice", "paid_amount", "outstanding_amount", "invoice")
)


def _f(field: str, op: str, value) -> list:
	return [VISIT_DOCTYPE, field, op, value]


OPEN = _f("status", "in", list(VISIT_OPEN_STATUSES))


def _shortcut(label: str, filters: list, color: str = "Blue", fmt: str = "{} waiting") -> dict:
	return {
		"type": "DocType",
		"link_to": VISIT_DOCTYPE,
		"doc_view": "List",
		"label": label,
		"color": color,
		"format": fmt,
		"stats_filter": json.dumps(filters),
	}


def _quick_list(label: str, filters: list) -> dict:
	return {"document_type": VISIT_DOCTYPE, "label": label, "quick_list_filter": json.dumps(filters)}


def _doc_shortcut(
	label: str,
	doctype: str,
	filters: list | None = None,
	color: str = "Blue",
	fmt: str = "{} open",
	view: str = "List",
) -> dict:
	"""Shortcut to another DocType (Phase 2 records). `filters` are [doctype, field, op, value] rows."""
	row = {"type": "DocType", "link_to": doctype, "doc_view": view, "label": label, "color": color}
	if filters:
		row.update({"format": fmt, "stats_filter": json.dumps(filters)})
	return row


def _doc_quick_list(label: str, doctype: str, filters: list) -> dict:
	return {"document_type": doctype, "label": label, "quick_list_filter": json.dumps(filters)}


def _report_shortcut(label: str, report: str, ref_doctype: str, color: str = "Orange") -> dict:
	return {
		"type": "Report",
		"link_to": report,
		"report_ref_doctype": ref_doctype,
		"label": label,
		"color": color,
	}


def department_filters(dept: str) -> list:
	return [_f("current_department", "=", dept), OPEN]


def department_block(dept: str) -> dict:
	"""Shortcuts and quick lists of one department's queue block. Labels are department-prefixed."""
	if dept == DEPT_RECEPTION:
		shortcuts = [
			_shortcut(f"{dept}: Waiting", [_f("status", "=", VISIT_STATUS_REGISTERED)], "Orange"),
			_shortcut(f"{dept}: All Open Visits", [OPEN], "Blue", "{} open"),
			{"type": "DocType", "link_to": VISIT_DOCTYPE, "doc_view": "New", "label": f"{dept}: New Visit"},
			# B3 front desk (FR-41/42) and B4 consent (FR-37)
			_doc_shortcut(f"{dept}: New Screening", SCREENING_DOCTYPE, view="New"),
			_doc_shortcut(
				f"{dept}: Visitors Inside",
				VISITOR_DOCTYPE,
				[[VISITOR_DOCTYPE, "status", "=", "Inside"]],
				"Orange",
				"{} inside",
			),
			_doc_shortcut(f"{dept}: Patient Consent", CONSENT_DOCTYPE),
			_report_shortcut(f"{dept}: Daily Facility Entry", REPORT_DAILY_ENTRY, VISIT_DOCTYPE, "Blue"),
		]
		quick_lists = [
			_quick_list(f"{dept} Queue", [_f("status", "=", VISIT_STATUS_REGISTERED)]),
			_doc_quick_list(
				f"{dept}: Visitors Inside", VISITOR_DOCTYPE, [[VISITOR_DOCTYPE, "status", "=", "Inside"]]
			),
			_doc_quick_list(
				f"{dept}: Screenings Outside Expected Range",
				SCREENING_DOCTYPE,
				[
					[SCREENING_DOCTYPE, "docstatus", "=", 1],
					[SCREENING_DOCTYPE, "outcome", "=", "Outside Expected Range"],
				],
			),
		]
	elif dept == DEPT_TRIAGE:
		shortcuts = [
			_shortcut(f"{dept}: Waiting for Triage", [_f("status", "=", VISIT_STATUS_REGISTERED)], "Orange"),
			_shortcut(
				f"{dept}: In Triage", [_f("status", "=", VISIT_STATUS_IN_TRIAGE)], "Blue", "{} in triage"
			),
			{
				"type": "DocType",
				"link_to": "Vital Signs",
				"doc_view": "List",
				"label": f"{dept}: Vital Signs",
			},
		]
		quick_lists = [
			_quick_list(f"{dept}: Waiting for Triage", [_f("status", "=", VISIT_STATUS_REGISTERED)]),
			_quick_list(f"{dept}: In Triage", [_f("status", "=", VISIT_STATUS_IN_TRIAGE)]),
		]
	elif dept == DEPT_LABORATORY:
		# B2 (FR-16): worklist, samples and results to approve.
		shortcuts = [
			_shortcut(f"{dept}: Awaiting Laboratory", [_f("lab_open", "=", 1), OPEN], "Purple"),
			_report_shortcut(f"{dept}: Worklist", REPORT_LAB_WORKLIST, "Service Request"),
			_doc_shortcut(
				f"{dept}: Samples to Collect",
				"Sample Collection",
				[["Sample Collection", "docstatus", "=", 0]],
				"Orange",
			),
			_doc_shortcut(
				f"{dept}: Results to Approve",
				"Diagnostic Report",
				[["Diagnostic Report", "status", "!=", "Approved"]],
				"Blue",
			),
			_shortcut(f"{dept}: Routed to {dept}", department_filters(dept), "Blue"),
		]
		quick_lists = [
			_quick_list(f"{dept} Queue", department_filters(dept)),
			_doc_quick_list(
				f"{dept}: Open Sample Collections",
				"Sample Collection",
				[["Sample Collection", "docstatus", "=", 0]],
			),
		]
	elif dept == DEPT_PHARMACY:
		# B3 (FR-20): worklist, open prescriptions, dispensing records.
		open_lines = [
			["Medication Request", "docstatus", "=", 1],
			["Medication Request", "krcs_dispense_status", "in", ["Pending", "Partly Dispensed"]],
		]
		shortcuts = [
			_shortcut(f"{dept}: Awaiting Pharmacy", [_f("pharmacy_open", "=", 1)], "Red"),
			_report_shortcut(f"{dept}: Worklist", REPORT_PHARMACY_WORKLIST, "Medication Request"),
			_doc_shortcut(
				f"{dept}: Dispensing Records",
				DISPENSING_DOCTYPE,
				[[DISPENSING_DOCTYPE, "docstatus", "=", 1]],
				"Green",
				"{} dispensed",
			),
			_shortcut(f"{dept}: Routed to {dept}", department_filters(dept), "Blue"),
		]
		quick_lists = [
			_quick_list(f"{dept} Queue", department_filters(dept)),
			_doc_quick_list(f"{dept}: Open Prescriptions", "Medication Request", open_lines),
		]
	elif dept == DEPT_HIS:
		# B4 (FR-38): data access requests and consent exceptions.
		not_closed = [DATA_ACCESS_REQUEST_DOCTYPE, "status", "!=", "Closed"]
		shortcuts = [
			_doc_shortcut(
				f"{dept}: Data Access Requests", DATA_ACCESS_REQUEST_DOCTYPE, [not_closed], "Orange"
			),
			_doc_shortcut(
				f"{dept}: Overdue Requests",
				DATA_ACCESS_REQUEST_DOCTYPE,
				[not_closed, [DATA_ACCESS_REQUEST_DOCTYPE, "due_date", "<", "Today"]],
				"Red",
				"{} overdue",
			),
			_report_shortcut(f"{dept}: Consent Exceptions", REPORT_CONSENT_EXCEPTIONS, CONSENT_DOCTYPE),
			_shortcut(f"{dept}: Routed to {dept}", department_filters(dept), "Blue"),
			# Phase 3 (FR-29..31): MOH report runs and the ICD-11 mapping exceptions.
			_doc_shortcut(
				f"{dept}: MOH Runs to Submit",
				MOH_RUN_DOCTYPE,
				[[MOH_RUN_DOCTYPE, "status", "=", "Generated"]],
				"Orange",
				"{} to submit",
			),
			_report_shortcut(f"{dept}: Unmapped Diagnoses", REPORT_MOH_UNMAPPED, "MOH Diagnosis Mapping"),
			# Phase 4 (FR-26, OI-11): outreach records waiting on the HIS Officer.
			_doc_shortcut(
				f"{dept}: Open Sync Conflicts",
				SYNC_CONFLICT_DOCTYPE,
				[[SYNC_CONFLICT_DOCTYPE, "status", "=", "Open"]],
				"Red",
				"{} open",
			),
		]
		quick_lists = [
			_quick_list(f"{dept} Queue", department_filters(dept)),
			_doc_quick_list(f"{dept}: Open Data Access Requests", DATA_ACCESS_REQUEST_DOCTYPE, [not_closed]),
			_doc_quick_list(
				f"{dept}: Open MOH Runs",
				MOH_RUN_DOCTYPE,
				[[MOH_RUN_DOCTYPE, "status", "in", ["Draft", "Generated"]]],
			),
		]
	else:
		shortcuts = [
			_shortcut(f"{dept}: Routed to {dept}", department_filters(dept), "Blue"),
			_shortcut(
				f"{dept}: Waiting for Consultation",
				[_f("current_department", "=", dept), _f("status", "=", VISIT_STATUS_ROUTED)],
				"Orange",
			),
			_shortcut(
				f"{dept}: In Consultation",
				[_f("current_department", "=", dept), _f("status", "=", VISIT_STATUS_IN_CONSULTATION)],
				"Green",
				"{} in consultation",
			),
		]
		quick_lists = [
			_quick_list(f"{dept} Queue", department_filters(dept)),
			# B1: this department's consultations not yet completed.
			_doc_quick_list(
				f"{dept}: Open Consultations",
				ENCOUNTER_DOCTYPE,
				[[ENCOUNTER_DOCTYPE, "docstatus", "=", 0], [ENCOUNTER_DOCTYPE, "krcs_department", "=", dept]],
			),
		]
	return {"shortcuts": shortcuts, "quick_lists": quick_lists}


def workspace_definition(workspace: str) -> dict:
	"""Our rows and content blocks for one Marley workspace."""
	shortcuts, quick_lists, number_cards, blocks = [], [], [], []
	if workspace == WS_HEALTHCARE:
		blocks.append(_header("overview", "Visit Overview"))
		for card in OVERVIEW_CARDS:
			if frappe.db.exists("Number Card", card):
				number_cards.append({"number_card_name": card, "label": card})
				blocks.append(_block("number_card", card, 3))
		# Phase 2 oversight (B1 referrals, B4 data access and audit): Medical In-Charge.
		overview = [
			_doc_shortcut(
				"Visit Overview: Open Referrals",
				REFERRAL_DOCTYPE,
				[[REFERRAL_DOCTYPE, "status", "in", ["Sent", "Received"]]],
				"Orange",
			),
			_doc_shortcut(
				"Visit Overview: Requests Awaiting Decision",
				DATA_ACCESS_REQUEST_DOCTYPE,
				[[DATA_ACCESS_REQUEST_DOCTYPE, "status", "=", "Identity Verified"]],
				"Red",
				"{} to decide",
			),
			_report_shortcut(
				"Visit Overview: Consent Exceptions", REPORT_CONSENT_EXCEPTIONS, CONSENT_DOCTYPE
			),
			_report_shortcut("Visit Overview: Access Audit", REPORT_ACCESS_AUDIT, ACCESS_LOG_DOCTYPE, "Grey"),
		]
		for sc in overview:
			blocks.append(_block("shortcut", sc["label"], 3))
		shortcuts += overview
		blocks.append(_spacer("overview"))
	for dept, ws in DEPARTMENT_WORKSPACE.items():
		if ws != workspace:
			continue
		block = department_block(dept)
		key = dept.lower().replace(" ", "-")
		blocks.append(_header(key, f"{dept} Queue"))
		for sc in block["shortcuts"]:
			blocks.append(_block("shortcut", sc["label"], 4))
		for ql in block["quick_lists"]:
			blocks.append(_block("quick_list", ql["label"], 6))
		blocks.append(_spacer(key))
		shortcuts += block["shortcuts"]
		quick_lists += block["quick_lists"]
	return {
		"shortcuts": shortcuts,
		"quick_lists": quick_lists,
		"number_cards": number_cards,
		"blocks": blocks,
	}


def _header(key: str, text: str) -> dict:
	return {
		"id": f"{BLOCK_PREFIX}h-{key}",
		"type": "header",
		"data": {"text": f'<span class="h4"><b>{text}</b></span>', "col": 12},
	}


def _spacer(key: str) -> dict:
	return {"id": f"{BLOCK_PREFIX}s-{key}", "type": "spacer", "data": {"col": 12}}


def _block(block_type: str, label: str, col: int) -> dict:
	key = frappe.scrub(label).replace("_", "-")
	return {
		"id": f"{BLOCK_PREFIX}{block_type[0]}-{key}",
		"type": block_type,
		"data": {f"{block_type}_name": label, "col": col},
	}


# Child table -> (child DocType, fields compared/written).
CHILD_TABLES = {
	"shortcuts": (
		"Workspace Shortcut",
		("type", "link_to", "doc_view", "label", "color", "format", "stats_filter"),
	),
	"quick_lists": ("Workspace Quick List", ("document_type", "label", "quick_list_filter")),
	"number_cards": ("Workspace Number Card", ("number_card_name", "label")),
}


def install() -> None:
	"""Put the department queues on Marley's workspaces and strip billing items. Idempotent."""
	changed = remove_retired_workspaces()
	for workspace in (WS_HEALTHCARE, WS_OUTPATIENT, WS_DIAGNOSTICS):
		if frappe.db.exists("Workspace", workspace):
			changed |= apply_workspace(workspace)
	if changed:
		frappe.cache.delete_key("bootinfo")
		from frappe.cache_manager import clear_user_cache

		clear_user_cache()


def remove_retired_workspaces() -> bool:
	removed = False
	for name in RETIRED_WORKSPACES:
		if (
			frappe.db.exists("Workspace", name)
			and frappe.db.get_value("Workspace", name, "module") == "EMR Visit"
		):
			frappe.delete_doc("Workspace", name, force=True, ignore_permissions=True)
			removed = True
	return removed


def apply_workspace(workspace: str) -> bool:
	definition = workspace_definition(workspace)
	changed = False
	removed_labels: dict[str, set[str]] = {}
	for parentfield, (child_doctype, fields) in CHILD_TABLES.items():
		changed |= _sync_rows(workspace, parentfield, child_doctype, fields, definition[parentfield])
	billing = remove_billing_rows(workspace)
	for parentfield, labels in billing.items():
		removed_labels[parentfield] = labels
		changed |= bool(labels)
	changed |= _sync_content(workspace, definition["blocks"], removed_labels)
	if changed:
		frappe.clear_document_cache("Workspace", workspace)
	return changed


def _sync_rows(workspace: str, parentfield: str, child_doctype: str, fields: tuple, wanted: list) -> bool:
	"""Make our rows (recognised by label) equal `wanted`; leave every other row untouched."""
	wanted_labels = {row["label"] for row in wanted}
	existing = frappe.get_all(
		child_doctype,
		filters={"parenttype": "Workspace", "parent": workspace, "parentfield": parentfield},
		fields=["name", "idx", *fields],
		order_by="idx asc",
	)
	ours = [r for r in existing if r.label in wanted_labels]
	current = [tuple((r.get(f) or None) for f in fields) for r in ours]
	target = [tuple((row.get(f) or None) for f in fields) for row in wanted]
	if current == target:
		return False
	if ours:
		frappe.db.delete(child_doctype, {"name": ["in", [r.name for r in ours]]})
	idx = max((r.idx for r in existing if r.label not in wanted_labels), default=0)
	for row in wanted:
		idx += 1
		doc = frappe.new_doc(child_doctype)
		doc.update(
			{
				**row,
				"name": frappe.generate_hash(length=10),
				"parent": workspace,
				"parenttype": "Workspace",
				"parentfield": parentfield,
				"idx": idx,
			}
		)
		doc.db_insert()
	return True


def is_billing_filter(filters_json: str | None) -> bool:
	try:
		filters = json.loads(filters_json or "[]")
	except ValueError:
		return False
	if isinstance(filters, dict):
		return bool(BILLING_FIELDS & set(filters))
	return any(isinstance(f, list | tuple) and len(f) > 1 and f[1] in BILLING_FIELDS for f in filters)


def remove_billing_rows(workspace: str) -> dict[str, set[str]]:
	"""Delete billing shortcuts, links, number cards and charts (C-5). Returns removed labels per table."""
	billing = set(BILLING_DOCTYPES)
	removed: dict[str, set[str]] = {}

	def drop(child_doctype: str, parentfield: str, rows: list) -> None:
		if rows:
			frappe.db.delete(child_doctype, {"name": ["in", [r.name for r in rows]]})
			removed[parentfield] = {r.label for r in rows}

	base = {"parenttype": "Workspace", "parent": workspace}
	shortcuts = frappe.get_all(
		"Workspace Shortcut",
		filters=base,
		fields=["name", "label", "type", "link_to", "report_ref_doctype", "stats_filter"],
	)
	drop(
		"Workspace Shortcut",
		"shortcuts",
		[
			s
			for s in shortcuts
			if (s.type == "DocType" and s.link_to in billing)
			or s.report_ref_doctype in billing
			or (s.type == "Report" and frappe.db.get_value("Report", s.link_to, "ref_doctype") in billing)
		],
	)
	links = frappe.get_all(
		"Workspace Link", filters=base, fields=["name", "label", "type", "link_type", "link_to"]
	)
	drop(
		"Workspace Link",
		"links",
		[
			lnk
			for lnk in links
			if lnk.type == "Link"
			and (
				(lnk.link_type == "DocType" and lnk.link_to in billing)
				or (
					lnk.link_type == "Report"
					and frappe.db.get_value("Report", lnk.link_to, "ref_doctype") in billing
				)
			)
		],
	)
	cards = frappe.get_all(
		"Workspace Number Card", filters=base, fields=["name", "label", "number_card_name"]
	)
	drop(
		"Workspace Number Card",
		"number_cards",
		[c for c in cards if _is_billing_source("Number Card", c.number_card_name, billing)],
	)
	charts = frappe.get_all("Workspace Chart", filters=base, fields=["name", "label", "chart_name"])
	drop(
		"Workspace Chart",
		"charts",
		[c for c in charts if _is_billing_source("Dashboard Chart", c.chart_name, billing)],
	)
	return removed


def _is_billing_source(doctype: str, name: str, billing: set[str]) -> bool:
	src = frappe.db.get_value(doctype, name, ["document_type", "filters_json"], as_dict=True)
	if not src:
		return False
	return src.document_type in billing or is_billing_filter(src.filters_json)


BLOCK_ROW_TABLE = {
	"shortcut": "shortcuts",
	"quick_list": "quick_lists",
	"number_card": "number_cards",
	"chart": "charts",
	"card": "links",
}


def _sync_content(workspace: str, our_blocks: list, removed_labels: dict[str, set[str]]) -> bool:
	raw = frappe.db.get_value("Workspace", workspace, "content") or "[]"
	try:
		content = json.loads(raw)
	except ValueError:
		content = []
	kept = []
	for block in content:
		if str(block.get("id", "")).startswith(BLOCK_PREFIX):
			continue
		table = BLOCK_ROW_TABLE.get(block.get("type"))
		label = (block.get("data") or {}).get(f"{block.get('type')}_name")
		if table and label in removed_labels.get(table, set()):
			continue
		if table and table != "links" and label and not _row_exists(workspace, table, label):
			# A Marley block whose row is gone (e.g. a billing row removed on an earlier run).
			continue
		kept.append(block)
	insert_at = 0
	while insert_at < len(kept) and kept[insert_at].get("type") == "onboarding":
		insert_at += 1
	new_content = kept[:insert_at] + our_blocks + kept[insert_at:]
	if new_content == content:
		return False
	frappe.db.set_value("Workspace", workspace, "content", json.dumps(new_content), update_modified=False)
	return True


def _row_exists(workspace: str, parentfield: str, label: str) -> bool:
	child = {
		"shortcuts": "Workspace Shortcut",
		"quick_lists": "Workspace Quick List",
		"number_cards": "Workspace Number Card",
		"charts": "Workspace Chart",
	}[parentfield]
	return bool(
		frappe.db.exists(
			child,
			{"parenttype": "Workspace", "parent": workspace, "parentfield": parentfield, "label": label},
		)
	)
