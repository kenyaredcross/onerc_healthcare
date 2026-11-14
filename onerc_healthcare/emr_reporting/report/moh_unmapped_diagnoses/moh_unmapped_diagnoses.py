# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MOH Unmapped Diagnoses (R2; FR-29..31): ICD-11 codes used as New diagnoses in the period that have no
MOH mapping, so they fell to the catch-all ("All other diseases"), with their counts. No patient data:
codes and counts only. The HIS Officer maps selected codes from here (`mapping.map_codes`, POST).
Roles: HIS Officer, Medical In-Charge (report JSON, checked again here)."""

import frappe
from frappe import _
from frappe.utils import get_first_day, get_last_day, getdate, today

from onerc_healthcare.constants import REPORT_MOH_UNMAPPED
from onerc_healthcare.reporting import diagnosis, mapping
from onerc_healthcare.reporting.registry import SourceContext


def execute(filters=None):
	filters = frappe._dict(filters or {})
	mapping._require_roles(mapping.TEST_ROLES)
	ctx = context(filters, REPORT_MOH_UNMAPPED)
	rows = [r for r in diagnosis.code_usage(ctx) if r.unmapped]
	catch_all = frappe.db.get_value("MOH Disease Category", {"is_catch_all": 1}, "title")
	data = [
		{
			"code": r.code,
			"code_value": r.code_value or r.code,
			"title": r.display,
			"chapter": r.krcs_chapter,
			"encounters": r.encounters,
			"diagnoses": r.diagnoses,
			"counted_under": r.category or _("Not counted (no catch-all)"),
		}
		for r in rows
	]
	message = _("PROVISIONAL — HIS Officer to confirm (OI-5). Counted under {0} until mapped.").format(
		catch_all or _("no category")
	)
	return columns(), data, message


def context(filters, report: str) -> SourceContext:
	from_date = getdate(filters.get("from_date") or get_first_day(today()))
	to_date = getdate(filters.get("to_date") or get_last_day(today()))
	if from_date > to_date:
		frappe.throw(_("From Date must be on or before To Date."))
	return SourceContext(
		report=report, from_date=from_date, to_date=to_date, facility=filters.get("facility")
	)


def columns() -> list[dict]:
	return [
		{"fieldname": "code", "label": _("Code"), "fieldtype": "Link", "options": "Code Value", "width": 150},
		{"fieldname": "code_value", "label": _("ICD-11"), "fieldtype": "Data", "width": 90},
		{"fieldname": "title", "label": _("Title"), "fieldtype": "Data", "width": 320},
		{"fieldname": "chapter", "label": _("Chapter"), "fieldtype": "Data", "width": 70},
		{"fieldname": "encounters", "label": _("Encounters"), "fieldtype": "Int", "width": 100},
		{"fieldname": "diagnoses", "label": _("Diagnoses"), "fieldtype": "Int", "width": 100},
		{"fieldname": "counted_under", "label": _("Counted Under"), "fieldtype": "Data", "width": 160},
	]
