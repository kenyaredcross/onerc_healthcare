# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient Visit state machine (FR-7, SDD §4.4). Status changes only through transition().

Human actions are restricted by role. System events (`request_lab`, `lab_results_ready`, `prescribe`,
`dispensed`, `receive_referral`) are raised by code with ``system=True`` (or by Administrator), normally
through `onerc_healthcare.visit.tasks.raise_event`, which never fails a lab/pharmacy record because of
the visit's state.

Each `Transition` has a status precondition (`from_statuses`) and an optional `guard(doc)` that returns
an error message when the action is impossible in the current flag state. `transition()` raises the
guard message; `get_available_actions()` hides the action. Both use the same checks.

"Completed" below means the consultation is complete: the visit's `encounter` is a submitted Patient
Encounter (`is_consultation_complete`).

Invariants (kept by every action):

1. Closed (Discharged / Referred) visits never change status again. `closed_at` is set exactly when the
   visit enters a closed status. Close Visit and Discharge leave no open task; Referred may keep open
   tasks, and the lab/pharmacy events then only clear the flags.
2. Discharged by `discharge` or by a system event requires ``lab_open == pharmacy_open == 0``.
3. Before completion an open laboratory task takes precedence: ``lab_open`` implies Awaiting Laboratory
   (the results bring the patient back to In Consultation). ``pharmacy_open`` alone gives Awaiting
   Pharmacy, or In Consultation once results have come back.
4. After completion an open pharmacy task takes precedence (the patient goes to the pharmacy while
   results are pending): ``pharmacy_open`` gives Awaiting Pharmacy, else ``lab_open`` gives Awaiting
   Laboratory, else the visit is Discharged. A completed visit is never In Consultation.
5. Awaiting Laboratory implies ``lab_open``; Awaiting Pharmacy implies ``pharmacy_open``.
6. Registered, In Triage and Routed to Department have no open tasks.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import now_datetime

from onerc_healthcare.constants import (
	DEPT_RECEPTION,
	DEPT_TRIAGE,
	ENCOUNTER_DOCTYPE,
	REFERRAL_DOCTYPE,
	ROLE_CLINICIAN,
	ROLE_MHO,
	ROLE_MIC,
	ROLE_RECEPTIONIST,
	ROLE_TRIAGE,
	VISIT_CLOSED_STATUSES,
	VISIT_CLOSURE_REASONS,
	VISIT_DOCTYPE,
	VISIT_OPEN_STATUSES,
	VISIT_STATUS_AWAITING_LAB,
	VISIT_STATUS_AWAITING_PHARMACY,
	VISIT_STATUS_DISCHARGED,
	VISIT_STATUS_IN_CONSULTATION,
	VISIT_STATUS_IN_TRIAGE,
	VISIT_STATUS_REFERRED,
	VISIT_STATUS_REGISTERED,
	VISIT_STATUS_ROUTED,
)

ACTION_REGISTER = "register"
ACTION_START_TRIAGE = "start_triage"
ACTION_ROUTE = "route"
ACTION_REROUTE = "reroute"
ACTION_RETURN_TO_TRIAGE = "return_to_triage"
ACTION_START_CONSULTATION = "start_consultation"
ACTION_REQUEST_LAB = "request_lab"
ACTION_LAB_RESULTS_READY = "lab_results_ready"
ACTION_PRESCRIBE = "prescribe"
ACTION_DISPENSED = "dispensed"
ACTION_DISCHARGE = "discharge"
ACTION_REFER = "refer"
ACTION_COMPLETE_CONSULTATION = "complete_consultation"
ACTION_CLOSE_VISIT = "close_visit"
ACTION_RECEIVE_REFERRAL = "receive_referral"

CONSULTING_ROLES = (ROLE_CLINICIAN, ROLE_MHO)
CLOSE_VISIT_ROLES = (ROLE_RECEPTIONIST, ROLE_TRIAGE, ROLE_CLINICIAN, ROLE_MHO, ROLE_MIC)

# Intake departments: a visit is never routed *to* them. Reception and Triage queues filter on status
# (Registered / In Triage), so a visit "Routed to Department" there would show in no queue. A patient
# who needs re-triage goes back with `return_to_triage` instead.
NON_ROUTABLE_DEPARTMENTS = (DEPT_RECEPTION, DEPT_TRIAGE)


class IllegalVisitTransitionError(frappe.ValidationError):
	"""The action is not allowed from the visit's current status."""


