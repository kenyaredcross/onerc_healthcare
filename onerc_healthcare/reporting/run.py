# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MOH Report Run lifecycle (FR-30, SDD §5.8 "Snapshots and export"). Status changes only through here (D8).

| Action       | From              | To         | Roles       | Requires | Effect                                    |
|--------------|-------------------|------------|-------------|----------|-------------------------------------------|
| `create`     | -                 | Draft      | HIS Officer | -        | desk insert; one active run per period    |
| `generate`   | Draft, Generated  | Generated  | HIS Officer | -        | evaluates, stores the snapshot, XLSX, PDF |
| `submit_run` | Generated         | Submitted  | HIS Officer | -        | marked as submitted to the MOH; frozen    |
| `revise`     | Submitted         | Superseded | HIS Officer | reason   | new Draft run that `supersedes` this one  |
| `void`       | Draft, Generated  | Void       | HIS Officer | reason   | frees the period for a new run            |

- Administrator passes every role check. The Medical In-Charge and the Head of Operations only read
  (the Head of Operations does not see the exceptions, which sit at permlevel 1).
- **One active run** (not Void, not Superseded) per report, facility and period: checked on insert
  under a lock on the MOH Report row, so two concurrent creations cannot both pass.
- **Generation** is synchronous, or enqueued on the ``long`` queue when the period is longer than
  `LONG_RANGE_DAYS`. While queued, ``generation_status`` is "Queued" (the form says "being
  generated"), and Generate and Mark Submitted are refused; the job sets "Failed" with the error
  if it fails. Generating a run regenerates it: a new snapshot and new XLSX and PDF files (earlier
  files stay attached; the Excel and PDF fields point at the current ones).
- A Blocked report cannot be generated (nor a run created for it).
- Submitted, Superseded and Void runs are frozen: nothing changes except through an action. Nothing is
  ever deleted (the run is in `CLINICAL_DOCTYPES`).
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import cstr, now_datetime

from onerc_healthcare.constants import (
	MOH_REPORT_DOCTYPE,
	MOH_RUN_DOCTYPE,
	ROLE_HEAD_OPS,
	ROLE_HIS,
	ROLE_MIC,
	USER_DESIGNATION_FIELD,
)
from onerc_healthcare.reporting import engine
from onerc_healthcare.reporting.period import period_days

STATUS_DRAFT = "Draft"
STATUS_GENERATED = "Generated"
STATUS_SUBMITTED = "Submitted"
STATUS_SUPERSEDED = "Superseded"
STATUS_VOID = "Void"
STATUSES = (STATUS_DRAFT, STATUS_GENERATED, STATUS_SUBMITTED, STATUS_SUPERSEDED, STATUS_VOID)
INACTIVE_STATUSES = (STATUS_VOID, STATUS_SUPERSEDED)
FROZEN_STATUSES = (STATUS_SUBMITTED, STATUS_SUPERSEDED, STATUS_VOID)
READABLE_VALUE_STATUSES = (STATUS_GENERATED, STATUS_SUBMITTED)

ACTION_CREATE = "create"
ACTION_GENERATE = "generate"
ACTION_SUBMIT = "submit_run"
ACTION_REVISE = "revise"
ACTION_VOID = "void"

GENERATION_QUEUED = "Queued"
GENERATION_FAILED = "Failed"
LONG_RANGE_DAYS = 92  # longer periods are generated on the long queue
JOB_TIMEOUT = 60 * 60
JOB_SAVEPOINT = "krcs_moh_generation"
MAX_EXCEPTION_ROWS = 1000
MAX_TEXT_LENGTH = 1000
# Not offered on runs of a Blocked report: generate refuses it, and revise creates a run for it.
BLOCKED_REPORT_ACTIONS = (ACTION_GENERATE, ACTION_REVISE)

RUN_ROLES = (ROLE_HIS,)
READ_ROLES = (ROLE_HIS, ROLE_MIC, ROLE_HEAD_OPS)
REALTIME_EVENT = "krcs_moh_run_generated"


@dataclass(frozen=True)
class Transition:
	label: str
	from_statuses: tuple[str, ...]  # empty for the creating action
	to_status: str
	roles: tuple[str, ...]
	required: tuple[str, ...] = ()


TRANSITIONS: dict[str, Transition] = {
	ACTION_CREATE: Transition(_("Create"), (), STATUS_DRAFT, RUN_ROLES),
	ACTION_GENERATE: Transition(_("Generate"), (STATUS_DRAFT, STATUS_GENERATED), STATUS_GENERATED, RUN_ROLES),
	ACTION_SUBMIT: Transition(_("Mark Submitted"), (STATUS_GENERATED,), STATUS_SUBMITTED, RUN_ROLES),
	ACTION_REVISE: Transition(
		_("Revise"), (STATUS_SUBMITTED,), STATUS_SUPERSEDED, RUN_ROLES, required=("reason",)
	),
	ACTION_VOID: Transition(
		_("Void"), (STATUS_DRAFT, STATUS_GENERATED), STATUS_VOID, RUN_ROLES, required=("reason",)
	),
}

# Set only by this module (refused in a normal save).
PROTECTED_FIELDS = (
	"status",
	"company",
	"period_label",
	"report_version",
	"report_status",
	"generation_status",
	"generation_error",
	"queued_at",
	"generated_at",
	"generated_by",
	"submitted_at",
	"submitted_by",
	"snapshot",
	"exception_count",
	"xlsx_file",
	"pdf_file",
	"supersedes",
	"superseded_by",
	"void_reason",
	"voided_by",
	"voided_at",
	"krcs_archived",
)
# Chosen on creation, then fixed (the snapshot belongs to them).
PARAMETER_FIELDS = ("report", "facility", "period_type", "from_date", "to_date")
# Editable by the HIS Officer while Draft or Generated; they appear on the exports.
EDITABLE_FIELDS = (
	"compiled_by",
	"compiled_by_designation",
	"narrative",
	"photo_1",
	"photo_2",
	"photo_3",
)
EXCEPTION_FIELDS = ("kind", "element", "detail", "reference_doctype", "reference_name")
LOG_FIELDS = ("from_status", "to_status", "action", "user", "timestamp", "comment")


class IllegalTransitionError(frappe.ValidationError):
	"""The action is not allowed from the run's status (or while it is being generated)."""


class LockedFieldError(frappe.ValidationError):
	"""A protected field, a frozen run or a history row was changed outside the actions."""


class DuplicateRunError(frappe.ValidationError):
	"""Another active run exists for the same report, facility and period."""


class ReportBlockedError(frappe.ValidationError):
	"""The report definition is Blocked and cannot be generated."""


# --- roles and checks ---------------------------------------------------------------------------------


def has_any_role(roles, user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user == "Administrator" or bool(set(roles) & set(frappe.get_roles(user)))


def check_role(rule: Transition, doc=None, system: bool = False) -> None:
	if system or frappe.session.user == "Administrator":
		return
	if not has_any_role(rule.roles):
		frappe.throw(
			_("You are not allowed to {0} MOH report runs. Required role: {1}.").format(
				frappe.bold(rule.label), ", ".join(_(r) for r in rule.roles)
			),
			frappe.PermissionError,
		)
	if doc is not None and not doc.has_permission("read"):
		frappe.throw(_("You do not have access to {0}.").format(doc.name), frappe.PermissionError)


def check_from_status(rule: Transition, doc) -> None:
	if doc.status not in rule.from_statuses:
		frappe.throw(
			_("{0} is not allowed for {1} in status {2}.").format(
				frappe.bold(rule.label), doc.name, frappe.bold(_(doc.status))
			),
			IllegalTransitionError,
		)


def clean_text(value, label: str, required: bool = False) -> str | None:
	value = cstr(value).strip() or None
	if required and not value:
		frappe.throw(_("Enter the {0}.").format(_(label)), IllegalTransitionError, title=_("Missing Value"))
	if value and len(value) > MAX_TEXT_LENGTH:
		frappe.throw(
			_("{0} is {1} characters long; the limit is {2}.").format(_(label), len(value), MAX_TEXT_LENGTH),
			IllegalTransitionError,
		)
	return value


def append_log(doc, from_status: str | None, to_status: str, action: str, comment: str | None = None):
	doc.append(
		"log",
		{
			"from_status": from_status,
			"to_status": to_status,
			"action": action,
			"user": frappe.session.user,
			"timestamp": now_datetime(),
			"comment": comment,
		},
	)


def save_from_workflow(doc) -> None:
	"""Save an action's changes. Links are not re-validated: exception rows reference records by ID
	(they may be archived or merged since), and the run's own links were validated on insert."""
	doc.flags.from_workflow = True
	doc.flags.ignore_links = True
	try:
		doc.save(ignore_permissions=True)
	finally:
		doc.flags.from_workflow = False
		doc.flags.ignore_links = False


def assert_generatable(report) -> None:
	if report.status == engine.REPORT_STATUS_BLOCKED:
		frappe.throw(
			_("{0} is Blocked and cannot be generated: {1}").format(
				report.name, report.blocked_reason or _("no reason given")
			),
			ReportBlockedError,
		)


def assert_single_active(doc) -> None:
	"""One active run per report, facility and period (a revision may coexist with the run it
	supersedes until that run is marked Superseded in the same transaction)."""
	frappe.db.sql(f"select name from `tab{MOH_REPORT_DOCTYPE}` where name = %s for update", doc.report)
	# Locking read (review L3): the rows found stay locked until the transaction ends.
	others = frappe.db.sql_list(
		f"""select name from `tab{MOH_RUN_DOCTYPE}`
		where `report` = %(report)s and `facility` = %(facility)s and `from_date` = %(from_date)s
			and `to_date` = %(to_date)s and `status` not in %(inactive)s and `name` != %(name)s
			and `name` != %(supersedes)s
		for update""",
		{
			"report": doc.report,
			"facility": doc.facility,
			"from_date": doc.from_date,
			"to_date": doc.to_date,
			"inactive": INACTIVE_STATUSES,
			"name": doc.name or "",
			"supersedes": doc.supersedes or "",
		},
	)
	if others:
		frappe.throw(
			_("{0} already exists for {1}, {2}, {3}. Void it, or revise it if it was submitted.").format(
				frappe.bold(others[0]), doc.report, doc.facility, doc.period_label or doc.from_date
			),
			DuplicateRunError,
			title=_("Run Exists"),
		)


def report_is_blocked(report: str | None) -> bool:
	return bool(report) and frappe.db.get_value(MOH_REPORT_DOCTYPE, report, "status") == (
		engine.REPORT_STATUS_BLOCKED
	)


def is_queued(doc) -> bool:
	return doc.get("generation_status") == GENERATION_QUEUED


def get_available_actions(doc, user: str | None = None) -> list[dict]:
	"""Actions the user may take on `doc` now (for the form buttons; same checks as `transition`)."""
	out = []
	blocked = report_is_blocked(doc.report)
	for action, rule in TRANSITIONS.items():
		if action == ACTION_CREATE or doc.status not in rule.from_statuses:
			continue
		if action in (ACTION_GENERATE, ACTION_SUBMIT) and is_queued(doc):
			continue
		if action in BLOCKED_REPORT_ACTIONS and blocked:
			continue
		if not has_any_role(rule.roles, user):
			continue
		out.append({"action": action, "label": rule.label, "requires_reason": "reason" in rule.required})
	return out


# --- actions ------------------------------------------------------------------------------------------


def transition(run: str, action: str, system: bool = False, reason: str | None = None) -> dict:
	"""Apply `action` to MOH Report Run `run` as the current user. Returns a small result dict."""
	rule = TRANSITIONS.get(action)
	if not rule or action == ACTION_CREATE:
		frappe.throw(_("Unknown MOH report run action: {0}").format(action), IllegalTransitionError)
	doc = frappe.get_doc(MOH_RUN_DOCTYPE, run, for_update=True)
	check_role(rule, doc, system)
	check_from_status(rule, doc)
	reason = clean_text(reason, _("Reason"), required="reason" in rule.required)
	if action in (ACTION_GENERATE, ACTION_SUBMIT) and is_queued(doc):
		frappe.throw(
			_("{0} is being generated in the background. Wait until it finishes.").format(doc.name),
			IllegalTransitionError,
		)
	return _HANDLERS[action](doc, rule, reason)


def _generate(doc, rule: Transition, reason: str | None) -> dict:
	report = frappe.get_doc(MOH_REPORT_DOCTYPE, doc.report)
	assert_generatable(report)
	if period_days(doc.from_date, doc.to_date) > LONG_RANGE_DAYS:
		return _enqueue(doc)
	generate_now(doc, report)
	return {"run": doc.name, "status": doc.status, "queued": False}


def _enqueue(doc) -> dict:
	doc.generation_status = GENERATION_QUEUED
	doc.queued_at = now_datetime()
	doc.generation_error = None
	save_from_workflow(doc)
	frappe.enqueue(
		"onerc_healthcare.reporting.run.run_generation_job",
		queue="long",
		timeout=JOB_TIMEOUT,
		enqueue_after_commit=True,
		job_id=f"krcs-moh-run-{doc.name}",
		deduplicate=True,
		run=doc.name,
	)
	return {"run": doc.name, "status": doc.status, "queued": True}


def generate_now(doc, report=None) -> None:
	"""Evaluate the report, store the snapshot and exceptions, build the files, and log the change."""
	from onerc_healthcare.reporting.export_pdf import attach_pdf
	from onerc_healthcare.reporting.export_xlsx import attach_xlsx

	report = report or frappe.get_doc(MOH_REPORT_DOCTYPE, doc.report)
	assert_generatable(report)
	from_status = doc.status
	ctx = engine.build_context(doc, report)
	result = engine.evaluate_report(report, ctx)
	snapshot = engine.build_snapshot(doc, report, ctx, result)
	user = frappe.session.user

	doc.snapshot = engine.dumps(snapshot)
	doc.report_version = report.version
	doc.report_status = report.status
	doc.period_label = snapshot["period"]["label"]
	doc.generated_at = now_datetime()
	doc.generated_by = user
	if not doc.compiled_by and user not in ("Administrator", "Guest"):
		doc.compiled_by = user
	if doc.compiled_by:
		doc.compiled_by_name = frappe.db.get_value("User", doc.compiled_by, "full_name")
		if not doc.compiled_by_designation:
			doc.compiled_by_designation = designation_of(doc.compiled_by)
	_set_exceptions(doc, ctx.exceptions)
	doc.generation_status = None
	doc.generation_error = None
	doc.queued_at = None
	attach_xlsx(doc)
	attach_pdf(doc)
	doc.status = STATUS_GENERATED
	append_log(
		doc,
		from_status,
		STATUS_GENERATED,
		ACTION_GENERATE,
		_("Regenerated") if from_status == STATUS_GENERATED else None,
	)
	save_from_workflow(doc)


def designation_of(user: str | None) -> str | None:
	if not user or not frappe.get_meta("User").has_field(USER_DESIGNATION_FIELD):
		return None
	return frappe.db.get_value("User", user, USER_DESIGNATION_FIELD)


def _set_exceptions(doc, exceptions: list[dict]) -> None:
	doc.set("exceptions", [])
	for row in exceptions[:MAX_EXCEPTION_ROWS]:
		doc.append(
			"exceptions",
			{
				"kind": cstr(row.get("kind"))[:140],
				"element": cstr(row.get("element"))[:140] or None,
				"detail": cstr(row.get("detail"))[:MAX_TEXT_LENGTH],
				"reference_doctype": row.get("reference_doctype") or None,
				"reference_name": cstr(row.get("reference_name"))[:140] or None,
			},
		)
	if len(exceptions) > MAX_EXCEPTION_ROWS:
		doc.append(
			"exceptions",
			{
				"kind": engine.EXCEPTION_ENGINE,
				"detail": _("{0} more exceptions are not listed.").format(
					len(exceptions) - MAX_EXCEPTION_ROWS
				),
			},
		)
	doc.exception_count = len(exceptions)


def run_generation_job(run: str) -> None:
	"""Background generation (long queue). Runs as the user who asked for it."""
	doc = frappe.get_doc(MOH_RUN_DOCTYPE, run, for_update=True)
	if not is_queued(doc) or doc.status not in TRANSITIONS[ACTION_GENERATE].from_statuses:
		return  # voided or already handled meanwhile
	frappe.db.savepoint(JOB_SAVEPOINT)
	try:
		generate_now(doc)
	except Exception as e:
		frappe.db.rollback(save_point=JOB_SAVEPOINT)
		frappe.log_error(title=f"MOH report run {run} generation failed")
		frappe.db.set_value(
			MOH_RUN_DOCTYPE,
			run,
			{"generation_status": GENERATION_FAILED, "generation_error": cstr(e)[:MAX_TEXT_LENGTH]},
			update_modified=False,
		)
	frappe.publish_realtime(REALTIME_EVENT, {"run": run}, user=frappe.session.user, after_commit=True)


def _submit(doc, rule: Transition, reason: str | None) -> dict:
	doc.status = STATUS_SUBMITTED
	doc.submitted_at = now_datetime()
	doc.submitted_by = frappe.session.user
	append_log(doc, STATUS_GENERATED, STATUS_SUBMITTED, ACTION_SUBMIT, reason)
	save_from_workflow(doc)
	return {"run": doc.name, "status": doc.status}


def _revise(doc, rule: Transition, reason: str | None) -> dict:
	new = frappe.new_doc(MOH_RUN_DOCTYPE)
	for f in (*PARAMETER_FIELDS, "compiled_by", "compiled_by_designation", "narrative"):
		new.set(f, doc.get(f))
	new.supersedes = doc.name
	new.flags.from_workflow = True
	new.flags.creation_comment = _("Revision of {0}: {1}").format(doc.name, reason)
	new.insert(ignore_permissions=True)
	new.flags.from_workflow = False

	doc.status = STATUS_SUPERSEDED
	doc.superseded_by = new.name
	append_log(
		doc,
		STATUS_SUBMITTED,
		STATUS_SUPERSEDED,
		ACTION_REVISE,
		_("{0} (new run {1})").format(reason, new.name),
	)
	save_from_workflow(doc)
	return {"run": doc.name, "status": doc.status, "new_run": new.name}


def _void(doc, rule: Transition, reason: str | None) -> dict:
	from_status = doc.status
	doc.status = STATUS_VOID
	doc.void_reason = reason
	doc.voided_by = frappe.session.user
	doc.voided_at = now_datetime()
	doc.generation_status = None  # a queued job finds the run Void and stops
	doc.queued_at = None
	append_log(doc, from_status, STATUS_VOID, ACTION_VOID, reason)
	save_from_workflow(doc)
	return {"run": doc.name, "status": doc.status}


_HANDLERS = {
	ACTION_GENERATE: _generate,
	ACTION_SUBMIT: _submit,
	ACTION_REVISE: _revise,
	ACTION_VOID: _void,
}


@frappe.whitelist(methods=["POST"])
def apply_action(run: str, action: str, reason: str | None = None) -> dict:
	"""Form buttons: generate, submit_run, revise (reason), void (reason)."""
	return transition(run, action, reason=reason)


@frappe.whitelist(methods=["GET"])
def default_period(period_type: str = "Monthly") -> dict:
	"""The last complete month or quarter (new-run form defaults)."""
	from onerc_healthcare.reporting.period import PERIOD_CUSTOM, PERIOD_TYPES, previous_period

	if period_type not in PERIOD_TYPES or period_type == PERIOD_CUSTOM:
		period_type = PERIOD_TYPES[0]
	start, end = previous_period(period_type)
	return {"from_date": str(start), "to_date": str(end)}


def protected_field_names(doc) -> tuple[str, ...]:
	return tuple(f for f in PROTECTED_FIELDS if doc.meta.has_field(f))


def table_key(rows, fields) -> list[tuple]:
	return [tuple(cstr(r.get(f)) for f in fields) for r in rows or []]
