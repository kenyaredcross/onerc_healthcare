# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Outreach sync endpoints (Phase 4, O2; OUT-14..17, FR-24/25, NFR-8/11). Wire contract: docs/OUTREACH_PROTOCOL.md §4.

`push(session, device_id, batch_id, records)` (POST) and `status(session, uuids)` (GET). Both act as
``frappe.session.user`` (a cookie sign-in, or the device grant checked by O1's `outreach.auth` hook) and require
that user to be on the session team; `push` also requires an open session (Planned, Active or Returned).

How a call is handled:

- One Outreach Sync Batch row per ``batch_id`` and user; a re-sent call reuses (updates) the same row.
- At most ``max_batch`` records (EMR Outreach Settings, else ``OUTREACH_DEFAULTS``) per call.
- Every record gets exactly one result, in the order sent. Each runs in its own savepoint (and, outside tests,
  is committed on its own), so one failure never rolls back another, and a crash later in the call never
  loses a record already acknowledged.
- **Idempotency:** the Outreach Sync Record is named by the client UUID (unique) and read with
  ``SELECT ... FOR UPDATE``. A record already stored Accepted / Discarded returns ``duplicate``, Held returns
  ``held`` and Conflict returns ``conflict``, each with the stored result; nothing is applied twice. A record
  stored Failed is retried with the payload just received.
- **Dependencies** (``depends_on``): a dependency the server has not received, or one that failed, makes the
  record ``failed``; a dependency that is held or in conflict makes it ``held`` (stored with its payload and
  replayed by `outreach.conflicts` once the conflict is resolved).
- Records are applied by `outreach.apply` (the same pipelines as the desk) inside the outreach context.
- Error messages are plain text (HTML stripped) so the device can show them.

Outreach Sync Record status changes (`RECORD_TRANSITIONS`, append-only history in the document Version log):

| Event | From | To |
|---|---|---|
| receive (first time) | (none) | Accepted / Failed / Held / Conflict |
| retry (re-sent) | Failed | Accepted / Failed / Held / Conflict |
| replay (after a conflict decision) | Held | Accepted / Failed / Held / Conflict |
| resolve (`outreach.conflicts`) | Conflict | Accepted / Discarded |
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from html import unescape

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, cstr, get_datetime, getdate, now_datetime, strip_html

from onerc_healthcare.constants import (
	OUTREACH_BATCH_DOCTYPE,
	OUTREACH_RECORD_DOCTYPE,
	RECORD_STATUS_ACCEPTED,
	RECORD_STATUS_CONFLICT,
	RECORD_STATUS_DISCARDED,
	RECORD_STATUS_FAILED,
	RECORD_STATUS_HELD,
	RECORD_TYPES,
	RESULT_ACCEPTED,
	RESULT_CONFLICT,
	RESULT_DUPLICATE,
	RESULT_FAILED,
	RESULT_HELD,
)
from onerc_healthcare.outreach import apply, devices
from onerc_healthcare.outreach import session as outreach_session

FUTURE_TOLERANCE = timedelta(minutes=10)
DEVICE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
STATUS_MAX_UUIDS = 200
MAX_DEVICE_ID_LENGTH = 140
RATE_WINDOW = 60 * 60
PUSH_RATE_LIMIT = 600  # calls per user per hour (50 records each)
STATUS_RATE_LIMIT = 1200
SAVEPOINT = "krcs_outreach_record"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Stored status -> wire result: first answer (or `status`), and answer to a re-send.
STATUS_RESULT = {
	RECORD_STATUS_ACCEPTED: (RESULT_ACCEPTED, RESULT_DUPLICATE),
	RECORD_STATUS_DISCARDED: (RESULT_ACCEPTED, RESULT_DUPLICATE),
	RECORD_STATUS_FAILED: (RESULT_FAILED, RESULT_FAILED),
	RECORD_STATUS_HELD: (RESULT_HELD, RESULT_HELD),
	RECORD_STATUS_CONFLICT: (RESULT_CONFLICT, RESULT_CONFLICT),
}
_APPLIED = (RECORD_STATUS_ACCEPTED, RECORD_STATUS_FAILED, RECORD_STATUS_HELD, RECORD_STATUS_CONFLICT)
RECORD_TRANSITIONS: dict[str, tuple[tuple[str | None, ...], tuple[str, ...]]] = {
	"receive": ((None,), _APPLIED),
	"retry": ((RECORD_STATUS_FAILED,), _APPLIED),
	"replay": ((RECORD_STATUS_HELD,), _APPLIED),
	"resolve": ((RECORD_STATUS_CONFLICT,), (RECORD_STATUS_ACCEPTED, RECORD_STATUS_DISCARDED)),
}
# Final for the device (it may purge them `purge_after_days` after the acknowledgement).
FINAL_STATUSES = (RECORD_STATUS_ACCEPTED, RECORD_STATUS_DISCARDED)


# --------------------------------------------------------------------------------------------- endpoints


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=PUSH_RATE_LIMIT, seconds=RATE_WINDOW, ip_based=False, user_based=True)
def push(session: str, device_id: str, batch_id: str, records: list | str) -> dict:
	"""Receive one batch of offline records (protocol §4). Returns {"batch", "results"}."""
	user = frappe.session.user
	outreach_session.assert_member(session, user)
	outreach_session.assert_open(session)
	device_id = cstr(device_id).strip()
	devices.assert_request_grant(session, device_id)  # a device grant only syncs its own session and device
	if not device_id or len(device_id) > MAX_DEVICE_ID_LENGTH:
		frappe.throw(_("device_id is required."), frappe.ValidationError)
	batch_id = clean_uuid(batch_id)
	if not batch_id:
		frappe.throw(_("batch_id must be a UUID."), frappe.ValidationError)
	if isinstance(records, str):
		records = frappe.parse_json(records)
	if not isinstance(records, list):
		frappe.throw(_("records must be a list."), frappe.ValidationError)
	max_batch = apply.get_setting("max_batch")
	if len(records) > max_batch:
		frappe.throw(
			_("At most {0} records can be sent in one call; {1} were sent.").format(max_batch, len(records)),
			frappe.ValidationError,
			title=_("Batch Too Large"),
		)

	row = apply.session_row(session)
	batch = _open_batch(session, device_id, batch_id, user, len(records))
	call = frappe._dict(session=row, user=user, device_id=device_id, batch=batch.name)
	results = [receive(env, call) for env in records]
	_close_batch(batch.name, results)
	_commit()
	return {"batch": batch.name, "results": results}


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=STATUS_RATE_LIMIT, seconds=RATE_WINDOW, ip_based=False, user_based=True)
def status(session: str, uuids: list | str) -> dict:
	"""Current result of records sent earlier by this user in `session` (protocol §4). A held or conflicted
	record shows its final result once the conflict is resolved. Unknown UUIDs come back with ``result``
	null. The session may already be closed (read-only)."""
	user = frappe.session.user
	outreach_session.assert_member(session, user)
	devices.assert_request_grant(session)
	if isinstance(uuids, str):
		uuids = frappe.parse_json(uuids)
	if not isinstance(uuids, list):
		frappe.throw(_("uuids must be a list."), frappe.ValidationError)
	if len(uuids) > STATUS_MAX_UUIDS:
		frappe.throw(
			_("Ask for at most {0} records at a time.").format(STATUS_MAX_UUIDS), frappe.ValidationError
		)
	results = []
	for value in uuids:
		name = clean_uuid(value)
		row = _stored(name) if name else None
		if not row or row.user != user or row.session != session:
			results.append(_result(cstr(value), None, error=_("Not received by the server.")))
		else:
			results.append(stored_result(row))
	return {"results": results}


