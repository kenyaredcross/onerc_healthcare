# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Base filters every MOH count applies (W0 contract; R1 may add helpers but never changes these).

Table aliases used by the SQL fragments:

- ``v``: `tabPatient Visit`
- ``pat``: `tabPatient` (joined on ``pat.name = v.patient``)
- ``enc``: `tabPatient Encounter` (joined on ``enc.krcs_visit = v.name``)

What is excluded and why:

- Visits closed as "Registered in error" or "Superseded by a new visit": not real attendances.
- Archived visits and encounters (``krcs_archived``): kept for retrieval only (NFR-9).
- Patients merged into another record (``krcs_merged_into``): merging moves the visits to the surviving
  record, so anything still on a merged record is an error, not care.
- Draft and cancelled encounters.
- Anything a ``krcs_report_exclusions`` hook excludes (reserved for migrated SmartCare history, FR-43 /
  OI-4). Each hook is a dotted path ``fn(ctx) -> str``: a SQL condition on the aliases above that keeps a
  row, or "" for no condition.

An archived *patient* is not excluded: the care was given.
"""

from __future__ import annotations

import frappe

from onerc_healthcare.constants import REPORT_EXCLUDED_CLOSURE_REASONS
from onerc_healthcare.reporting.registry import SourceContext

ARCHIVED_FIELD = "krcs_archived"
MERGED_INTO_FIELD = "krcs_merged_into"
EXCLUSIONS_HOOK = "krcs_report_exclusions"


def visit_conditions(ctx: SourceContext, by_visit_date: bool = True) -> tuple[str, dict]:
	"""WHERE fragment (joined with AND) and its parameters for ``v`` and ``pat``.

	`by_visit_date`: restrict ``v.visit_date`` to the period (attendance counts). Pass False when the
	period applies to another date, such as the encounter date.
	"""
	params = {
		"krcs_excluded_closures": list(REPORT_EXCLUDED_CLOSURE_REASONS),
		"krcs_from": ctx.from_date,
		"krcs_to": ctx.to_date,
	}
	conds = [
		"IFNULL(v.closure_reason, '') NOT IN %(krcs_excluded_closures)s",
		f"IFNULL(v.`{ARCHIVED_FIELD}`, 0) = 0",
		f"IFNULL(pat.`{MERGED_INTO_FIELD}`, '') = ''",
	]
	if by_visit_date:
		conds.append("v.visit_date BETWEEN %(krcs_from)s AND %(krcs_to)s")
	if ctx.facility:
		conds.append("v.facility = %(krcs_facility)s")
		params["krcs_facility"] = ctx.facility
	if ctx.company:
		conds.append("v.company = %(krcs_company)s")
		params["krcs_company"] = ctx.company
	for path in frappe.get_hooks(EXCLUSIONS_HOOK):
		extra = frappe.get_attr(path)(ctx)
		if extra:
			conds.append(f"({extra})")
	return " AND ".join(conds), params


def encounter_conditions(ctx: SourceContext) -> tuple[str, dict]:
	"""Visit conditions plus: submitted, not archived, encounter date in the period (``enc``)."""
	where, params = visit_conditions(ctx, by_visit_date=False)
	where += (
		f" AND enc.docstatus = 1 AND IFNULL(enc.`{ARCHIVED_FIELD}`, 0) = 0"
		" AND enc.encounter_date BETWEEN %(krcs_from)s AND %(krcs_to)s"
	)
	return where, params


VISIT_FROM = "`tabPatient Visit` v JOIN `tabPatient` pat ON pat.name = v.patient"
ENCOUNTER_FROM = (
	"`tabPatient Encounter` enc JOIN `tabPatient Visit` v ON v.name = enc.krcs_visit"
	" JOIN `tabPatient` pat ON pat.name = v.patient"
)
