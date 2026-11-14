# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Read-only aggregate interface for MOH reports (SDD §5.8 "Open interface for DHIS2").

GET ``/api/method/onerc_healthcare.reporting.api.<method>``:

| Method              | Returns                                                                  |
|---------------------|--------------------------------------------------------------------------|
| `list_reports`      | definitions: code, title, status, version, period type, element counts   |
| `list_runs`         | Generated and Submitted runs, filtered by report, facility and period    |
| `get_report_values` | one run's values, from its stored snapshot only (Generated or Submitted) |

- **Roles:** HIS Officer, Medical In-Charge and MOH Data Reader (API-only role, `ROLE_MOH_API`); also
  Administrator. Everyone else is refused, including the Head of Operations, who reads runs in the desk
  but not through this interface.
- Values are aggregates: no patient data, no exceptions. Element codes and ``dhis2_uid`` let a future
  DHIS2 connector map values without a structural change.
- Every call is rate-limited per user and written to the Access Log Entry (reference MOH Report Run).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, getdate

from onerc_healthcare.constants import (
	MOH_REPORT_DOCTYPE,
	MOH_RUN_DOCTYPE,
	ROLE_HIS,
	ROLE_MIC,
	ROLE_MOH_API,
)
from onerc_healthcare.privacy.access_log import ACTION_LIST, ACTION_READ_API, log_access
from onerc_healthcare.reporting import engine
from onerc_healthcare.reporting.registry import AVAILABILITY_GAP
from onerc_healthcare.reporting.run import READABLE_VALUE_STATUSES, has_any_role

API_ROLES = (ROLE_HIS, ROLE_MIC, ROLE_MOH_API)
RATE_LIMIT = 120  # calls per user per minute, per method
RATE_WINDOW = 60
MAX_RUNS = 500


def check_api_access() -> None:
	if not has_any_role(API_ROLES):
		frappe.throw(
			_("The MOH reporting interface is open to {0} only.").format(", ".join(_(r) for r in API_ROLES)),
			frappe.PermissionError,
		)


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=RATE_LIMIT, seconds=RATE_WINDOW, user_based=True)
def list_reports() -> list[dict]:
	"""Every MOH report definition, without its rules."""
	check_api_access()
	reports = frappe.get_all(
		MOH_REPORT_DOCTYPE,
		fields=["name", "title", "status", "version", "period_type", "note", "blocked_reason"],
		order_by="name asc",
	)
	counts = {}
	for row in frappe.get_all(
		"MOH Report Data Element",
		filters={"parenttype": MOH_REPORT_DOCTYPE},
		fields=["parent", "availability"],
	):
		c = counts.setdefault(row.parent, {"elements": 0, "gaps": 0})
		c["elements"] += 1
		c["gaps"] += 1 if row.availability == AVAILABILITY_GAP else 0
	out = [{**r, **counts.get(r.name, {"elements": 0, "gaps": 0})} for r in reports]
	log_access(ACTION_LIST, MOH_RUN_DOCTYPE, None, detail=f"MOH API list_reports: {len(out)} reports")
	return out


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=RATE_LIMIT, seconds=RATE_WINDOW, user_based=True)
def list_runs(
	report: str | None = None,
	facility: str | None = None,
	from_date: str | None = None,
	to_date: str | None = None,
) -> list[dict]:
	"""Generated and Submitted runs; `from_date` / `to_date` keep runs whose period lies inside them."""
	check_api_access()
	filters: dict = {"status": ["in", READABLE_VALUE_STATUSES]}
	if report:
		filters["report"] = report
	if facility:
		filters["facility"] = facility
	if from_date:
		filters["from_date"] = [">=", getdate(from_date)]
	if to_date:
		filters["to_date"] = ["<=", getdate(to_date)]
	runs = frappe.get_all(
		MOH_RUN_DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"report",
			"facility",
			"from_date",
			"to_date",
			"period_type",
			"period_label",
			"status",
			"report_version",
			"report_status",
			"generated_at",
			"submitted_at",
			"supersedes",
		],
		order_by="from_date desc, report asc",
		limit=MAX_RUNS,
	)
	log_access(
		ACTION_LIST,
		MOH_RUN_DOCTYPE,
		None,
		detail=f"MOH API list_runs: filters {sorted(k for k in filters if k != 'status')}, {len(runs)} runs",
	)
	return runs


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=RATE_LIMIT, seconds=RATE_WINDOW, user_based=True)
def get_report_values(run: str) -> dict:
	"""Values of one Generated or Submitted run, from its snapshot.

	Each element lists ``values`` as ``{"band", "sex", "value"}``; a blank band or sex is a total over
	that dimension, so ``{"band": "", "sex": ""}`` is the element total. Gap and not-computed elements
	have no values and say why in ``note``.
	"""
	check_api_access()
	row = frappe.db.get_value(
		MOH_RUN_DOCTYPE,
		run,
		["name", "status", "report", "facility", "submitted_at", "supersedes", "snapshot"],
		as_dict=True,
	)
	if not row or row.status not in READABLE_VALUE_STATUSES or not row.snapshot:
		frappe.throw(
			_("Run {0} has no values to read: only Generated or Submitted runs are served.").format(run),
			frappe.DoesNotExistError,
		)
	snapshot = engine.parse_json(row.snapshot)
	cells = engine.cells_of(snapshot)
	elements = []
	for el in snapshot.get("elements") or []:
		blank = el.get("availability") == AVAILABILITY_GAP or bool(el.get("unavailable"))
		values = (
			[]
			if blank
			else [
				{"band": band, "sex": sex, "value": cint(value)}
				for (code, band, sex), value in sorted(cells.items())
				if code == el["code"]
			]
		)
		if not blank and not values:
			values = [{"band": "", "sex": "", "value": 0}]
		elements.append(
			{
				"code": el["code"],
				"label": el["label"],
				"section": el.get("section"),
				"disaggregation": el.get("disaggregation"),
				"availability": el.get("availability"),
				"dhis2_uid": el.get("dhis2_uid") or None,
				"note": engine.element_note(el) or None,
				"values": values,
			}
		)
	log_access(ACTION_READ_API, MOH_RUN_DOCTYPE, row.name, detail="MOH API get_report_values")
	return {
		"run": row.name,
		"run_status": row.status,
		"supersedes": row.supersedes,
		"submitted_at": row.submitted_at,
		"report": snapshot.get("report"),
		"provisional": engine.is_provisional(snapshot),
		"facility": snapshot.get("facility"),
		"period": snapshot.get("period"),
		"age_bands": snapshot.get("age_bands"),
		"generated_at": snapshot.get("generated_at"),
		"fingerprints": snapshot.get("fingerprints"),
		"elements": elements,
	}