# --------------------------------------------------------------------------------------------- one record


def receive(env, call: frappe._dict) -> dict:
	"""Handle one envelope of a push call and return its wire result."""
	raw_uuid = env.get("client_uuid") if isinstance(env, dict) else None
	name = clean_uuid(raw_uuid)
	if not name:
		return _result(cstr(raw_uuid) or None, RESULT_FAILED, error=_("client_uuid must be a UUID."))

	existing = lock_record(name)
	if existing:
		if existing.user != call.user or existing.session != call.session.name:
			return _result(
				name,
				RESULT_FAILED,
				error=_("This record was already received from another user or outreach session."),
			)
		if existing.status != RECORD_STATUS_FAILED:
			return _touch(existing, call)

	errors, values = _read_envelope(env, call)
	if values is None:  # not attributable to this call (type or session/device): not stored
		return _result(name, RESULT_FAILED, error=" ".join(errors))
	doc = (
		frappe.get_doc(OUTREACH_RECORD_DOCTYPE, name) if existing else frappe.new_doc(OUTREACH_RECORD_DOCTYPE)
	)
	_fill(doc, name, values, call)
	return process(doc, call.session, errors)


def process(doc, session_row: frappe._dict, errors: list[str] | None = None) -> dict:
	"""Check the dependencies, apply the record and store the outcome on `doc` (an Outreach Sync Record,
	new or stored as Failed / Held), each in one savepoint. Returns the wire result."""
	is_new = doc.is_new()
	log_start = len(frappe.local.message_log)
	frappe.db.savepoint(SAVEPOINT)
	if is_new:
		try:
			doc.insert(ignore_permissions=True)  # claims the client UUID (unique name); see the report
		except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
			# Another call stored the same record meanwhile: answer with its result.
			frappe.db.rollback(save_point=SAVEPOINT)
			_restore_messages(log_start)
			row = lock_record(doc.name)
			return stored_result(row, duplicate=True)
	try:
		if errors:
			outcome = apply.Outcome(status=RECORD_STATUS_FAILED, error=" ".join(errors))
		else:
			outcome = check_dependencies(doc) or apply.apply_record(record_context(doc, session_row))
		_set_outcome(doc, outcome)
		doc.save(ignore_permissions=True)
	except Exception as exc:
		message = readable_error(exc, log_start)
		frappe.db.rollback(save_point=SAVEPOINT)
		if not isinstance(exc, frappe.ValidationError | frappe.PermissionError | frappe.DoesNotExistError):
			frappe.log_error(title=f"Outreach sync: record {doc.name} failed")
		doc = _failed(doc, is_new, message)
	finally:
		_restore_messages(log_start)
	_commit()
	return stored_result(doc)


