# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Prescription line (Medication Request) dispensing state, FR-21, FR-22, BR-6, SDD §5.5.

A Medication Request is one prescription line. The Pharmacist records Dispensing Records against it
(partial dispensing is allowed). The line state lives in custom fields on the Medication Request,
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

Clinician hold (`krcs_hold_status`, `HOLD_TRANSITIONS`; endpoints in `pharmacy.api`). A second,
independent dimension set by the prescriber (Clinical Officer / Mental Health Officer, own lines) or the
Medical In-Charge (any line), always with a reason:

| Action   | Line state      | Hold from      | Hold to | Marley status            | Visit effect              |
|----------|-----------------|----------------|---------|--------------------------|---------------------------|
| `hold`   | Pending, Partly | (none)         | Held    | on-hold                  | none (a held line is open)|
| `stop`   | Pending, Partly | (none), Held   | Stopped | stopped                  | `dispensed` if last line  |
| `resume` | Pending, Partly | Held           | (none)  | from the line state      | none                      |

A Marley status set from outside our machine (on-hold, stopped, cancelled, entered-in-error, unknown,
ended, e.g. through Marley's own status endpoints) is kept by `recompute_line` and acts as an external
hold: dispensing is refused (only `active` is dispensable), and for the visit an external stopped /
cancelled / entered-in-error / ended line counts as resolved while on-hold / unknown stays open.

While Held or Stopped, no Dispensing Record can be submitted. `recompute_line` keeps the hold: the Marley
status is the hold's status whenever `krcs_hold_status` is set (so cancelling a dispensing record on a held
line leaves it held). A Stopped line counts as resolved for the visit (`is_line_resolved`); a Held one does
not.

Events (`LINE_EVENTS`; S1 can drive every row): submitting a Dispensing Record, cancelling one, and the
three hold actions. An open line accepts new records; a resolved line (Dispensed / Not Dispensed / Closed)
accepts none until a record is cancelled, which re-opens it.

Visit: when every submitted Medication Request of the encounter is resolved, `tasks.raise_event(visit,
"dispensed")` closes the pharmacy task. Cancelling a record (allowed only while the visit is open)
re-opens the task with `prescribe` when the visit had no open pharmacy task left.

Locking (fix round M7 / L1; lock order Visit -> Encounter -> Medication Request): every path that changes a
line (dispensing submit and cancel, hold / stop / resume) first locks the Patient Visit row, then the
Medication Request row (`lock_line`), and every read after that is a locking read (`FOR UPDATE`): the
running totals, the line itself and the statuses of the visit's other lines. Concurrent dispensing of
one line, or of the last two lines of a visit, is therefore serialised and sees committed data.
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
HOLD_FIELD = "krcs_hold_status"
HOLD_REASON_FIELD = "krcs_hold_reason"

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
MR_STATUS_ON_HOLD = "on-hold-Medication Request Status"

HOLD_NONE = ""
HOLD_HELD = "Held"
HOLD_STOPPED = "Stopped"
HOLD_STATES = (HOLD_NONE, HOLD_HELD, HOLD_STOPPED)
HOLD_MARLEY_STATUS = {HOLD_HELD: MR_STATUS_ON_HOLD, HOLD_STOPPED: MR_STATUS_STOPPED}

# Marley statuses our line machine does not manage. When one is set from outside (Marley's
# `set_medication_request_status` / `set_request_status`, by a role that still has write) and not by our
# own hold, `recompute_line` keeps it: it acts as an external hold (DEFECT-3, second case).
MR_STATUS_CANCELLED = "cancelled-Medication Request Status"
MR_STATUS_ENTERED_IN_ERROR = "entered-in-error-Medication Request Status"
MR_STATUS_UNKNOWN = "unknown-Medication Request Status"
MR_STATUS_ENDED = "ended-Medication Request Status"
EXTERNAL_MR_STATUSES = (
	MR_STATUS_ON_HOLD,
	MR_STATUS_STOPPED,
	MR_STATUS_CANCELLED,
	MR_STATUS_ENTERED_IN_ERROR,
	MR_STATUS_UNKNOWN,
	MR_STATUS_ENDED,
)
# External statuses that end the line for the visit; the others (on-hold, unknown) keep it open.
EXTERNAL_RESOLVED_STATUSES = (
	MR_STATUS_STOPPED,
	MR_STATUS_CANCELLED,
	MR_STATUS_ENTERED_IN_ERROR,
	MR_STATUS_ENDED,
)


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

ACTION_HOLD = "hold"
ACTION_STOP = "stop"
ACTION_RESUME = "resume"