class TriageVitalsMissingError(frappe.ValidationError):
	"""Routing without submitted triage vital signs and without an emergency reason (FR-9)."""


class VisitGuardError(IllegalVisitTransitionError):
	"""The action's guard failed: it is not possible in the visit's current task / encounter state."""


class PharmacyTaskOpenError(VisitGuardError):
	"""Discharge attempted while a pharmacy task is still open."""


class GuardFailure(str):
	"""A guard message that carries a more specific exception class."""

	exc: type[Exception] = VisitGuardError

	def __new__(cls, message: str, exc: type[Exception] = VisitGuardError):
		obj = super().__new__(cls, message)
		obj.exc = exc
		return obj


def is_consultation_complete(doc) -> bool:
	"""The visit's encounter is a submitted Patient Encounter."""
	encounter = doc.get("encounter")
	return bool(encounter) and frappe.db.get_value(ENCOUNTER_DOCTYPE, encounter, "docstatus") == 1


def _guard_lab_open(doc) -> str | None:
	if not doc.lab_open:
		return _("Visit {0} has no open laboratory task.").format(doc.name)
	return None


def _guard_pharmacy_open(doc) -> str | None:
	if not doc.pharmacy_open:
		return _("Visit {0} has no open pharmacy task.").format(doc.name)
	return None


def _guard_no_open_tasks(doc) -> str | None:
	if doc.pharmacy_open:
		return GuardFailure(
			_("Visit {0} cannot be discharged while a pharmacy task is open.").format(doc.name),
			PharmacyTaskOpenError,
		)
	if doc.lab_open:
		return _("Visit {0} cannot be discharged while a laboratory task is open.").format(doc.name)
	return None


def _guard_encounter_submitted(doc) -> str | None:
	if not is_consultation_complete(doc):
		return _("Submit the consultation (Patient Encounter) for visit {0} before completing it.").format(
			doc.name
		)
	return None


@dataclass(frozen=True)
class Transition:
	label: str
	from_statuses: tuple[str, ...]
	# Target status. None: computed by the action handler (system events with open tasks).
	to_status: str | None
	# Roles allowed to perform it. Empty: system event (``system=True`` or Administrator only).
	roles: tuple[str, ...] = ()
	needs_department: bool = False
	needs_comment: bool = False
	needs_closure_reason: bool = False
	timestamp_field: str | None = None
	# guard(doc) -> error message when the action is impossible in the current flag / encounter state.
	guard: Callable | None = None

	@property
	def is_system(self) -> bool:
		return not self.roles