def record_context(doc, session_row: frappe._dict, resolution: dict | None = None) -> frappe._dict:
	"""What `outreach.apply` needs from a stored Outreach Sync Record."""
	payload = doc.payload
	if isinstance(payload, str):
		payload = frappe.parse_json(payload) if payload else {}
	return frappe._dict(
		name=doc.name,
		record_type=doc.record_type,
		payload=payload,
		captured_at=get_datetime(doc.captured_at),
		session=session_row,
		user=doc.user,
		depends_on=depends_on(doc),
		resolution=resolution,
	)


def depends_on(doc) -> list[str]:
	value = doc.get("depends_on")
	if not value:
		return []
	if isinstance(value, str):
		value = json.loads(value)
	return list(value)


def check_dependencies(doc) -> apply.Outcome | None:
	"""Failed when a dependency is unknown or failed; Held (with the blocking conflict) when one is held or in
	conflict; None when every dependency has been applied (Accepted, or Discarded after a decision)."""
	held = None
	for dep_name in depends_on(doc):
		dep = frappe.db.get_value(
			OUTREACH_RECORD_DOCTYPE,
			dep_name,
			["name", "status", "session", "record_type", "error", "conflict"],
			as_dict=True,
		)
		if not dep or dep.session != doc.session:
			return apply.Outcome(
				status=RECORD_STATUS_FAILED,
				error=_("Dependency not received: record {0} must be synced first.").format(dep_name),
			)
		if dep.status == RECORD_STATUS_FAILED:
			return apply.Outcome(
				status=RECORD_STATUS_FAILED,
				error=_("The {0} record {1} this record needs failed: {2}").format(
					dep.record_type, dep_name, dep.error or ""
				),
			)
		if dep.status in (RECORD_STATUS_HELD, RECORD_STATUS_CONFLICT) and not held:
			held = apply.Outcome(status=RECORD_STATUS_HELD, conflict=dep.conflict)
	return held


# --------------------------------------------------------------------------------------------- helpers


def clean_uuid(value) -> str | None:
	"""The canonical (lower-case, hyphenated) form of a UUID string, else None."""
	text = cstr(value).strip().lower() if value else ""
	if not _UUID.match(text):
		return None
	try:
		return str(uuid.UUID(text))
	except ValueError:
		return None


def parse_device_time(value) -> datetime | None:
	"""A device time ``YYYY-MM-DD HH:mm:ss`` (site timezone), else None."""
	text = cstr(value).strip() if value else ""
	try:
		return datetime.strptime(text, DEVICE_TIME_FORMAT)
	except ValueError:
		return None


def lock_record(name: str) -> frappe._dict | None:
	"""The stored Outreach Sync Record, read with a row lock (``frappe.get_all`` has no FOR UPDATE)."""
	rows = frappe.db.sql(
		f"""SELECT name, status, user, session, record_type, target_doctype, target_name, conflict, error
		FROM `tab{OUTREACH_RECORD_DOCTYPE}` WHERE name = %s FOR UPDATE""",
		(name,),
		as_dict=True,
	)
	return rows[0] if rows else None


def _stored(name: str) -> frappe._dict | None:
	return frappe.db.get_value(
		OUTREACH_RECORD_DOCTYPE,
		name,
		["name", "status", "user", "session", "target_doctype", "target_name", "conflict", "error"],
		as_dict=True,
	)


