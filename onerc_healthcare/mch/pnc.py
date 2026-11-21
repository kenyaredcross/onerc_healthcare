# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""PNC Visit rules (MCH-A). W0 stubs: the signatures are the contract (docs/MCH_CONTRACT.md §6)."""

from __future__ import annotations

from onerc_healthcare.mch import common


def visit_validate(doc) -> None:
	if common.guard_draft(doc):
		return
	common.check_visit(doc, required=not doc.get("maternity_admission"))
	common.guard_one_per_visit(doc, ("visit", "subject", "patient"))
	common.set_recorded_by(doc)


def visit_before_submit(doc) -> None:
	common.before_submit(doc)


def visit_before_cancel(doc) -> None:
	common.refuse_cancel(doc)


def visit_before_update_after_submit(doc) -> None:
	common.guard_after_submit(doc)