@dataclass(frozen=True)
class HoldTransition:
	label: str
	from_line_states: tuple[str, ...]
	from_holds: tuple[str, ...]
	to_hold: str


HOLD_TRANSITIONS: dict[str, HoldTransition] = {
	ACTION_HOLD: HoldTransition(_("Hold"), OPEN_LINE_STATES, (HOLD_NONE,), HOLD_HELD),
	ACTION_STOP: HoldTransition(_("Stop"), OPEN_LINE_STATES, (HOLD_NONE, HOLD_HELD), HOLD_STOPPED),
	ACTION_RESUME: HoldTransition(_("Resume"), OPEN_LINE_STATES, (HOLD_HELD,), HOLD_NONE),
}

# Event -> (line states it applies from, hold states it applies from, description).
# Dispensing events: the resulting line state is always compute_line_state(); the hold is unchanged.
LINE_EVENTS: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
	"submit_dispensed": (
		OPEN_LINE_STATES,
		(HOLD_NONE,),
		"submit a Dispensed record (quantity > 0, optional final)",
	),
	"submit_not_dispensed": (OPEN_LINE_STATES, (HOLD_NONE,), "submit a Not Dispensed record (always final)"),
	"cancel_record": (
		tuple(LINE_STATES),
		HOLD_STATES,
		"cancel a submitted record (Pharmacist, own, visit open)",
	),
	**{
		action: (rule.from_line_states, rule.from_holds, f"{action}: hold -> {rule.to_hold or '(none)'}")
		for action, rule in HOLD_TRANSITIONS.items()
	},
}

# Marley statuses a clinician can set that stop the pharmacy from dispensing.
DISPENSABLE_MR_STATUSES = (MR_STATUS_ACTIVE,)

# Medication Request columns read under the line lock.
MR_LOCK_FIELDS = (
	"name",
	"docstatus",
	"status",
	"patient",
	"practitioner",
	"order_group",
	"medication",
	"medication_item",
	"dosage",
	"period",
	"quantity",
	"total_dispensable_quantity",
	STATUS_FIELD,
	QTY_FIELD,
	HOLD_FIELD,
)


class OverDispenseError(frappe.ValidationError):
	"""The running total would exceed the prescribed (dispensable) quantity."""


class LineResolvedError(frappe.ValidationError):
	"""The prescription line is already resolved (Dispensed, Not Dispensed or Closed)."""


class LineHeldError(frappe.ValidationError):
	"""The prescriber has put the line on hold or stopped it."""


def compute_line_state(total: float, dispensed: float, final: bool) -> str:
	"""The line state for a prescribed `total`, a `dispensed` running total and a final record flag."""
	total, dispensed = flt(total), flt(dispensed)
	if total > 0 and dispensed >= total:
		return LINE_DISPENSED
	if final:
		return LINE_NOT_DISPENSED if dispensed <= 0 else LINE_CLOSED
	return LINE_PARTLY if dispensed > 0 else LINE_PENDING


def marley_status_for(state: str, hold: str | None) -> str:
	"""Marley status of a line: the hold's status while held or stopped, else the line state's (DEFECT-3)."""
	return HOLD_MARLEY_STATUS.get(hold or HOLD_NONE) or LINE_STATES[state].marley_status


def external_status(state: str | None, hold: str | None, status: str | None) -> str | None:
	"""Marley status set outside our line machine (not by our hold, not our own Not Dispensed `stopped`)."""
	if hold or status not in EXTERNAL_MR_STATUSES:
		return None
	if status == MR_STATUS_STOPPED and (state or LINE_PENDING) == LINE_NOT_DISPENSED:
		return None  # written by recompute_line for a Not Dispensed line
	return status


def is_line_resolved(state: str | None, hold: str | None, status: str | None = None) -> bool:
	"""Resolved for the visit: Dispensed / Not Dispensed / Closed, stopped by the prescriber, or ended
	from outside (Marley status stopped / cancelled / entered-in-error / ended). On hold stays open."""
	if (state or LINE_PENDING) in RESOLVED_LINE_STATES or (hold or HOLD_NONE) == HOLD_STOPPED:
		return True
	return external_status(state, hold, status) in EXTERNAL_RESOLVED_STATUSES


def dispensable_quantity(mr: dict | frappe._dict) -> float:
	"""Prescribed quantity of a line: Marley's total_dispensable_quantity (quantity plus repeats)."""
	return flt(mr.get("total_dispensable_quantity")) or flt(mr.get("quantity"))


# ---------------------------------------------------------------------------
# Locking (order: Visit -> Encounter -> Medication Request)
# ---------------------------------------------------------------------------