# SDD §4.4 visit lifecycle table.
TRANSITIONS: dict[str, Transition] = {
	ACTION_START_TRIAGE: Transition(
		_("Start Triage"),
		(VISIT_STATUS_REGISTERED,),
		VISIT_STATUS_IN_TRIAGE,
		(ROLE_TRIAGE,),
		timestamp_field="triage_started_at",
	),
	ACTION_ROUTE: Transition(
		_("Route to Department"),
		(VISIT_STATUS_IN_TRIAGE,),
		VISIT_STATUS_ROUTED,
		(ROLE_TRIAGE,),
		needs_department=True,
		timestamp_field="routed_at",
	),
	ACTION_REROUTE: Transition(
		_("Re-route"),
		(VISIT_STATUS_ROUTED,),
		VISIT_STATUS_ROUTED,
		(ROLE_TRIAGE,),
		needs_department=True,
	),
	ACTION_RETURN_TO_TRIAGE: Transition(
		_("Return to Triage"),
		(VISIT_STATUS_ROUTED,),
		VISIT_STATUS_IN_TRIAGE,
		(ROLE_TRIAGE, *CONSULTING_ROLES),
		needs_comment=True,
	),
	ACTION_START_CONSULTATION: Transition(
		_("Start Consultation"),
		(VISIT_STATUS_ROUTED,),
		VISIT_STATUS_IN_CONSULTATION,
		CONSULTING_ROLES,
		timestamp_field="consultation_started_at",
	),
	ACTION_REQUEST_LAB: Transition(
		_("Laboratory Requested"),
		(VISIT_STATUS_IN_CONSULTATION, VISIT_STATUS_AWAITING_PHARMACY, VISIT_STATUS_AWAITING_LAB),
		VISIT_STATUS_AWAITING_LAB,
	),
	ACTION_LAB_RESULTS_READY: Transition(
		_("Laboratory Results Ready"),
		(VISIT_STATUS_AWAITING_LAB, VISIT_STATUS_AWAITING_PHARMACY, VISIT_STATUS_REFERRED),
		None,
		guard=_guard_lab_open,
	),
	ACTION_PRESCRIBE: Transition(
		_("Prescription Submitted"),
		(VISIT_STATUS_IN_CONSULTATION, VISIT_STATUS_AWAITING_LAB, VISIT_STATUS_AWAITING_PHARMACY),
		None,
	),
	ACTION_DISPENSED: Transition(
		_("Dispensing Complete"),
		(
			VISIT_STATUS_AWAITING_PHARMACY,
			VISIT_STATUS_AWAITING_LAB,
			VISIT_STATUS_IN_CONSULTATION,
			VISIT_STATUS_REFERRED,
		),
		None,
		guard=_guard_pharmacy_open,
	),
	ACTION_DISCHARGE: Transition(
		_("Discharge"),
		(VISIT_STATUS_IN_CONSULTATION,),
		VISIT_STATUS_DISCHARGED,
		CONSULTING_ROLES,
		timestamp_field="closed_at",
		guard=_guard_no_open_tasks,
	),
	ACTION_COMPLETE_CONSULTATION: Transition(
		_("Complete Consultation"),
		(VISIT_STATUS_IN_CONSULTATION, VISIT_STATUS_AWAITING_LAB, VISIT_STATUS_AWAITING_PHARMACY),
		None,
		CONSULTING_ROLES,
		guard=_guard_encounter_submitted,
	),
	ACTION_REFER: Transition(
		_("Refer"),
		(VISIT_STATUS_IN_CONSULTATION,),
		VISIT_STATUS_REFERRED,
		CONSULTING_ROLES,
		needs_comment=True,
		timestamp_field="closed_at",
	),
	ACTION_CLOSE_VISIT: Transition(
		_("Close Visit"),
		VISIT_OPEN_STATUSES,
		VISIT_STATUS_DISCHARGED,
		CLOSE_VISIT_ROLES,
		needs_comment=True,
		needs_closure_reason=True,
		timestamp_field="closed_at",
	),
	ACTION_RECEIVE_REFERRAL: Transition(
		_("Receive Internal Referral"),
		(VISIT_STATUS_REGISTERED,),
		VISIT_STATUS_ROUTED,
		needs_department=True,
		timestamp_field="routed_at",
	),
}

# Fields only the engine may change after insert (the controller rejects other edits).
WORKFLOW_FIELDS = (
	"status",
	"current_department",
	"status_changed_at",
	"lab_open",
	"pharmacy_open",
	"triage_started_at",
	"routed_at",
	"consultation_started_at",
	"closed_at",
	"triage_level",
	"triage_vital_signs",
	"routed_without_vitals",
	"encounter",
	"referred_from",
	"closure_reason",
)
# Fields set by the server at registration and never changed by hand afterwards.
REGISTRATION_FIELDS = ("visit_date", "arrived_at", "queue_number", "patient_identifier", "company")
PROTECTED_FIELDS = WORKFLOW_FIELDS + REGISTRATION_FIELDS
# Status log columns that must never change once written (SDD §4.4 audit trail).
STATUS_LOG_FIELDS = ("from_status", "to_status", "action", "user", "timestamp", "department", "comment")
# Longest comment kept on a status-log row (NFR-10); also the limit for an emergency routing reason.
MAX_COMMENT_LENGTH = 1000
WITHOUT_VITALS_PREFIX = "Routed without vital signs: "
CLOSED_PREFIX = "Closed: "


