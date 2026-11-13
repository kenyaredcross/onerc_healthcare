# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Visit events raised by clinical, laboratory and pharmacy code (Phase 2 W0 contract).

Builders never call `engine.transition` directly for system events. They call `raise_event`, which
applies the event only when it is legal now, and otherwise records a Comment on the visit and returns
False. A lab result or a dispensing record is therefore never rolled back because the visit has been
closed in the meantime.

Extension points (dotted paths; builders report theirs and the integrator appends them here, since
this file belongs to A2):

- ``VISIT_CLOSED_HANDLERS``: called in order with the Patient Visit document after Close Visit
  (for example to revoke unsent orders). Signature ``handler(visit_doc) -> None``.
- ``TASK_CHECKERS``: ``{"lab": path, "pharmacy": path}``; each is ``checker(visit: str) -> bool`` and
  returns True when that task is complete. `reconcile_open_tasks` (scheduler) and `recheck_tasks`
  (Medical In-Charge button) then raise ``lab_results_ready`` / ``dispensed``.
"""

from __future__ import annotations

import frappe
from frappe import _

from onerc_healthcare.constants import (
	ENCOUNTER_DOCTYPE,
	ENCOUNTER_VISIT_FIELD,
	ROLE_MIC,
	VISIT_DOCTYPE,
	VISIT_ENCOUNTER_FIELD,
	VISIT_OPEN_STATUSES,
	VISIT_STATUS_REFERRED,
)
from onerc_healthcare.visit import engine

VISIT_CLOSED_HANDLERS: list[str] = [
	"onerc_healthcare.clinical.consultation.on_visit_closed",  # B1: revoke unsent lab orders
]
TASK_CHECKERS: dict[str, str] = {
	"lab": "onerc_healthcare.lab.tasks.is_lab_complete",  # B2
	"pharmacy": "onerc_healthcare.pharmacy.tasks.is_pharmacy_complete",  # B3
}

# task key -> (visit flag, event raised when the checker reports the task complete)
TASK_EVENTS = {
	"lab": ("lab_open", engine.ACTION_LAB_RESULTS_READY),
	"pharmacy": ("pharmacy_open", engine.ACTION_DISPENSED),
}
RECONCILED_COMMENT = "Reconciled"


def on_visit_closed(visit_doc) -> None:
	"""Run the registered Close Visit handlers in order."""
	for path in VISIT_CLOSED_HANDLERS:
		frappe.get_attr(path)(visit_doc)


def blocked_reason(doc, action: str) -> str | None:
	"""Why `action` cannot be applied to `doc` now (status or guard), or None when it can."""
	rule = engine.TRANSITIONS[action]
	if doc.status not in rule.from_statuses:
		return _("visit is {0}").format(_(doc.status))
	if rule.guard:
		failure = rule.guard(doc)
		if failure:
			return str(failure)
	return None


def raise_event(visit: str, action: str, comment: str | None = None, **kw) -> bool:
	"""Apply system event `action` to `visit` if it is legal now; otherwise log a Comment and return False.

	Raises only for programming errors: an unknown action or a missing visit.
	"""
	if action not in engine.TRANSITIONS:
		raise ValueError(f"Unknown visit action: {action}")
	doc = frappe.get_doc(VISIT_DOCTYPE, visit, for_update=True)  # DoesNotExistError if missing
	why = blocked_reason(doc, action)
	if why:
		doc.add_comment("Comment", _("Skipped {0}: {1}").format(action, why))
		return False
	engine.transition(visit, action, system=True, comment=comment, **kw)
	return True


def visit_for_encounter(encounter: str) -> str | None:
	"""The Patient Visit of a Patient Encounter (B1's `krcs_visit` field, else the visit's link)."""
	if frappe.get_meta(ENCOUNTER_DOCTYPE).has_field(ENCOUNTER_VISIT_FIELD):
		visit = frappe.db.get_value(ENCOUNTER_DOCTYPE, encounter, ENCOUNTER_VISIT_FIELD)
		if visit:
			return visit
	return frappe.db.get_value(VISIT_DOCTYPE, {VISIT_ENCOUNTER_FIELD: encounter}, "name")


def reconcile_visit(visit: str) -> list[str]:
	"""Ask the registered checkers about the visit's open tasks; raise the events that are due."""
	raised = []
	flags = frappe.db.get_value(VISIT_DOCTYPE, visit, ["lab_open", "pharmacy_open"], as_dict=True)
	if not flags:
		return raised
	for key, (flag, action) in TASK_EVENTS.items():
		path = TASK_CHECKERS.get(key)
		if not path or not flags.get(flag):
			continue
		if frappe.get_attr(path)(visit) and raise_event(visit, action, comment=RECONCILED_COMMENT):
			raised.append(action)
	return raised


def reconcile_open_tasks() -> int:
	"""Scheduler job: close lab/pharmacy tasks the registered checkers report as complete.

	Each visit runs under its own savepoint, so one failure does not stop the others.
	"""
	if not TASK_CHECKERS:
		return 0
	visits = frappe.get_all(
		VISIT_DOCTYPE,
		filters={"status": ["in", [*VISIT_OPEN_STATUSES, VISIT_STATUS_REFERRED]]},
		or_filters={"lab_open": 1, "pharmacy_open": 1},
		pluck="name",
	)
	count = 0
	for visit in visits:
		frappe.db.savepoint("krcs_reconcile")
		try:
			count += len(reconcile_visit(visit))
		except Exception:
			frappe.db.rollback(save_point="krcs_reconcile")
			frappe.log_error(title=f"Visit task reconciliation failed: {visit}")
	return count


@frappe.whitelist(methods=["POST"])
def recheck_tasks(visit: str) -> dict:
	"""Medical In-Charge: re-check a visit's open lab/pharmacy tasks now."""
	if frappe.session.user != "Administrator" and ROLE_MIC not in frappe.get_roles():
		frappe.throw(_("Only the Medical In-Charge can re-check visit tasks."), frappe.PermissionError)
	frappe.has_permission(VISIT_DOCTYPE, "read", doc=visit, throw=True)
	raised = reconcile_visit(visit)
	doc = frappe.db.get_value(VISIT_DOCTYPE, visit, ["status", "lab_open", "pharmacy_open"], as_dict=True)
	return {"raised": raised, **doc}
