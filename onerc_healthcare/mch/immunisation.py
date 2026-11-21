# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Immunisation, the KEPI schedule and the Due Immunisations list (MCH-B). W0 stubs: the signatures are the
contract (docs/MCH_CONTRACT.md §7)."""

from __future__ import annotations

import frappe

from onerc_healthcare.mch import common


# KEPI Vaccine Schedule controller
def schedule_row_validate(doc) -> None:
	pass


# Immunisation controller
def immunisation_validate(doc) -> None:
	if common.guard_draft(doc):
		return
	common.check_visit(doc)
	common.guard_one_per_visit(doc)
	common.set_recorded_by(doc)


def immunisation_before_submit(doc) -> None:
	common.before_submit(doc)


def immunisation_before_cancel(doc) -> None:
	common.refuse_cancel(doc)


def immunisation_before_update_after_submit(doc) -> None:
	common.guard_after_submit(doc)


def seed_schedule() -> None:
	"""Insert the KEPI rows of mch/data/kepi_schedule.json that do not exist yet. Never edits a row."""


@frappe.whitelist()
def schedule(patient: str, on: str | None = None) -> list[dict]:
	"""The child's schedule on date `on` (default today): one dict per active KEPI dose
	{vaccine, antigen, dose_number, due_date, latest_date, status (SCHEDULE_*), given_on, immunisation}."""
	return []
