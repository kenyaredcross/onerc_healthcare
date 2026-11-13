# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Lab results inside the consultation record (FR-18). B1's encounter form renders this.

Result values are released only once approved (single-step approval by the Laboratory Technician,
provisional until OI-9); until then the test shows its progress label only.
"""

from __future__ import annotations

import frappe
from frappe.utils import get_datetime

from onerc_healthcare.constants import ENCOUNTER_DOCTYPE
from onerc_healthcare.lab.tasks import EncounterLab


@frappe.whitelist(methods=["GET"])
def get_encounter_results(encounter: str) -> list[dict]:
	"""Each lab test of the encounter with its results, units, flags, approval status and times."""
	frappe.has_permission(ENCOUNTER_DOCTYPE, "read", doc=encounter, throw=True)
	lab = EncounterLab(encounter)
	out = []
	for request in lab.requests:
		sample_label, sample_collection = lab.sample_status(request.name, request.template_dn)
		result_label, root = lab.result_status(request.name, request.template_dn)
		results = _results(root) if root else []
		approved = lab.is_done(request.name)
		approval_times = [r["time_of_approval"] for r in results if r.get("time_of_approval")]
		out.append(
			{
				"service_request": request.name,
				"test": request.template_dn,
				"ordered_at": _ordered_at(request),
				"sample_status": sample_label,
				"sample_collection": sample_collection,
				"status": result_label,
				"approved": approved,
				"approved_at": max(approval_times) if approved and approval_times else None,
				"observation": root,
				"results": results,
			}
		)
	return out


def _ordered_at(request) -> str | None:
	if request.order_date:
		return str(get_datetime(f"{request.order_date} {request.order_time or '00:00:00'}"))
	return str(request.creation) if request.creation else None


def _results(root: str) -> list[dict]:
	"""Result rows of a root Observation (a panel is flattened into its components, in template order)."""
	from healthcare.healthcare.doctype.observation.observation import (
		ensure_result_flag,
		get_child_observations,
	)

	obs = frappe.get_all("Observation", filters={"name": root}, fields=["*"])
	if not obs:
		return []
	rows, stack = [], [(obs[0], None)]
	while stack:
		row, panel = stack.pop(0)
		if row.get("has_component"):
			children = get_child_observations(row)
			stack[0:0] = [(c, row.get("observation_template")) for c in children]
			continue
		rows.append(_result_row(ensure_result_flag(row), panel))
	return rows


def _result_row(obs: dict, panel: str | None) -> dict:
	approved = obs.get("docstatus") == 1 and obs.get("status") == "Approved"
	return {
		"observation": obs.get("name"),
		"test": obs.get("preferred_display_name") or obs.get("observation_template"),
		"panel": panel,
		"result": obs.get("result_display") if approved else None,
		"unit": obs.get("permitted_unit"),
		"reference": obs.get("reference"),
		"flag": obs.get("result_flag") if approved else None,
		"flag_color": obs.get("result_flag_color") if approved else None,
		"interpretation": obs.get("result_interpretation") if approved else None,
		"note": obs.get("note") if approved else None,
		"status": obs.get("status"),
		"approved": approved,
		"time_of_result": obs.get("time_of_result"),
		# Marley fills time_of_approval only for status "Final"; an approval is the submit itself.
		"time_of_approval": (obs.get("time_of_approval") or obs.get("modified")) if approved else None,
	}
