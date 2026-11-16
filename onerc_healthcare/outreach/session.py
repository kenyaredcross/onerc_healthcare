# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Outreach Session rules and lifecycle (OUT-03, SDD §5.6, D58). O1 owns this file.

W0 contract kept for O2 (sync): `members`, `is_member`, `assert_member(session, user)` and
`assert_open(session) -> row`.

**Planning rules** (`validate_session`, called by the controller):
- `facility` is the catchment area's facility (filled from it when blank) and a registration facility of this
  operation; `company` comes from the facility. A disabled catchment area cannot be chosen.
- `end_date` >= `start_date`.
- Team: at least one member, no user twice; every member is an enabled user holding the Outreach Team
  Member role (other roles are allowed too). With no lead ticked, the first member becomes the lead.
- Departments offered: at least one, no duplicates, only routable clinical departments: never
  `OUTREACH_EXCLUDED_DEPARTMENTS` (Mental Health, OI-7) nor Reception, Triage, Laboratory, Pharmacy or Health
  Information (`NON_CLINICAL_DEPARTMENTS`).
- Team, departments and dates change only while Planned or Active; facility and catchment area only while
  Planned (devices already hold the package once it is Active).
- A member removed from the team loses every device grant of this session at once.
- `status`, `cancellation_reason` and the status log change only through `transition` (D8: the log is
  append-only).

**Lifecycle** (`TRANSITIONS`; status is read-only):

| Action     | From     | To        | Who                                        | Rules |
|------------|----------|-----------|--------------------------------------------|-------|
| (insert)   | -        | Planned   | HIS Officer, Medical In-Charge (DocPerm)   | as above |
| `activate` | Planned  | Active    | system: first package download             | - |
| `return`   | Active   | Returned  | HIS Officer, Medical In-Charge, team lead  | records may still sync |
| `close`    | Returned | Closed    | HIS Officer, Medical In-Charge             | refused while an Outreach Sync Record of the session is Failed, Held or Conflict (protocol §6 F1) |
| `cancel`   | Planned  | Cancelled | HIS Officer, Medical In-Charge             | reason required; refused once the session has any Outreach Sync Record |

Planned, Active and Returned are open (`SESSION_OPEN_STATUSES`): devices may enrol, download and sync.
Closed means "no new work" (protocol §6 F1): the package and a new device's enrolment are refused, but the
grant keeps working for `sync.push` / `sync.status` until it expires and `enrol` may renew the existing grant of
the same user and device, so records captured during the session still arrive (`SYNC_STATUSES`). Cancelled
refuses everything. A session that received records is never cancelled (return and close it instead).
`allowed_actions` offers an action only when `transition` would accept it (the record checks included).
Administrator passes every role check; `system=True` (server code) skips it.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime

from onerc_healthcare.constants import (
	CATCHMENT_AREA_DOCTYPE,
	DEPT_HIS,
	DEPT_LABORATORY,
	DEPT_PHARMACY,
	DEPT_RECEPTION,
	DEPT_TRIAGE,
	OUTREACH_EXCLUDED_DEPARTMENTS,
	OUTREACH_MANAGER_ROLES,
	OUTREACH_RECORD_DOCTYPE,
	OUTREACH_SESSION_DOCTYPE,
	OUTREACH_TEAM_DOCTYPE,
	RECORD_STATUS_CONFLICT,
	RECORD_STATUS_FAILED,
	RECORD_STATUS_HELD,
	ROLE_OUTREACH,
	SESSION_ACTIVE,
	SESSION_CANCELLED,
	SESSION_CLOSED,
	SESSION_OPEN_STATUSES,
	SESSION_PLANNED,
	SESSION_RETURNED,
)

ACTION_CREATE = "create"
ACTION_ACTIVATE = "activate"
ACTION_RETURN = "return"
ACTION_CLOSE = "close"
ACTION_CANCEL = "cancel"

# Departments that are not a clinical service a team can offer (not routable, or a support service).
NON_CLINICAL_DEPARTMENTS = (DEPT_RECEPTION, DEPT_TRIAGE, DEPT_LABORATORY, DEPT_PHARMACY, DEPT_HIS)
NOT_OFFERED_DEPARTMENTS = tuple(OUTREACH_EXCLUDED_DEPARTMENTS) + NON_CLINICAL_DEPARTMENTS

