# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""ANC Pregnancy and ANC Contact rules (MCH-A). W0 stubs: the signatures are the contract
(docs/MCH_CONTRACT.md §5); MCH-A implements the bodies."""

from __future__ import annotations

import frappe

from onerc_healthcare.mch import common


# ANC Pregnancy controller
def pregnancy_validate(doc) -> None:
	common.set_recorded_by(doc)


def pregnancy_on_update(doc) -> None:
	pass


# ANC Contact controller
def contact_validate(doc) -> None:
	if common.guard_draft(doc):
		return
	common.check_visit(doc)
	common.guard_one_per_visit(doc)
	common.set_recorded_by(doc)


def contact_before_submit(doc) -> None:
	common.before_submit(doc)


def contact_before_cancel(doc) -> None:
	common.refuse_cancel(doc)


def contact_before_update_after_submit(doc) -> None:
	common.guard_after_submit(doc)


def open_pregnancy(patient: str, for_update: bool = False) -> str | None:
	"""Name of the patient's open, non-archived ANC Pregnancy, or None."""
	return None


def close_on_delivery(doc, method=None) -> None:
	"""Delivery Record on_submit (hooks.py doc_events): close the mother's open pregnancy as Delivered."""


@frappe.whitelist(methods=["POST"])
def close_pregnancy(pregnancy: str, reason: str, note: str | None = None) -> dict:
	"""Close an open pregnancy as Pregnancy ended or Transferred out (MCH_ROLES)."""
	raise NotImplementedError


@frappe.whitelist()
def admit_prefill(patient: str) -> dict:
	"""LMP, EDD basis, EDD, gravida, para and blood group/rhesus of the open pregnancy, for the Maternity admit
	dialog ({} when none or the user cannot read it)."""
	return {}
