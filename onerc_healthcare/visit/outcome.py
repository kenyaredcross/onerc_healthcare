# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Visit outcome and attendance helpers for the visit form, and the outcome backfill (Phase 3). Owner: R3.

The engine records the outcome on the closing actions (`engine.resolve_outcome`). This module adds:

- `get_outcome_choices` (GET): the outcomes the user may choose for an action and closure reason, for
  the Close Visit dialog (wraps `engine.outcome_choices`).
- `get_visit_capture` (GET): which capture buttons the visit form shows to the current user (attendance
  and referred-in corrections, services). Visibility only; every endpoint checks again.
- `backfill_outcomes`: the data migration for visits closed before outcomes existed (patch
  `patches/backfill_visit_outcome.py`):

  | Closed visit | Outcome |
  |---|---|
  | Discharged, closed by Close Visit with reason "Died" | Died |
  | Discharged after a `complete_consultation` (or the retired `discharge`) log row | Discharged home |
  | Discharged by Close Visit (no consultation completed) | from the reason (`engine.CLOSURE_OUTCOMES`; "Other" → Not applicable) |
  | Referred | Referred internally when its Referral is Internal, else Referred out |

  `outcome_at` = `closed_at`; `outcome_by` stays empty (nobody chose it). Rows are written directly
  (`update_modified=False`), without status-log rows, and only where the outcome is still empty, so the
  backfill is idempotent. Visits matching no row of the table are left empty and counted.
"""

from __future__ import annotations

import frappe
from frappe import _

from onerc_healthcare.constants import (
	OUTCOME_DIED,
	OUTCOME_DISCHARGED,
	OUTCOME_NOT_APPLICABLE,
	OUTCOME_REFERRED_INTERNAL,
	OUTCOME_REFERRED_OUT,
	REFERRAL_DOCTYPE,
	ROLE_CLINICIAN,
	ROLE_MHO,
	ROLE_MIC,
	VISIT_DOCTYPE,
	VISIT_STATUS_DISCHARGED,
	VISIT_STATUS_REFERRED,
)
from onerc_healthcare.visit import attendance, engine

# Roles shown the Services section of a visit (Service Delivered is never part of the visit record).
SERVICE_ROLES = (ROLE_CLINICIAN, ROLE_MHO, ROLE_MIC)
SERVICE_CREATE_ROLES = (ROLE_CLINICIAN, ROLE_MHO)


def _readable_visit(visit: str):
	if not frappe.db.exists(VISIT_DOCTYPE, visit):
		frappe.throw(_("Patient Visit {0} does not exist.").format(visit), frappe.DoesNotExistError)
	frappe.has_permission(VISIT_DOCTYPE, "read", doc=visit, throw=True)
	return frappe.get_doc(VISIT_DOCTYPE, visit)


@frappe.whitelist(methods=["GET"])
def get_outcome_choices(visit: str, action: str, closure_reason: str | None = None) -> dict:
	"""Outcomes the current user may choose for `action` on `visit` (Close Visit / Complete Consultation).

	Returns ``{"choices": [...], "default": ..., "current": ..., "keeps_current": bool}``. With one
	choice the outcome is implied (e.g. "Left before being seen" → Left without completing).
	``keeps_current``: an outcome recorded by Complete Consultation stays unless one is chosen."""
	doc = _readable_visit(visit)
	closure_reason = (closure_reason or "").strip() or None
	choices = engine.outcome_choices(doc, action, closure_reason)
	implied = engine.CLOSURE_OUTCOMES.get(closure_reason) if action == engine.ACTION_CLOSE_VISIT else None
	keeps = bool(action == engine.ACTION_CLOSE_VISIT and doc.get("outcome") and implied != OUTCOME_DIED)
	if action == engine.ACTION_CLOSE_VISIT:
		default = implied or (None if keeps else OUTCOME_NOT_APPLICABLE)
	else:
		default = choices[0] if choices else None
	return {"choices": choices, "default": default, "current": doc.get("outcome"), "keeps_current": keeps}


@frappe.whitelist(methods=["GET"])
def get_visit_capture(visit: str) -> dict:
	"""What the visit form offers the current user: corrections and the Services section."""
	doc = _readable_visit(visit)
	user = frappe.session.user
	roles = set(frappe.get_roles(user))
	is_admin = user == "Administrator"
	return {
		"can_correct_attendance": attendance.can_correct(attendance.ACTION_CORRECT_ATTENDANCE, doc.status),
		"can_correct_referred_in": attendance.can_correct(attendance.ACTION_CORRECT_REFERRED_IN, doc.status),
		"can_view_services": is_admin or bool(roles & set(SERVICE_ROLES)),
		"can_record_services": is_admin or bool(roles & set(SERVICE_CREATE_ROLES)),
	}


# --------------------------------------------------------------------------- backfill


def backfill_outcome_for(visit: frappe._dict) -> str | None:
	"""The outcome a closed visit without one gets (module table), or None when nothing applies."""
	actions = set(
		frappe.get_all(
			"Patient Visit Status Log",
			filters={"parent": visit.name, "parenttype": VISIT_DOCTYPE},
			pluck="action",
		)
	)
	if visit.status == VISIT_STATUS_REFERRED:
		types = frappe.get_all(
			REFERRAL_DOCTYPE,
			filters={"source_visit": visit.name, "status": ["not in", ["Draft", "Cancelled"]]},
			pluck="referral_type",
		)
		return OUTCOME_REFERRED_INTERNAL if "Internal" in types else OUTCOME_REFERRED_OUT
	if visit.status != VISIT_STATUS_DISCHARGED:
		return None
	closed_by_close = engine.ACTION_CLOSE_VISIT in actions
	if closed_by_close and visit.closure_reason == "Died":
		return OUTCOME_DIED
	if actions & {engine.ACTION_COMPLETE_CONSULTATION, engine.ACTION_DISCHARGE}:
		return OUTCOME_DISCHARGED
	if closed_by_close:
		return engine.CLOSURE_OUTCOMES.get(visit.closure_reason, OUTCOME_NOT_APPLICABLE)
	return None


def backfill_outcomes() -> dict:
	"""Set the outcome of closed visits that have none (module docstring). Returns counts."""
	rows = frappe.get_all(
		VISIT_DOCTYPE,
		filters={
			"status": ["in", [VISIT_STATUS_DISCHARGED, VISIT_STATUS_REFERRED]],
			"outcome": ["is", "not set"],
		},
		fields=["name", "status", "closure_reason", "closed_at"],
		order_by="creation asc",
	)
	counts = {"updated": 0, "skipped": 0}
	for row in rows:
		outcome = backfill_outcome_for(row)
		if not outcome:
			counts["skipped"] += 1
			continue
		frappe.db.set_value(
			VISIT_DOCTYPE,
			row.name,
			{"outcome": outcome, "outcome_at": row.closed_at, "outcome_by": None},
			update_modified=False,
		)
		counts["updated"] += 1
	return counts
