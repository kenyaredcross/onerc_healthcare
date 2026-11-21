# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Growth Monitoring and WHO z-scores (MCH-B). W0 stubs: the signatures are the contract
(docs/MCH_CONTRACT.md §8)."""

from __future__ import annotations

import frappe

from onerc_healthcare.mch import common


def growth_validate(doc) -> None:
	if common.guard_draft(doc):
		return
	common.check_visit(doc)
	common.guard_one_per_visit(doc)
	common.set_recorded_by(doc)


def growth_before_submit(doc) -> None:
	common.before_submit(doc)


def growth_before_cancel(doc) -> None:
	common.refuse_cancel(doc)


def growth_before_update_after_submit(doc) -> None:
	common.guard_after_submit(doc)


def zscore(indicator: str, sex: str, x: float, measure: float) -> float | None:
	"""WHO LMS z-score. `indicator` is one of "wfa", "lhfa", "wfl", "wfh"; `x` is the age in days (wfa,
	lhfa) or the length/height in cm (wfl, wfh); `measure` is the weight (kg) or length/height (cm).
	None outside the table."""
	return None


@frappe.whitelist()
def chart_data(patient: str) -> dict:
	"""Submitted measurements of the child plus the WHO reference curves (-3, -2, 0, +2, +3) for the
	weight-for-age chart on the Patient form."""
	return {}