# Statuses in which team, departments and dates may still change; facility / catchment only while Planned.
EDITABLE_STATUSES = (SESSION_PLANNED, SESSION_ACTIVE)
PLAN_FIELDS = ("start_date", "end_date")
SITE_FIELDS = ("facility", "catchment_area")
LOG_TABLE = "status_log"
REASON_MAX_LENGTH = 1000
# Sync Record statuses that keep a session from closing (protocol §6 F1; checked only when the doctype exists).
UNRESOLVED_RECORD_STATUSES = (RECORD_STATUS_FAILED, RECORD_STATUS_HELD, RECORD_STATUS_CONFLICT)
# Session statuses in which devices may still deliver records (sync.push / sync.status; protocol §6 F1).
SYNC_STATUSES = (*SESSION_OPEN_STATUSES, SESSION_CLOSED)


class IllegalSessionTransitionError(frappe.ValidationError):
	"""The action is not allowed from the session's current status."""


class OutreachSessionClosedError(frappe.ValidationError):
	"""The session is Closed or Cancelled and refuses this request (push, status, enrol, package). The message
	names the status in English ("Closed" / "Cancelled"), so the app can tell them apart besides `exc_type`."""


class OutreachSessionError(frappe.ValidationError):
	"""The session plan breaks a rule."""


@dataclass(frozen=True)
class SessionTransition:
	label: str
	from_statuses: tuple[str, ...]
	to_status: str
	# Roles allowed. Empty: a system event (server code or Administrator only).
	roles: tuple[str, ...] = ()
	# The session's team lead may also perform it.
	lead_allowed: bool = False
	needs_reason: bool = False

	@property
	def is_system(self) -> bool:
		return not self.roles and not self.lead_allowed


TRANSITIONS: dict[str, SessionTransition] = {
	ACTION_ACTIVATE: SessionTransition(_("Activate"), (SESSION_PLANNED,), SESSION_ACTIVE),
	ACTION_RETURN: SessionTransition(
		_("Mark Team Returned"),
		(SESSION_ACTIVE,),
		SESSION_RETURNED,
		tuple(OUTREACH_MANAGER_ROLES),
		lead_allowed=True,
	),
	ACTION_CLOSE: SessionTransition(
		_("Close Session"), (SESSION_RETURNED,), SESSION_CLOSED, tuple(OUTREACH_MANAGER_ROLES)
	),
	ACTION_CANCEL: SessionTransition(
		_("Cancel Session"),
		(SESSION_PLANNED,),
		SESSION_CANCELLED,
		tuple(OUTREACH_MANAGER_ROLES),
		needs_reason=True,
	),
}


# --------------------------------------------------------------------------------------------- W0 contract


def members(session: str) -> list[str]:
	return frappe.get_all(
		OUTREACH_TEAM_DOCTYPE,
		filters={"parent": session, "parenttype": OUTREACH_SESSION_DOCTYPE},
		pluck="user",
	)


def is_member(session: str, user: str | None = None) -> bool:
	user = user or frappe.session.user
	return bool(
		frappe.db.exists(
			OUTREACH_TEAM_DOCTYPE,
			{"parent": session, "parenttype": OUTREACH_SESSION_DOCTYPE, "user": user},
		)
	)


def assert_member(session: str, user: str | None = None) -> None:
	"""PermissionError unless `user` (default: session user) is on the session team."""
	if not session or not frappe.db.exists(OUTREACH_SESSION_DOCTYPE, session):
		frappe.throw(_("Outreach Session {0} not found.").format(session or ""), frappe.DoesNotExistError)
	if not is_member(session, user):
		frappe.throw(
			_("You are not on the team of outreach session {0}.").format(session), frappe.PermissionError
		)