def transition(visit: str, action: str, **kwargs):
	"""Apply `action` to Patient Visit `visit` and return the saved document.

	Keyword arguments:
	- ``department``: target Medical Department (required for ``route``).
	- ``comment``: free text stored on the log row.
	- ``vital_signs``: Vital Signs to link as the triage record (``start_triage`` / ``route``).
	- ``without_vitals_reason``: emergency override for ``route`` when no triage vital signs have been
	  submitted (FR-9); stored on the log row and flags the visit ``routed_without_vitals``.
	- ``closure_reason``: one of ``VISIT_CLOSURE_REASONS`` (``close_visit``).
	- ``referral``: the Referral that created this visit (``receive_referral``).
	- ``system``: True when called by system code (lab/pharmacy events in later phases).

	Raises ``frappe.PermissionError`` for a wrong role and ``frappe.ValidationError`` for an illegal
	transition.
	"""
	rule = TRANSITIONS.get(action)
	if not rule:
		frappe.throw(_("Unknown visit action: {0}").format(action), IllegalVisitTransitionError)

	system = bool(kwargs.get("system"))
	department = kwargs.get("department")
	comment = (kwargs.get("comment") or "").strip() or None
	department = (department or "").strip() or None
	vital_signs = kwargs.get("vital_signs")

	doc = frappe.get_doc(VISIT_DOCTYPE, visit, for_update=True)
	_check_role(doc, action, rule, system)

	if doc.status not in rule.from_statuses:
		frappe.throw(
			_("Action {0} is not allowed for visit {1} in status {2}.").format(
				frappe.bold(rule.label), doc.name, frappe.bold(_(doc.status))
			),
			IllegalVisitTransitionError,
		)

	failure = rule.guard(doc) if rule.guard else None
	if failure:
		frappe.throw(failure, getattr(failure, "exc", VisitGuardError), title=_("Not Possible Now"))

	_validate_comment(rule, comment)
	if action == ACTION_COMPLETE_CONSULTATION and doc.lab_open and not comment:
		frappe.throw(
			_("Laboratory results are still pending. Give a reason to complete the consultation now."),
			IllegalVisitTransitionError,
			title=_("Reason Required"),
		)
	if vital_signs:
		link_vital_signs(doc, vital_signs, save=False)

	from_status = doc.status
	to_status = _apply(doc, action, rule, department=department)
	if action == ACTION_ROUTE:
		comment = _check_triage_vitals(doc, kwargs.get("without_vitals_reason"), comment)
	elif action == ACTION_CLOSE_VISIT:
		comment = _apply_closure(doc, kwargs.get("closure_reason"), comment)
	elif action == ACTION_RECEIVE_REFERRAL:
		_set_referral(doc, kwargs.get("referral"))

	now = now_datetime()
	if rule.timestamp_field and not doc.get(rule.timestamp_field):
		doc.set(rule.timestamp_field, now)
	if to_status in VISIT_CLOSED_STATUSES and not doc.closed_at:
		doc.closed_at = now
	if to_status != from_status:
		doc.status = to_status
		doc.status_changed_at = now

	doc.append(
		"status_log",
		{
			"from_status": from_status,
			"to_status": to_status,
			"action": action,
			"user": frappe.session.user,
			"timestamp": now,
			"department": doc.current_department,
			"comment": comment,
		},
	)
	_save_from_workflow(doc)
	if action == ACTION_CLOSE_VISIT:
		from onerc_healthcare.visit.tasks import on_visit_closed

		on_visit_closed(doc)
	return doc


def _save_from_workflow(doc) -> None:
	"""Save with the engine's flag; the controller refuses protected-field changes without it."""
	doc.flags.from_workflow = True
	try:
		doc.save(ignore_permissions=True)
	finally:
		doc.flags.from_workflow = False


def _validate_comment(rule: Transition, comment: str | None) -> None:
	if rule.needs_comment and not comment:
		frappe.throw(
			_("Enter a reason for {0}; it is recorded in the visit history.").format(frappe.bold(rule.label)),
			IllegalVisitTransitionError,
			title=_("Reason Required"),
		)
	if comment and len(comment) > MAX_COMMENT_LENGTH:
		frappe.throw(
			_("The comment is {0} characters long; the limit is {1}. Shorten it.").format(
				len(comment), MAX_COMMENT_LENGTH
			),
			IllegalVisitTransitionError,
			title=_("Comment Too Long"),
		)


def _check_role(doc, action: str, rule: Transition, system: bool) -> None:
	user = frappe.session.user
	if system or user == "Administrator":
		return
	if rule.is_system:
		frappe.throw(
			_("{0} is a system event and cannot be performed by a user.").format(frappe.bold(rule.label)),
			frappe.PermissionError,
		)
	if not set(frappe.get_roles(user)) & set(rule.roles):
		frappe.throw(
			_("You are not allowed to perform {0}. Required role: {1}.").format(
				frappe.bold(rule.label), ", ".join(_(r) for r in rule.roles)
			),
			frappe.PermissionError,
		)
	if not doc.has_permission("read"):
		frappe.throw(_("You do not have access to visit {0}.").format(doc.name), frappe.PermissionError)