def _read_envelope(env: dict, call: frappe._dict) -> tuple[list[str], dict | None]:
	"""(errors, values). values is None when the record cannot be stored under this call at all."""
	errors: list[str] = []
	record_type = cstr(env.get("type")).strip()
	if record_type not in RECORD_TYPES:
		return [_("Unknown record type {0}.").format(record_type or _("(none)"))], None
	if cstr(env.get("session")).strip() != call.session.name:
		return [_("The record belongs to another outreach session than this sync.")], None
	if cstr(env.get("device_id")).strip() != call.device_id:
		return [_("The record was sent with another device ID than this sync.")], None

	captured_at = parse_device_time(env.get("captured_at"))
	if not captured_at:
		errors.append(_("captured_at must be a date and time (YYYY-MM-DD HH:mm:ss)."))
	elif captured_at > now_datetime() + FUTURE_TOLERANCE:
		errors.append(_("captured_at {0} is in the future; check the device clock.").format(captured_at))
	elif call.session.start_date and getdate(captured_at) < getdate(call.session.start_date):
		errors.append(
			_("captured_at {0} is before the outreach session started ({1}).").format(
				captured_at, call.session.start_date
			)
		)
	deps = env.get("depends_on") or []
	clean_deps: list[str] = []
	if not isinstance(deps, list):
		errors.append(_("depends_on must be a list of record UUIDs."))
	else:
		for value in deps:
			dep = clean_uuid(value)
			if not dep:
				errors.append(_("depends_on holds {0}, which is not a UUID.").format(cstr(value)))
			elif dep not in clean_deps:
				clean_deps.append(dep)
	payload = env.get("payload")
	if isinstance(payload, str):
		try:
			payload = json.loads(payload)
		except ValueError:
			payload = None
	if not isinstance(payload, dict):
		errors.append(_("The record has no payload."))
		payload = {}
	return errors, {
		"record_type": record_type,
		"captured_at": captured_at,
		"depends_on": clean_deps,
		"payload": payload,
	}


def _fill(doc, name: str, values: dict, call: frappe._dict) -> None:
	now = now_datetime()
	doc.client_uuid = name
	doc.record_type = values["record_type"]
	doc.captured_at = values["captured_at"]
	doc.session = call.session.name
	doc.user = call.user
	doc.device_id = call.device_id
	doc.batch = call.batch
	doc.depends_on = json.dumps(values["depends_on"])
	doc.payload = json.dumps(values["payload"], default=str)
	doc.attempts = cint(doc.attempts) + 1
	doc.first_received_at = doc.first_received_at or now
	doc.last_received_at = now
	doc.status = doc.status or RECORD_STATUS_FAILED


def _set_outcome(doc, outcome: apply.Outcome) -> None:
	doc.status = outcome.status
	doc.error = outcome.get("error") or None
	doc.conflict = outcome.get("conflict") or None
	if outcome.get("doctype"):
		doc.target_doctype = outcome.doctype
		doc.target_name = outcome.name
	if outcome.get("patient"):
		doc.patient = outcome.patient
	if outcome.status in FINAL_STATUSES:
		doc.applied_at = now_datetime()
	doc.acknowledged_result = json.dumps(stored_result(doc))


def _failed(doc, is_new: bool, message: str):
	"""Store the record as Failed after its savepoint was rolled back (the in-memory `doc` is stale)."""
	values = {
		f: doc.get(f)
		for f in (
			"client_uuid",
			"record_type",
			"captured_at",
			"session",
			"user",
			"device_id",
			"batch",
			"depends_on",
			"payload",
			"attempts",
			"first_received_at",
			"last_received_at",
		)
	}
	fresh = (
		frappe.new_doc(OUTREACH_RECORD_DOCTYPE)
		if is_new
		else frappe.get_doc(OUTREACH_RECORD_DOCTYPE, doc.name)
	)
	fresh.update(values)
	fresh.status = RECORD_STATUS_FAILED
	fresh.error = message
	fresh.conflict = None
	fresh.acknowledged_result = json.dumps(stored_result(fresh))
	if is_new:
		fresh.insert(ignore_permissions=True)
	else:
		fresh.save(ignore_permissions=True)
	return fresh


def _touch(row: frappe._dict, call: frappe._dict) -> dict:
	"""A re-sent record that needs nothing more: count the receipt, answer with the stored result."""
	result = stored_result(row, duplicate=True)
	frappe.db.set_value(
		OUTREACH_RECORD_DOCTYPE,
		row.name,
		{
			"attempts": cint(frappe.db.get_value(OUTREACH_RECORD_DOCTYPE, row.name, "attempts")) + 1,
			"last_received_at": now_datetime(),
			"batch": call.batch,
			"acknowledged_result": json.dumps(result),
		},
		update_modified=False,
	)
	return result