def _session_row(session: str) -> frappe._dict:
	row = frappe.db.get_value(
		OUTREACH_SESSION_DOCTYPE,
		session,
		["name", "status", "facility", "company", "start_date", "end_date", "catchment_area"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("Outreach Session {0} not found.").format(session or ""), frappe.DoesNotExistError)
	return row


def assert_open(session: str) -> frappe._dict:
	"""The session row ({name, status, facility, company, start_date, end_date, catchment_area}); refuses a
	Closed or Cancelled session."""
	row = _session_row(session)
	if row.status not in SESSION_OPEN_STATUSES:
		frappe.throw(
			_("Outreach session {0} is {1}; it no longer accepts records.").format(session, row.status),
			OutreachSessionClosedError,
			title=_("Session Closed"),
		)
	return row


def assert_syncable(session: str) -> frappe._dict:
	"""The session row when devices may still deliver records (open or Closed, protocol §6 F1); refuses a
	Cancelled session."""
	row = _session_row(session)
	if row.status not in SYNC_STATUSES:
		frappe.throw(
			_("Outreach session {0} is {1}; it no longer accepts records.").format(session, row.status),
			OutreachSessionClosedError,
			title=_("Session Closed"),
		)
	return row


# ------------------------------------------------------------------------------------------------ helpers


def is_lead(session: str, user: str | None = None) -> bool:
	user = user or frappe.session.user
	return bool(
		frappe.db.exists(
			OUTREACH_TEAM_DOCTYPE,
			{"parent": session, "parenttype": OUTREACH_SESSION_DOCTYPE, "user": user, "is_lead": 1},
		)
	)


def is_manager(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user == "Administrator" or bool(set(OUTREACH_MANAGER_ROLES) & set(frappe.get_roles(user)))


def session_departments(session: str) -> list[str]:
	"""Departments the session offers, in form order, never a department in `NOT_OFFERED_DEPARTMENTS`."""
	from onerc_healthcare.constants import OUTREACH_DEPARTMENT_DOCTYPE

	rows = frappe.get_all(
		OUTREACH_DEPARTMENT_DOCTYPE,
		filters={"parent": session, "parenttype": OUTREACH_SESSION_DOCTYPE},
		pluck="department",
		order_by="idx asc",
	)
	return [d for d in dict.fromkeys(rows) if d and d not in NOT_OFFERED_DEPARTMENTS]


def may_act(rule: SessionTransition, doc, user: str | None = None) -> bool:
	"""Role (or team lead) check only; the status is checked separately."""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	if rule.is_system:
		return False
	if set(rule.roles) & set(frappe.get_roles(user)):
		return True
	return rule.lead_allowed and any(r.user == user and r.is_lead for r in (doc.get("team") or []))


def allowed_actions(doc, user: str | None = None) -> list[dict]:
	"""User actions offered on the form: [{action, label, needs_reason}]. An action `transition` would refuse
	because of the session's sync records (close, cancel) is not offered (DEFECT-P4-1)."""
	return [
		{"action": action, "label": rule.label, "needs_reason": rule.needs_reason}
		for action, rule in TRANSITIONS.items()
		if not rule.is_system
		and doc.status in rule.from_statuses
		and may_act(rule, doc, user)
		and not record_block(doc.name, action)
	]


def _record_count(session: str, statuses: tuple[str, ...] | None = None) -> int:
	if not session or not frappe.db.table_exists(OUTREACH_RECORD_DOCTYPE):
		return 0
	meta = frappe.get_meta(OUTREACH_RECORD_DOCTYPE)
	if not (meta.has_field("session") and meta.has_field("status")):
		return 0
	filters: dict = {"session": session}
	if statuses:
		filters["status"] = ["in", list(statuses)]
	return frappe.db.count(OUTREACH_RECORD_DOCTYPE, filters)


def unresolved_sync_records(session: str) -> int:
	"""Outreach Sync Records of `session` still Failed, Held or in Conflict (0 while the doctype does not exist)."""
	return _record_count(session, UNRESOLVED_RECORD_STATUSES)


def sync_records(session: str) -> int:
	"""Every Outreach Sync Record of `session`, whatever its status."""
	return _record_count(session)


def record_block(session: str, action: str) -> str | None:
	"""Why the session's sync records forbid `action` (close, cancel), else None."""
	if action == ACTION_CLOSE:
		pending = unresolved_sync_records(session)
		if pending:
			return _(
				"{0} synced record(s) of this session are still failed, held or in conflict. Have the team "
				"correct and send the failed records again, and resolve the Sync Conflicts, before closing "
				"the session."
			).format(pending)
	elif action == ACTION_CANCEL:
		received = sync_records(session)
		if received:
			return _(
				"{0} record(s) were received from this session's devices, so it cannot be cancelled. Return "
				"and close it instead (it becomes Active when the team downloads the package)."
			).format(received)
	return None


# --------------------------------------------------------------------------------------------- validation


def validate_session(doc) -> None:
	"""Controller validate (see module docstring)."""
	from onerc_healthcare.privacy.lifecycle import guard_fields, guard_log

	before = None if doc.is_new() else doc.get_doc_before_save()
	if doc.is_new():
		if not doc.flags.from_workflow:
			doc.status = SESSION_PLANNED
			doc.cancellation_reason = None
			doc.set(LOG_TABLE, [])
			_append_log(doc, "", SESSION_PLANNED, ACTION_CREATE)
	elif before is not None:
		guard_fields(doc, before, ("status", "cancellation_reason"))
		guard_log(doc, before, LOG_TABLE)
		_guard_plan_edits(doc, before)

	if before is not None and doc.flags.from_workflow:
		return  # a lifecycle action changes only the status, its reason and the log
	doc.title = (doc.title or "").strip()
	# Re-check only what is new or changed: a member disabled after planning must not block other edits.
	if before is None or any((doc.get(f) or None) != (before.get(f) or None) for f in SITE_FIELDS):
		_validate_site(doc, before)
	_validate_dates(doc)
	if before is None or _team_key(doc.get("team")) != _team_key(before.get("team")):
		_validate_team(doc)
	if before is None or _dept_key(doc.get("departments")) != _dept_key(before.get("departments")):
		_validate_departments(doc)


def _append_log(doc, from_status: str, to_status: str, action: str, comment: str | None = None) -> None:
	doc.append(
		LOG_TABLE,
		{
			"from_status": from_status,
			"to_status": to_status,
			"action": action,
			"user": frappe.session.user,
			"timestamp": now_datetime(),
			"comment": comment,
		},
	)


def _team_key(rows) -> list[tuple]:
	return [(r.get("user"), int(r.get("is_lead") or 0)) for r in rows or []]


def _dept_key(rows) -> list[str]:
	return [r.get("department") for r in rows or []]


def _guard_plan_edits(doc, before) -> None:
	if doc.flags.from_workflow:
		return
	status = before.status
	changed = []
	if status != SESSION_PLANNED:
		changed += [f for f in SITE_FIELDS if (doc.get(f) or None) != (before.get(f) or None)]
	if status not in EDITABLE_STATUSES:
		changed += [f for f in PLAN_FIELDS if (getdate(doc.get(f)) if doc.get(f) else None) != before.get(f)]
		if _team_key(doc.get("team")) != _team_key(before.get("team")):
			changed.append("team")
		if _dept_key(doc.get("departments")) != _dept_key(before.get("departments")):
			changed.append("departments")
	if changed:
		frappe.throw(
			_("{0} cannot change while the session is {1}.").format(
				", ".join(frappe.bold(_(doc.meta.get_label(f))) for f in changed), frappe.bold(_(status))
			),
			OutreachSessionError,
			title=_("Not Editable"),
		)


def _validate_site(doc, before) -> None:
	from onerc_healthcare.emr_outreach.doctype.catchment_area.catchment_area import (
		validate_operation_facility,
	)

	area = frappe.db.get_value(
		CATCHMENT_AREA_DOCTYPE, doc.catchment_area, ["name", "facility", "disabled"], as_dict=True
	)
	if not area:
		frappe.throw(_("Select the catchment area."), OutreachSessionError)
	if area.disabled and (before is None or before.catchment_area != doc.catchment_area):
		frappe.throw(
			_("Catchment area {0} is disabled.").format(frappe.bold(doc.catchment_area)), OutreachSessionError
		)
	if not doc.facility:
		doc.facility = area.facility
	if doc.facility != area.facility:
		frappe.throw(
			_("The session facility must be the catchment area's facility, {0}.").format(
				frappe.bold(area.facility)
			),
			OutreachSessionError,
			title=_("Wrong Facility"),
		)
	validate_operation_facility(doc.facility)
	doc.company = frappe.db.get_value("Healthcare Service Unit", doc.facility, "company")


def _validate_dates(doc) -> None:
	if doc.start_date and doc.end_date and getdate(doc.end_date) < getdate(doc.start_date):
		frappe.throw(_("The end date cannot be before the start date."), OutreachSessionError)


def _validate_team(doc) -> None:
	rows = [r for r in doc.get("team") or [] if r.user]
	if not rows:
		frappe.throw(_("Add at least one team member."), OutreachSessionError, title=_("Team Required"))
	users = [r.user for r in rows]
	duplicates = sorted({u for u in users if users.count(u) > 1})
	if duplicates:
		frappe.throw(
			_("{0} is on the team more than once.").format(", ".join(frappe.bold(u) for u in duplicates)),
			OutreachSessionError,
		)
	enabled = set(frappe.get_all("User", filters={"name": ["in", users], "enabled": 1}, pluck="name"))
	with_role = set(
		frappe.get_all(
			"Has Role",
			filters={"parent": ["in", users], "parenttype": "User", "role": ROLE_OUTREACH},
			pluck="parent",
		)
	)
	invalid = [u for u in users if u not in enabled or u not in with_role]
	if invalid:
		frappe.throw(
			_("{0}: every team member must be an enabled user with the {1} role.").format(
				", ".join(frappe.bold(u) for u in invalid), frappe.bold(_(ROLE_OUTREACH))
			),
			OutreachSessionError,
			title=_("Invalid Team Member"),
		)
	if not any(int(r.is_lead or 0) for r in rows):
		rows[0].is_lead = 1


def _validate_departments(doc) -> None:
	names = [r.department for r in doc.get("departments") or [] if r.department]
	if not names:
		frappe.throw(
			_("Add at least one department the team offers."),
			OutreachSessionError,
			title=_("Departments Required"),
		)
	duplicates = sorted({d for d in names if names.count(d) > 1})
	if duplicates:
		frappe.throw(
			_("{0} is listed more than once.").format(", ".join(frappe.bold(d) for d in duplicates)),
			OutreachSessionError,
		)
	refused = [d for d in names if d in NOT_OFFERED_DEPARTMENTS]
	if refused:
		frappe.throw(
			_("{0} cannot be offered by an outreach team (Mental Health is never offered offline).").format(
				", ".join(frappe.bold(_(d)) for d in refused)
			),
			OutreachSessionError,
			title=_("Department Not Allowed"),
		)


def on_session_update(doc) -> None:
	"""Controller on_update: a member taken off the team loses this session's device grants."""
	before = doc.get_doc_before_save()
	if not before:
		return
	removed = {r.user for r in before.get("team") or []} - {r.user for r in doc.get("team") or []}
	if removed:
		from onerc_healthcare.outreach.devices import revoke_grants

		revoke_grants(
			{"session": doc.name, "user": ["in", sorted(removed)]},
			_("Removed from the team of outreach session {0}").format(doc.name),
		)


# ---------------------------------------------------------------------------------------------- lifecycle


def transition(session: str, action: str, reason: str | None = None, system: bool = False):
	"""Apply `action` to `session` as the current user (or as the system). Returns the saved document."""
	rule = TRANSITIONS.get(action)
	if not rule:
		frappe.throw(_("Unknown session action: {0}").format(action), IllegalSessionTransitionError)
	doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, session, for_update=True)
	user = frappe.session.user
	if not system:
		if not may_act(rule, doc, user):
			frappe.throw(
				_("You are not allowed to {0} outreach session {1}.").format(rule.label.lower(), doc.name)
				if not rule.is_system
				else _("{0} is a system event and cannot be performed by a user.").format(
					frappe.bold(rule.label)
				),
				frappe.PermissionError,
			)
		if user != "Administrator" and not doc.has_permission("read"):
			frappe.throw(_("You do not have access to {0}.").format(doc.name), frappe.PermissionError)
	if doc.status not in rule.from_statuses:
		frappe.throw(
			_("{0} is not allowed: outreach session {1} is {2}.").format(
				frappe.bold(rule.label), doc.name, frappe.bold(_(doc.status))
			),
			IllegalSessionTransitionError,
		)
	reason = (reason or "").strip() or None
	if rule.needs_reason and not reason:
		frappe.throw(
			_("Enter a reason for {0}.").format(frappe.bold(rule.label)),
			IllegalSessionTransitionError,
			title=_("Reason Required"),
		)
	if reason and len(reason) > REASON_MAX_LENGTH:
		frappe.throw(
			_("The reason is {0} characters long; the limit is {1}.").format(len(reason), REASON_MAX_LENGTH),
			IllegalSessionTransitionError,
		)
	blocked = record_block(doc.name, action)
	if blocked:
		frappe.throw(
			blocked,
			IllegalSessionTransitionError,
			title=_("Unresolved Records") if action == ACTION_CLOSE else _("Records Received"),
		)
	if action == ACTION_CANCEL:
		doc.cancellation_reason = reason
	_append_log(doc, doc.status, rule.to_status, action, reason)
	doc.status = rule.to_status
	doc.flags.from_workflow = True
	try:
		doc.save(ignore_permissions=True)
	finally:
		doc.flags.from_workflow = False
	return doc


def activate_on_download(session: str) -> bool:
	"""System: the first package download moves a Planned session to Active. Returns True when it did."""
	status = frappe.db.get_value(OUTREACH_SESSION_DOCTYPE, session, "status", for_update=True)
	if status != SESSION_PLANNED:
		return False
	transition(session, ACTION_ACTIVATE, system=True)
	return True


@frappe.whitelist(methods=["POST"])
def apply_action(session: str, action: str, reason: str | None = None) -> dict:
	"""Perform a user action on an outreach session (see TRANSITIONS)."""
	if action not in TRANSITIONS or TRANSITIONS[action].is_system:
		frappe.throw(_("Unknown session action: {0}").format(action), IllegalSessionTransitionError)
	doc = transition(session, action, reason=reason)
	return {"name": doc.name, "status": doc.status}
