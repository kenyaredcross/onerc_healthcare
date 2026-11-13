# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient Visit state machine (FR-7, SDD §4.4). Status changes only through transition().

Human actions are restricted by role. System events (`request_lab`, `lab_results_ready`, `prescribe`,
`dispensed`) are raised by code in later phases with ``system=True`` (or by Administrator).

Laboratory and pharmacy tasks can be open together (SDD §4.4): `lab_open` / `pharmacy_open` carry the
open tasks, and `status` shows the one that currently holds the patient. An open laboratory task takes
precedence, because its results bring the patient back to consultation.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import now_datetime

from onerc_healthcare.constants import (
	DEPT_RECEPTION,
	DEPT_TRIAGE,
	ROLE_CLINICIAN,
	ROLE_MHO,
	ROLE_TRIAGE,
	VISIT_CLOSED_STATUSES,
	VISIT_DOCTYPE,
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

CONSULTING_ROLES = (ROLE_CLINICIAN, ROLE_MHO)

# Intake departments: a visit is never routed *to* them. Reception and Triage queues filter on status
# (Registered / In Triage), so a visit "Routed to Department" there would show in no queue. A patient
# who needs re-triage goes back with `return_to_triage` instead.
NON_ROUTABLE_DEPARTMENTS = (DEPT_RECEPTION, DEPT_TRIAGE)


class IllegalVisitTransitionError(frappe.ValidationError):
	"""The action is not allowed from the visit's current status."""


class TriageVitalsMissingError(frappe.ValidationError):
	"""Routing without submitted triage vital signs and without an emergency reason (FR-9)."""


class PharmacyTaskOpenError(frappe.ValidationError):
	"""Discharge attempted while a pharmacy task is still open."""


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
	timestamp_field: str | None = None

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
		(VISIT_STATUS_AWAITING_LAB,),
		VISIT_STATUS_IN_CONSULTATION,
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
	),
	ACTION_DISCHARGE: Transition(
		_("Discharge"),
		(VISIT_STATUS_IN_CONSULTATION, VISIT_STATUS_AWAITING_PHARMACY),
		VISIT_STATUS_DISCHARGED,
		CONSULTING_ROLES,
		timestamp_field="closed_at",
	),
	ACTION_REFER: Transition(
		_("Refer"),
		(VISIT_STATUS_IN_CONSULTATION,),
		VISIT_STATUS_REFERRED,
		CONSULTING_ROLES,
		needs_comment=True,
		timestamp_field="closed_at",
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
)
# Fields set by the server at registration and never changed by hand afterwards.
REGISTRATION_FIELDS = ("visit_date", "arrived_at", "queue_number", "patient_identifier", "company")
PROTECTED_FIELDS = WORKFLOW_FIELDS + REGISTRATION_FIELDS
# Status log columns that must never change once written (SDD §4.4 audit trail).
STATUS_LOG_FIELDS = ("from_status", "to_status", "action", "user", "timestamp", "department", "comment")
# Longest comment kept on a status-log row (NFR-10); also the limit for an emergency routing reason.
MAX_COMMENT_LENGTH = 1000
WITHOUT_VITALS_PREFIX = "Routed without vital signs: "


def transition(visit: str, action: str, **kwargs):
	"""Apply `action` to Patient Visit `visit` and return the saved document.

	Keyword arguments:
	- ``department``: target Medical Department (required for ``route``).
	- ``comment``: free text stored on the log row.
	- ``vital_signs``: Vital Signs to link as the triage record (``start_triage`` / ``route``).
	- ``without_vitals_reason``: emergency override for ``route`` when no triage vital signs have been
	  submitted (FR-9); stored on the log row and flags the visit ``routed_without_vitals``.
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

	_validate_comment(rule, comment)
	if vital_signs:
		link_vital_signs(doc, vital_signs, save=False)

	from_status = doc.status
	to_status = _apply(doc, action, rule, department=department)
	if action == ACTION_ROUTE:
		comment = _check_triage_vitals(doc, kwargs.get("without_vitals_reason"), comment)

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

	elif action in (ACTION_ROUTE, ACTION_REROUTE):
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

	elif action == ACTION_LAB_RESULTS_READY:
		if not doc.lab_open:
			frappe.throw(
				_("Visit {0} has no open laboratory task.").format(doc.name), IllegalVisitTransitionError
			)
		doc.lab_open = 0

	elif action == ACTION_PRESCRIBE:
		doc.pharmacy_open = 1
		to_status = VISIT_STATUS_AWAITING_LAB if doc.lab_open else VISIT_STATUS_AWAITING_PHARMACY

	elif action == ACTION_DISPENSED:
		if not doc.pharmacy_open:
			frappe.throw(
				_("Visit {0} has no open pharmacy task.").format(doc.name), IllegalVisitTransitionError
			)
		doc.pharmacy_open = 0
		# Awaiting Pharmacy -> Discharged once dispensing is complete (SDD §4.4); otherwise the
		# visit stays where it is and only the pharmacy task closes.
		if doc.status == VISIT_STATUS_AWAITING_PHARMACY and not doc.lab_open:
			to_status = VISIT_STATUS_DISCHARGED
		else:
			to_status = doc.status

	elif action == ACTION_DISCHARGE:
		if doc.pharmacy_open:
			frappe.throw(
				_("Visit {0} cannot be discharged while a pharmacy task is open.").format(doc.name),
				PharmacyTaskOpenError,
			)
		if doc.lab_open:
			frappe.throw(
				_("Visit {0} cannot be discharged while a laboratory task is open.").format(doc.name),
				IllegalVisitTransitionError,
			)

	return to_status


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
		actions.append(
			{
				"action": action,
				"label": rule.label,
				"needs_department": rule.needs_department,
				"needs_comment": rule.needs_comment,
				"is_primary": action in (ACTION_START_TRIAGE, ACTION_ROUTE, ACTION_START_CONSULTATION),
			}
		)
	return actions


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
) -> dict:
	"""Perform a visit action as the current user (form buttons, Vital Signs Route action)."""
	doc = transition(
		visit,
		action,
		department=department,
		comment=comment,
		without_vitals_reason=without_vitals_reason,
	)
	return {"name": doc.name, "status": doc.status, "current_department": doc.current_department}