def stored_result(row, duplicate: bool = False) -> dict:
	"""The wire result of a stored Outreach Sync Record (`row`: a document or dict)."""
	first, again = STATUS_RESULT[row.status]
	out = _result(
		row.name,
		again if duplicate else first,
		doctype=row.get("target_doctype"),
		name=row.get("target_name"),
		conflict=row.get("conflict"),
		error=row.get("error") if row.status == RECORD_STATUS_FAILED else None,
	)
	if row.status == RECORD_STATUS_DISCARDED:
		out["discarded"] = True
	return out


def _result(client_uuid, result, doctype=None, name=None, conflict=None, error=None) -> dict:
	return {
		"client_uuid": client_uuid,
		"result": result,
		"doctype": doctype or None,
		"name": name or None,
		"conflict": conflict or None,
		"error": error or None,
	}


def readable_error(exc: BaseException, log_start: int = 0) -> str:
	"""Plain-text message of a failed record: the messages Frappe queued while it failed (HTML removed), else
	the exception text."""
	entries = []
	for entry in frappe.local.message_log[log_start:]:
		if isinstance(entry, str):
			try:
				entry = json.loads(entry)
			except ValueError:
				entry = {"message": entry}
		if isinstance(entry, dict):
			entries.append(entry)
	# The messages that raised; warnings shown alongside (e.g. a missing consent) are not the failure.
	raised = [e for e in entries if e.get("raise_exception")]
	if not raised and not isinstance(exc, frappe.ValidationError | frappe.PermissionError):
		return _(
			"The server could not apply this record (error logged). Try again later or contact the HIS Officer."
		)
	parts: list[str] = []
	for entry in raised:
		message = entry.get("message")
		if isinstance(message, list | tuple):
			parts.extend(cstr(m) for m in message)
		elif message:
			parts.append(cstr(message))
	if not parts:
		parts = [cstr(exc) or exc.__class__.__name__]
	text = " ".join(_plain(p) for p in parts if p)
	return text[:1000] or _("The record could not be applied.")


def _plain(text: str) -> str:
	return " ".join(unescape(strip_html(cstr(text))).split())


def _restore_messages(log_start: int) -> None:
	"""Drop the messages queued while applying a record: each record reports through its own result."""
	frappe.local.message_log = frappe.local.message_log[:log_start]


def _commit() -> None:
	"""Commit each record on its own (no record loss if a later one crashes the call). Tests run inside a
	savepoint and never commit."""
	if not frappe.flags.in_test:
		frappe.db.commit()


# --------------------------------------------------------------------------------------------- batches


def _open_batch(session: str, device_id: str, batch_id: str, user: str, received: int):
	"""The Outreach Sync Batch of (user, batch_id), locked; created on the first call."""
	rows = frappe.db.sql(
		f"""SELECT name FROM `tab{OUTREACH_BATCH_DOCTYPE}` WHERE user = %s AND batch_id = %s FOR UPDATE""",
		(user, batch_id),
	)
	if rows:
		doc = frappe.get_doc(OUTREACH_BATCH_DOCTYPE, rows[0][0])
		if doc.session != session:
			frappe.throw(
				_("Batch {0} was sent for another outreach session.").format(batch_id), frappe.ValidationError
			)
	else:
		doc = frappe.new_doc(OUTREACH_BATCH_DOCTYPE)
		doc.update({"batch_id": batch_id, "session": session, "user": user, "started_at": now_datetime()})
	doc.device_id = device_id
	doc.calls = cint(doc.calls) + 1
	doc.received = received
	for field in ("accepted", "duplicate", "failed", "held", "conflict"):
		doc.set(field, 0)
	doc.finished_at = None
	if doc.is_new():
		doc.insert(ignore_permissions=True)  # server log; nobody has create on it
	else:
		doc.save(ignore_permissions=True)
	_commit()
	return doc


def _close_batch(name: str, results: list[dict]) -> None:
	counts = {"accepted": 0, "duplicate": 0, "failed": 0, "held": 0, "conflict": 0}
	for result in results:
		key = result.get("result")
		if key in counts:
			counts[key] += 1
	doc = frappe.get_doc(OUTREACH_BATCH_DOCTYPE, name)
	doc.update(counts)
	doc.finished_at = now_datetime()
	doc.save(ignore_permissions=True)