def lock_visit(visit: str | None) -> frappe._dict | None:
	"""Lock a Patient Visit row (SELECT ... FOR UPDATE) and return its status columns."""
	if not visit:
		return None
	rows = frappe.db.sql(
		"SELECT `name`, `status`, `pharmacy_open`, `lab_open` FROM `tabPatient Visit` WHERE `name`=%s FOR UPDATE",
		(visit,),
		as_dict=True,
	)
	return rows[0] if rows else None


def lock_request(medication_request: str) -> frappe._dict | None:
	"""Lock a Medication Request row and return it (locking read of `MR_LOCK_FIELDS`)."""
	columns = ", ".join(f"`{f}`" for f in MR_LOCK_FIELDS)
	rows = frappe.db.sql(
		f"SELECT {columns} FROM `tabMedication Request` WHERE `name`=%s FOR UPDATE",
		(medication_request,),
		as_dict=True,
	)
	return rows[0] if rows else None


def visit_of_request(medication_request: str) -> str | None:
	"""The Patient Visit of a line. Plain reads of fields that never change (taken before any lock)."""
	from onerc_healthcare.visit.tasks import visit_for_encounter

	encounter = frappe.db.get_value(MR_DOCTYPE, medication_request, "order_group")
	return visit_for_encounter(encounter) if encounter else None


def lock_line(medication_request: str) -> tuple[frappe._dict | None, frappe._dict | None]:
	"""Lock the line's visit, then the line itself. Returns (visit row, Medication Request row)."""
	visit_row = lock_visit(visit_of_request(medication_request))
	return visit_row, lock_request(medication_request)


def line_totals(
	medication_request: str, exclude: str | None = None, for_update: bool = False
) -> tuple[float, bool]:
	"""(dispensed running total, has a final record) over the submitted Dispensing Records of a line."""
	rows = frappe.db.sql(
		"SELECT `outcome`, `quantity`, `final` FROM `tabDispensing Record`"
		" WHERE `medication_request`=%s AND `docstatus`=1 AND `name`!=%s"
		+ (" FOR UPDATE" if for_update else ""),
		(medication_request, exclude or ""),
		as_dict=True,
	)
	dispensed = sum(flt(r.quantity) for r in rows if r.outcome == OUTCOME_DISPENSED)
	return dispensed, any(r.final for r in rows)


def line_status(medication_request: str) -> str:
	return frappe.db.get_value(MR_DOCTYPE, medication_request, STATUS_FIELD) or LINE_PENDING


def recompute_line(medication_request: str) -> str:
	"""Recompute and store a line's dispense status, dispensed quantity and Marley status. Returns the state.

	Called with the line already locked; all reads here are locking reads. The clinician's hold is kept,
	and so is a Marley status set from outside (`external_status`, DEFECT-3).
	"""
	mr = lock_request(medication_request)
	dispensed, final = line_totals(medication_request, for_update=True)
	state = compute_line_state(dispensable_quantity(mr), dispensed, final)
	hold = mr.get(HOLD_FIELD)
	status = external_status(mr.get(STATUS_FIELD), hold, mr.status) or marley_status_for(state, hold)
	frappe.db.set_value(
		MR_DOCTYPE, medication_request, {STATUS_FIELD: state, QTY_FIELD: dispensed, "status": status}
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


def raise_if_complete(visit: str | None, comment: str) -> bool:
	"""Raise `dispensed` when every line of the visit is resolved (locking reads). Returns True if raised."""
	from onerc_healthcare.pharmacy.tasks import is_pharmacy_complete
	from onerc_healthcare.visit import engine, tasks

	if visit and is_pharmacy_complete(visit, for_update=True):
		return tasks.raise_event(visit, engine.ACTION_DISPENSED, comment=comment)
	return False


def after_line_change(visit: str | None, record: str, cancelled: bool = False) -> None:
	"""Raise the visit event that follows a submitted or cancelled Dispensing Record (visit already locked)."""
	from onerc_healthcare.pharmacy.tasks import is_pharmacy_complete
	from onerc_healthcare.visit import engine, tasks

	if not visit:
		return
	if not cancelled:
		raise_if_complete(visit, _("All prescription lines resolved ({0})").format(record))
		return
	# Cancel: only an open visit (checked by the controller) whose pharmacy task had already closed.
	row = lock_visit(visit)
	if not row or row.status in VISIT_CLOSED_STATUSES:
		return
	if not row.pharmacy_open and not is_pharmacy_complete(visit, for_update=True):
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
			HOLD_FIELD,
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
		"hold": mr.get(HOLD_FIELD) or HOLD_NONE,
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
