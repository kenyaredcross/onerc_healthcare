# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MOH Mapping Coverage (R2; FR-29..31): how the provisional ICD-11 → MOH 705 mapping covers the codes in
use. View "Categories": every MOH Disease Category with its mapping counts and the period's usage. View
"Codes used": every code used as a New diagnosis in the period with the category and the rule that
matched it (Exact code / Prefix / Block / Catch-all). Usage is before laboratory confirmation and episode
de-duplication. Codes and counts only, no patient data. Roles: HIS Officer, Medical In-Charge."""

from collections import defaultdict

import frappe
from frappe import _

from onerc_healthcare.constants import MOH_CATEGORY_DOCTYPE, MOH_MAPPING_DOCTYPE, REPORT_MOH_COVERAGE
from onerc_healthcare.emr_reporting.report.moh_unmapped_diagnoses.moh_unmapped_diagnoses import context
from onerc_healthcare.reporting import diagnosis, mapping

VIEW_CATEGORIES = "Categories"
VIEW_CODES = "Codes used"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	mapping._require_roles(mapping.TEST_ROLES)
	usage = diagnosis.code_usage(context(filters, REPORT_MOH_COVERAGE))
	summary = _summary(usage)
	message = _("PROVISIONAL — HIS Officer to confirm (OI-5). Mapping fingerprint {0}.").format(
		mapping.fingerprint()
	)
	if filters.get("view") == VIEW_CODES:
		return _code_columns(), _code_rows(usage), message, None, summary
	return _category_columns(), _category_rows(usage), message, None, summary


def _summary(usage: list) -> list[dict]:
	by_match: dict[str, int] = defaultdict(int)
	for r in usage:
		by_match[r.matched_by] += r.diagnoses
	total = sum(by_match.values())
	out = [{"value": len(usage), "label": _("Codes used"), "datatype": "Int"}]
	for match in (*mapping.MATCH_TYPES, mapping.MATCHED_CATCH_ALL):
		out.append(
			{
				"value": by_match.get(match, 0),
				"label": _("Diagnoses by {0}").format(_(match)),
				"datatype": "Int",
			}
		)
	if total:
		mapped = total - by_match.get(mapping.MATCHED_CATCH_ALL, 0) - by_match.get(mapping.MATCHED_NONE, 0)
		out.append({"value": round(100.0 * mapped / total, 1), "label": _("Mapped %"), "datatype": "Percent"})
	return out


def _category_rows(usage: list) -> list[dict]:
	maps: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
	for m in frappe.get_all(MOH_MAPPING_DOCTYPE, fields=["category", "match_type", "enabled"]):
		maps[m.category][m.match_type if m.enabled else "disabled"] += 1
	used: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
	for r in usage:
		if r.category:
			used[r.category]["codes"] += 1
			used[r.category]["diagnoses"] += r.diagnoses
			used[r.category]["encounters"] += r.encounters
	rows = []
	for c in frappe.get_all(MOH_CATEGORY_DOCTYPE, fields=["*"], order_by="sort_order asc, name asc"):
		m, u = maps[c.name], used[c.name]
		rows.append(
			{
				"category": c.name,
				"title": c.title,
				"form": c.form,
				"group": c.category_group,
				"priority": c.priority,
				"flags": ", ".join(
					label
					for flag, label in (
						(c.is_catch_all, _("catch-all")),
						(c.excluded, _("excluded")),
						(c.requires_lab_confirmation, _("lab confirmed")),
						(c.seed_marker == mapping.SEED_EDITED, _("edited")),
					)
					if flag
				),
				"exact": m[mapping.MATCH_EXACT],
				"prefix": m[mapping.MATCH_PREFIX],
				"block": m[mapping.MATCH_BLOCK],
				"disabled": m["disabled"],
				"codes": u["codes"],
				"diagnoses": u["diagnoses"],
				"encounters": u["encounters"],
			}
		)
	return rows


def _code_rows(usage: list) -> list[dict]:
	return [
		{
			"code": r.code,
			"code_value": r.code_value or r.code,
			"title": r.display,
			"category": r.category,
			"matched_by": r.matched_by,
			"value": r.value,
			"diagnoses": r.diagnoses,
			"encounters": r.encounters,
		}
		for r in usage
	]


def _category_columns() -> list[dict]:
	return [
		{
			"fieldname": "category",
			"label": _("Category"),
			"fieldtype": "Link",
			"options": MOH_CATEGORY_DOCTYPE,
			"width": 200,
		},
		{"fieldname": "title", "label": _("Title"), "fieldtype": "Data", "width": 220},
		{"fieldname": "form", "label": _("Form"), "fieldtype": "Data", "width": 70},
		{"fieldname": "group", "label": _("Group"), "fieldtype": "Data", "width": 130},
		{"fieldname": "priority", "label": _("Priority"), "fieldtype": "Int", "width": 70},
		{"fieldname": "flags", "label": _("Flags"), "fieldtype": "Data", "width": 140},
		{"fieldname": "exact", "label": _("Exact"), "fieldtype": "Int", "width": 70},
		{"fieldname": "prefix", "label": _("Prefix"), "fieldtype": "Int", "width": 70},
		{"fieldname": "block", "label": _("Block"), "fieldtype": "Int", "width": 70},
		{"fieldname": "disabled", "label": _("Disabled"), "fieldtype": "Int", "width": 80},
		{"fieldname": "codes", "label": _("Codes Used"), "fieldtype": "Int", "width": 90},
		{"fieldname": "diagnoses", "label": _("Diagnoses"), "fieldtype": "Int", "width": 90},
		{"fieldname": "encounters", "label": _("Encounters"), "fieldtype": "Int", "width": 90},
	]


def _code_columns() -> list[dict]:
	return [
		{"fieldname": "code", "label": _("Code"), "fieldtype": "Link", "options": "Code Value", "width": 150},
		{"fieldname": "code_value", "label": _("ICD-11"), "fieldtype": "Data", "width": 90},
		{"fieldname": "title", "label": _("Title"), "fieldtype": "Data", "width": 280},
		{
			"fieldname": "category",
			"label": _("Category"),
			"fieldtype": "Link",
			"options": MOH_CATEGORY_DOCTYPE,
			"width": 180,
		},
		{"fieldname": "matched_by", "label": _("Matched By"), "fieldtype": "Data", "width": 100},
		{"fieldname": "value", "label": _("Mapping Value"), "fieldtype": "Data", "width": 120},
		{"fieldname": "diagnoses", "label": _("Diagnoses"), "fieldtype": "Int", "width": 90},
		{"fieldname": "encounters", "label": _("Encounters"), "fieldtype": "Int", "width": 90},
	]
