# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Prescription line (Medication Request) dispensing state, FR-21, FR-22, BR-6, SDD §5.5.

A Medication Request is one prescription line. The Pharmacist records Dispensing Records against it
(partial dispensing is allowed). The line state lives in two custom fields on the Medication Request,
`krcs_dispense_status` and `krcs_qty_dispensed`, and is **recomputed** from the submitted Dispensing
Records on every submit and cancel (never edited by hand). It is written with `db.set_value`, together
with Marley's `status` Code Value, so the Medication Request itself is never saved.

Line states (`LINE_STATES`), computed by `compute_line_state(total, dispensed, final)`:

| State            | Condition                                         | Open | Marley status          |
|------------------|---------------------------------------------------|------|------------------------|
| Pending          | nothing dispensed, no final record                | yes  | active                 |
| Partly Dispensed | 0 < dispensed < total, no final record            | yes  | active                 |
| Dispensed        | dispensed >= total (quantity reached)             | no   | completed              |
| Not Dispensed    | a final record and nothing dispensed              | no   | stopped                |
| Closed           | a final record after a partial quantity           | no   | completed              |

`total` is the Medication Request's `total_dispensable_quantity` (quantity plus repeats). A "Not
Dispensed" record is always final. Over-dispensing is refused, so `dispensed` never exceeds `total`.

Events (`LINE_EVENTS`; S1 can drive every row): submitting a Dispensing Record and cancelling one. An open
line accepts new records; a resolved line (Dispensed / Not Dispensed / Closed) accepts none until a
record is cancelled, which re-opens it.

Visit: when every submitted Medication Request of the encounter is resolved, `tasks.raise_event(visit,
"dispensed")` closes the pharmacy task. Cancelling a record (allowed only while the visit is open)
re-opens the task with `prescribe` when the visit had no open pharmacy task left.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import flt

from onerc_healthcare.constants import DISPENSING_DOCTYPE, VISIT_CLOSED_STATUSES, VISIT_DOCTYPE

MR_DOCTYPE = "Medication Request"
STATUS_FIELD = "krcs_dispense_status"
QTY_FIELD = "krcs_qty_dispensed"

LINE_PENDING = "Pending"
LINE_PARTLY = "Partly Dispensed"
LINE_DISPENSED = "Dispensed"
LINE_NOT_DISPENSED = "Not Dispensed"
LINE_CLOSED = "Closed"

OUTCOME_DISPENSED = "Dispensed"
OUTCOME_NOT_DISPENSED = "Not Dispensed"

MR_STATUS_ACTIVE = "active-Medication Request Status"
MR_STATUS_COMPLETED = "completed-Medication Request Status"
MR_STATUS_STOPPED = "stopped-Medication Request Status"


@dataclass(frozen=True)
class LineState:
	label: str
	is_open: bool
	marley_status: str
	condition: str


LINE_STATES: dict[str, LineState] = {
	LINE_PENDING: LineState(LINE_PENDING, True, MR_STATUS_ACTIVE, "nothing dispensed, no final record"),
	LINE_PARTLY: LineState(LINE_PARTLY, True, MR_STATUS_ACTIVE, "0 < dispensed < total, no final record"),
	LINE_DISPENSED: LineState(LINE_DISPENSED, False, MR_STATUS_COMPLETED, "dispensed >= total"),
	LINE_NOT_DISPENSED: LineState(LINE_NOT_DISPENSED, False, MR_STATUS_STOPPED, "final, nothing dispensed"),
	LINE_CLOSED: LineState(LINE_CLOSED, False, MR_STATUS_COMPLETED, "final after a partial quantity"),
}
OPEN_LINE_STATES = tuple(k for k, v in LINE_STATES.items() if v.is_open)
RESOLVED_LINE_STATES = tuple(k for k, v in LINE_STATES.items() if not v.is_open)

# Event -> (states it applies from, description). The resulting state is always compute_line_state().
LINE_EVENTS: dict[str, tuple[tuple[str, ...], str]] = {
	"submit_dispensed": (OPEN_LINE_STATES, "submit a Dispensed record (quantity > 0, optional final)"),
	"submit_not_dispensed": (OPEN_LINE_STATES, "submit a Not Dispensed record (always final)"),
	"cancel_record": (tuple(LINE_STATES), "cancel a submitted record (Pharmacist, own, visit open)"),
}

# Marley statuses a clinician can set that stop the pharmacy from dispensing.
DISPENSABLE_MR_STATUSES = (MR_STATUS_ACTIVE,)


class OverDispenseError(frappe.ValidationError):
	"""The running total would exceed the prescribed (dispensable) quantity."""


class LineResolvedError(frappe.ValidationError):
	"""The prescription line is already resolved (Dispensed, Not Dispensed or Closed)."""


def compute_line_state(total: float, dispensed: float, final: bool) -> str:
	"""The line state for a prescribed `total`, a `dispensed` running total and a final record flag."""
	total, dispensed = flt(total), flt(dispensed)
	if total > 0 and dispensed >= total:
		return LINE_DISPENSED
	if final:
		return LINE_NOT_DISPENSED if dispensed <= 0 else LINE_CLOSED
	return LINE_PARTLY if dispensed > 0 else LINE_PENDING


def dispensable_quantity(mr: dict | frappe._dict) -> float:
	"""Prescribed quantity of a line: Marley's total_dispensable_quantity (quantity plus repeats)."""
	return flt(mr.get("total_dispensable_quantity")) or flt(mr.get("quantity"))