def _apply(doc, action: str, rule: Transition, department: str | None = None) -> str:
	"""Apply action side effects to `doc` and return the target status."""
	to_status = rule.to_status

	if action == ACTION_START_TRIAGE:
		if frappe.db.exists("Medical Department", DEPT_TRIAGE):
			doc.current_department = DEPT_TRIAGE

	elif action in (ACTION_ROUTE, ACTION_REROUTE, ACTION_RECEIVE_REFERRAL):
		_validate_route_target(doc, action, department)
		doc.current_department = department
		if action == ACTION_REROUTE:
			# Waiting time in the new department's queue starts again.
			doc.routed_at = doc.status_changed_at = now_datetime()

	elif action == ACTION_RETURN_TO_TRIAGE:
		if frappe.db.exists("Medical Department", DEPT_TRIAGE):
			doc.current_department = DEPT_TRIAGE

	elif action == ACTION_REQUEST_LAB:
		doc.lab_open = 1
		to_status = _completed_status(doc) if is_consultation_complete(doc) else VISIT_STATUS_AWAITING_LAB

	elif action == ACTION_LAB_RESULTS_READY:
		doc.lab_open = 0
		if doc.status in VISIT_CLOSED_STATUSES:
			to_status = doc.status  # Referred: only the task closes
		elif is_consultation_complete(doc):
			to_status = _completed_status(doc)
		else:
			to_status = VISIT_STATUS_IN_CONSULTATION  # results bring the patient back

	elif action == ACTION_PRESCRIBE:
		doc.pharmacy_open = 1
		if is_consultation_complete(doc):
			to_status = _completed_status(doc)
		else:
			to_status = VISIT_STATUS_AWAITING_LAB if doc.lab_open else VISIT_STATUS_AWAITING_PHARMACY

	elif action == ACTION_DISPENSED:
		doc.pharmacy_open = 0
		if doc.status in VISIT_CLOSED_STATUSES:
			to_status = doc.status  # Referred: only the task closes
		elif is_consultation_complete(doc):
			to_status = _completed_status(doc)
		elif doc.status == VISIT_STATUS_AWAITING_PHARMACY:
			# Not completed yet: back to the clinician (lab_open would have meant Awaiting Laboratory).
			to_status = VISIT_STATUS_IN_CONSULTATION
		else:
			to_status = doc.status

	elif action == ACTION_COMPLETE_CONSULTATION:
		to_status = _completed_status(doc)

	return to_status


def _completed_status(doc) -> str:
	"""Status of a completed consultation from its open tasks (invariant 4)."""
	if doc.pharmacy_open:
		return VISIT_STATUS_AWAITING_PHARMACY
	if doc.lab_open:
		return VISIT_STATUS_AWAITING_LAB
	return VISIT_STATUS_DISCHARGED


def _apply_closure(doc, closure_reason: str | None, comment: str | None) -> str:
	"""Close Visit: reason category from VISIT_CLOSURE_REASONS, open tasks dropped. Returns the log text."""
	if closure_reason not in VISIT_CLOSURE_REASONS:
		frappe.throw(
			_("Choose why the visit is closed: {0}.").format(", ".join(_(r) for r in VISIT_CLOSURE_REASONS)),
			IllegalVisitTransitionError,
			title=_("Closure Reason Required"),
		)
	doc.closure_reason = closure_reason
	doc.lab_open = doc.pharmacy_open = 0
	return f"{CLOSED_PREFIX}{closure_reason} — {comment}"


def _set_referral(doc, referral: str | None) -> None:
	if referral:
		if not frappe.db.exists(REFERRAL_DOCTYPE, referral):
			frappe.throw(_("Referral {0} does not exist.").format(referral), IllegalVisitTransitionError)
		doc.referred_from = referral
	elif frappe.db.table_exists(REFERRAL_DOCTYPE):
		frappe.throw(_("The Referral that created this visit is required."), IllegalVisitTransitionError)


def has_submitted_triage_vitals(doc) -> bool:
	"""The visit's triage Vital Signs exists, is submitted and is linked back to this visit."""
	if not doc.get("triage_vital_signs"):
		return False
	vs = frappe.db.get_value("Vital Signs", doc.triage_vital_signs, ["docstatus", "krcs_visit"], as_dict=True)
	return bool(vs and vs.docstatus == 1 and vs.krcs_visit == doc.name)


def _check_triage_vitals(doc, reason: str | None, comment: str | None) -> str | None:
	"""FR-9: route needs submitted triage vitals, or an emergency reason. Returns the log comment."""
	if has_submitted_triage_vitals(doc):
		return comment
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(
			_(
				"Record and submit the triage vital signs before routing, or give a reason to route"
				" without them (emergency)."
			),
			TriageVitalsMissingError,
			title=_("Vital Signs Missing"),
		)
	if len(reason) > MAX_COMMENT_LENGTH:
		frappe.throw(
			_("The reason for routing without vital signs is {0} characters long; the limit is {1}.").format(
				len(reason), MAX_COMMENT_LENGTH
			),
			TriageVitalsMissingError,
			title=_("Reason Too Long"),
		)
	# Sticky: the visit stays reportable as an override even if it is re-triaged later.
	doc.routed_without_vitals = 1
	logged = WITHOUT_VITALS_PREFIX + reason
	return f"{logged} | {comment}" if comment else logged


def _validate_route_target(doc, action: str, department: str | None) -> None:
	if not department:
		frappe.throw(_("Select the department to route the patient to."), IllegalVisitTransitionError)
	if department in NON_ROUTABLE_DEPARTMENTS:
		frappe.throw(
			_(
				"A patient cannot be routed to {0}. Choose the clinical department that will see the patient;"
				" to repeat triage, use Return to Triage."
			).format(frappe.bold(_(department))),
			IllegalVisitTransitionError,
			title=_("Department Not Allowed"),
		)
	if not frappe.db.exists("Medical Department", department):
		frappe.throw(
			_("Medical Department {0} does not exist.").format(frappe.bold(department)),
			IllegalVisitTransitionError,
		)
	if action == ACTION_REROUTE and department == doc.current_department:
		frappe.throw(
			_("The visit is already routed to {0}. Choose a different department.").format(
				frappe.bold(_(department))
			),
			IllegalVisitTransitionError,
			title=_("Same Department"),
		)


def link_vital_signs(doc, vital_signs: str, save: bool = True) -> None:
	"""Link a Vital Signs record as the visit's triage record (and copy its triage level)."""
	doc.triage_vital_signs = vital_signs
	level = frappe.db.get_value("Vital Signs", vital_signs, "krcs_triage_level")
	if level:
		doc.triage_level = level
	if save:
		_save_from_workflow(doc)


def get_available_actions(doc, user: str | None = None) -> list[dict]:
	"""Human actions `user` may perform on `doc` now (for form buttons). System events are excluded."""
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	actions = []
	for action, rule in TRANSITIONS.items():
		if rule.is_system or doc.status not in rule.from_statuses:
			continue
		if user != "Administrator" and not roles & set(rule.roles):
			continue
		if rule.guard and rule.guard(doc):
			continue
		needs_comment = rule.needs_comment or (action == ACTION_COMPLETE_CONSULTATION and bool(doc.lab_open))
		actions.append(
			{
				"action": action,
				"label": rule.label,
				"needs_department": rule.needs_department,
				"needs_comment": needs_comment,
				"needs_closure_reason": rule.needs_closure_reason,
				"is_primary": action in (ACTION_START_TRIAGE, ACTION_ROUTE, ACTION_START_CONSULTATION),
			}
		)
	return actions


def link_encounter(visit: str, encounter: str):
	"""Link the visit's consultation (Patient Encounter). Called by the encounter code (B1)."""
	doc = frappe.get_doc(VISIT_DOCTYPE, visit, for_update=True)
	patient = frappe.db.get_value(ENCOUNTER_DOCTYPE, encounter, "patient")
	if not patient:
		frappe.throw(
			_("Patient Encounter {0} does not exist.").format(encounter), IllegalVisitTransitionError
		)
	if patient != doc.patient:
		frappe.throw(
			_("Patient Encounter {0} belongs to a different patient than visit {1}.").format(
				encounter, visit
			),
			IllegalVisitTransitionError,
		)
	if doc.encounter == encounter:
		return doc
	doc.encounter = encounter
	_save_from_workflow(doc)
	return doc


@frappe.whitelist()
def get_non_routable_departments() -> list[str]:
	"""Departments excluded from the route / re-route pickers (Reception, Triage)."""
	return list(NON_ROUTABLE_DEPARTMENTS)


@frappe.whitelist(methods=["POST"])
def apply_action(
	visit: str,
	action: str,
	department: str | None = None,
	comment: str | None = None,
	without_vitals_reason: str | None = None,
	closure_reason: str | None = None,
) -> dict:
	"""Perform a visit action as the current user (form buttons, Vital Signs Route action)."""
	doc = transition(
		visit,
		action,
		department=department,
		comment=comment,
		without_vitals_reason=without_vitals_reason,
		closure_reason=closure_reason,
	)
	return {"name": doc.name, "status": doc.status, "current_department": doc.current_department}