def line_totals(medication_request: str, exclude: str | None = None) -> tuple[float, bool]:
	"""(dispensed running total, has a final record) over the submitted Dispensing Records of a line."""
	rows = frappe.get_all(
		DISPENSING_DOCTYPE,
		filters={"medication_request": medication_request, "docstatus": 1, "name": ["!=", exclude or ""]},
		fields=["outcome", "quantity", "final"],
	)
	dispensed = sum(flt(r.quantity) for r in rows if r.outcome == OUTCOME_DISPENSED)
	return dispensed, any(r.final for r in rows)


def line_status(medication_request: str) -> str:
	return frappe.db.get_value(MR_DOCTYPE, medication_request, STATUS_FIELD) or LINE_PENDING


def recompute_line(medication_request: str) -> str:
	"""Recompute and store a line's dispense status, dispensed quantity and Marley status. Returns the state."""
	mr = frappe.db.get_value(
		MR_DOCTYPE, medication_request, ["quantity", "total_dispensable_quantity"], as_dict=True
	)
	dispensed, final = line_totals(medication_request)
	state = compute_line_state(dispensable_quantity(mr), dispensed, final)
	frappe.db.set_value(
		MR_DOCTYPE,
		medication_request,
		{STATUS_FIELD: state, QTY_FIELD: dispensed, "status": LINE_STATES[state].marley_status},
	)
	return state


# ---------------------------------------------------------------------------
# Visit
# ---------------------------------------------------------------------------


def visit_is_open(visit: str | None) -> bool:
	if not visit:
		return False
	status = frappe.db.get_value(VISIT_DOCTYPE, visit, "status")
	return bool(status) and status not in VISIT_CLOSED_STATUSES


def after_line_change(visit: str | None, record: str, cancelled: bool = False) -> None:
	"""Raise the visit event that follows a submitted or cancelled Dispensing Record."""
	from onerc_healthcare.pharmacy.tasks import is_pharmacy_complete
	from onerc_healthcare.visit import engine, tasks

	if not visit:
		return
	if not cancelled:
		if is_pharmacy_complete(visit):
			tasks.raise_event(
				visit,
				engine.ACTION_DISPENSED,
				comment=_("All prescription lines resolved ({0})").format(record),
			)
		return
	# Cancel: only an open visit (checked by the controller) whose pharmacy task had already closed.
	if not visit_is_open(visit):
		return
	if not frappe.db.get_value(VISIT_DOCTYPE, visit, "pharmacy_open") and not is_pharmacy_complete(visit):
		tasks.raise_event(
			visit, engine.ACTION_PRESCRIBE, comment=_("Dispensing Record {0} cancelled").format(record)
		)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_line_summary(medication_request: str) -> dict:
	"""Prescribed / dispensed / remaining quantity and state of a line (Dispensing Record form)."""
	frappe.has_permission(DISPENSING_DOCTYPE, "read", throw=True)
	frappe.has_permission(MR_DOCTYPE, "read", doc=medication_request, throw=True)
	mr = frappe.db.get_value(
		MR_DOCTYPE,
		medication_request,
		[
			"quantity",
			"total_dispensable_quantity",
			"medication",
			"medication_item",
			"docstatus",
			"patient",
			"patient_name",
			"order_group",
			"dosage",
			"period",
		],
		as_dict=True,
	)
	if not mr:
		frappe.throw(_("Medication Request {0} does not exist.").format(medication_request))
	total = dispensable_quantity(mr)
	dispensed, _final = line_totals(medication_request)
	return {
		"prescribed": total,
		"dispensed": dispensed,
		"remaining": max(total - dispensed, 0),
		"state": line_status(medication_request),
		"prescribed_medication": mr.medication,
		"prescribed_item": mr.medication_item,
		"prescribed_dosage": mr.dosage,
		"prescribed_period": mr.period,
		"patient": mr.patient,
		"patient_name": mr.patient_name,
		"encounter": mr.order_group,
	}


@frappe.whitelist(methods=["POST"])
def cancel_dispensing(name: str, reason: str) -> dict:
	"""Cancel a submitted Dispensing Record with a reason (Pharmacist, own record, visit open)."""
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Enter the reason for cancelling this dispensing record."))
	doc = frappe.get_doc(DISPENSING_DOCTYPE, name)
	doc.check_permission("cancel")
	if doc.docstatus != 1:
		frappe.throw(_("Only a submitted dispensing record can be cancelled."))
	doc.cancel_reason = reason  # saved with the cancel; nothing is written if the cancel is refused
	doc.cancel()
	return {"name": doc.name, "docstatus": doc.docstatus, "line_status": line_status(doc.medication_request)}


def dispensing_history(encounter: str) -> list[dict]:
	"""Submitted Dispensing Records of an encounter, oldest first (for the patient history, B1).

	Each row keeps the prescribed and the dispensed medicine, so a substitution shows both (FR-22).
	"""
	return frappe.get_all(
		DISPENSING_DOCTYPE,
		filters={"encounter": encounter, "docstatus": 1},
		fields=[
			"name",
			"medication_request",
			"outcome",
			"prescribed_medication",
			"prescribed_item",
			"dispensed_medication",
			"dispensed_item",
			"is_substitution",
			"substitution_reason",
			"quantity",
			"final",
			"not_dispensed_reason",
			"dispensed_by",
			"dispensed_at",
		],
		order_by="dispensed_at asc, creation asc",
	)
