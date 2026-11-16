# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Outreach devices and sync grants (OUT-07, OUT-08, NFR-8, DR-3, D58; protocol §1-2). O1 owns this file.

- `my_sessions` (GET): the open sessions (Planned, Active, Returned) the signed-in user is on.
- `enrol` (POST, online sign-in only): registers or refreshes the Outreach Device, and creates or refreshes the
  user's Outreach Device Grant for that device and session. Each call rotates the sync secret (returned once;
  only its salted SHA-256 is stored), sets `expires_at = now + offline_session_days` and escrows the device
  data key (Password field). A device holds one live grant per user per session; a revoked grant stays as
  history and a new one is issued next to it. A Revoked device cannot enrol. The user must hold the Outreach
  Team Member role. On a **Closed** session (protocol §6 F1) `enrol` only renews the live grant of the same
  user and device, so a device whose grant expired after the close can still deliver its records; a new device
  (or a revoked grant) is refused. A Cancelled session refuses every enrolment.
- `recover_key` (GET, online sign-in only): the escrowed data key, for the grant's own user, unless the grant
  or its device is revoked. Each recovery leaves an Info comment on the grant.
- `revoke_device` (POST, desk): HIS Officer, Medical In-Charge or EMR System Administrator, with a reason.
  The device becomes Revoked and every grant on it is revoked (lost-device procedure, OUT-25).

`verify_grant(name, secret, methods)` is the auth hook's check (`outreach.auth`). It refuses (401) a user who
no longer holds the Outreach Team Member role (protocol §6 F6), and accepts a Closed session only when every
method of the request is `sync.push` / `sync.status` (`CLOSED_SESSION_METHODS`, §6 F1). A request authenticated
by a device grant (`frappe.local.krcs_outreach_grant`) may never call `enrol`, `recover_key` or `my_sessions`;
the hook already refuses those paths, and the functions check again.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from onerc_healthcare.constants import (
	DEVICE_ACTIVE,
	DEVICE_REVOKED,
	OUTREACH_DEVICE_DOCTYPE,
	OUTREACH_GRANT_DOCTYPE,
	OUTREACH_MANAGER_ROLES,
	OUTREACH_SESSION_DOCTYPE,
	OUTREACH_TEAM_DOCTYPE,
	ROLE_OUTREACH,
	ROLE_SYSADMIN,
	SESSION_CLOSED,
	SESSION_OPEN_STATUSES,
)
from onerc_healthcare.outreach.auth import GRANT_LOCAL_KEY
from onerc_healthcare.outreach.session import (
	OutreachSessionClosedError,
	assert_member,
	assert_syncable,
	session_departments,
)

REVOKE_ROLES = (*OUTREACH_MANAGER_ROLES, ROLE_SYSADMIN)
# Settings the app needs (protocol §2, enrol response)
CLIENT_SETTINGS = (
	"offline_session_days",
	"purge_after_days",
	"pin_min_length",
	"max_pin_attempts",
	"max_batch",
	"summary_visits",
)
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,63}$")
DATA_KEY_BYTES = 32  # AES-256
SECRET_BYTES = 32
LABEL_MAX_LENGTH = 140
REASON_MAX_LENGTH = 1000
# The only methods a grant may call while its session is Closed (protocol §6 F1: records still arrive).
CLOSED_SESSION_METHODS = frozenset(
	("onerc_healthcare.outreach.sync.push", "onerc_healthcare.outreach.sync.status")
)


class DeviceEnrolmentError(frappe.ValidationError):
	"""The enrolment request is malformed."""


# ------------------------------------------------------------------------------------------- shared helpers


def grant_from_request() -> str | None:
	"""Name of the device grant that authenticated this request (set by `outreach.auth`), or None."""
	return getattr(frappe.local, GRANT_LOCAL_KEY, None)


def assert_request_grant(session: str, device_id: str | None = None) -> None:
	"""When this request was authenticated by a device grant, refuse a `session` (or `device_id`) other than the
	grant's own. A cookie request passes (session membership is checked separately). For O2's sync code too."""
	grant = grant_from_request()
	if not grant:
		return
	row = frappe.db.get_value(OUTREACH_GRANT_DOCTYPE, grant, ["session", "device"], as_dict=True)
	if not row or row.session != session:
		frappe.throw(_("This device grant is for another outreach session."), frappe.PermissionError)
	if device_id is not None and row.device != device_id:
		frappe.throw(_("This device grant belongs to another device."), frappe.PermissionError)


def assert_online_sign_in() -> None:
	"""Refuse Guest, and refuse a request authenticated by a device grant (protocol §1: online only)."""
	if frappe.session.user in (None, "", "Guest"):
		frappe.throw(_("Sign in to continue."), frappe.AuthenticationError)
	if grant_from_request():
		frappe.throw(
			_("This action needs an online sign-in; a device grant cannot be used for it."),
			frappe.PermissionError,
		)


def has_outreach_role(user: str) -> bool:
	"""The user still holds the Outreach Team Member role (protocol §6 F6)."""
	return bool(frappe.db.exists("Has Role", {"parent": user, "parenttype": "User", "role": ROLE_OUTREACH}))


def hash_secret(secret: str, salt: str) -> str:
	return hashlib.sha256(f"{salt}:{secret}".encode()).hexdigest()


def _norm(df, value):
	if df and df.fieldtype in ("Datetime", "Date") and value:
		return get_datetime(value)
	if df and df.fieldtype in ("Check", "Int"):
		return cint(value)
	return value or None


def guard_server_fields(doc, fields, password_fields=()) -> None:
	"""Controller validate for Outreach Device / Grant: only this module creates or changes them
	(`doc.flags.from_outreach`)."""
	if doc.flags.from_outreach:
		return
	if doc.is_new():
		frappe.throw(
			_("{0} records are created by enrolment in the outreach app.").format(_(doc.doctype)),
			frappe.PermissionError,
		)
	before = doc.get_doc_before_save()
	changed = [
		f
		for f in fields
		if before and _norm(doc.meta.get_field(f), doc.get(f)) != _norm(doc.meta.get_field(f), before.get(f))
	]
	changed += [f for f in password_fields if doc.get(f) and set(doc.get(f)) != {"*"}]
	if changed:
		frappe.throw(
			_("{0} can only change through enrolment or Revoke Device.").format(
				", ".join(frappe.bold(_(doc.meta.get_label(f))) for f in changed)
			),
			frappe.PermissionError,
			title=_("Not Editable"),
		)


def _save(doc):
	doc.flags.from_outreach = True
	try:
		if doc.is_new():
			doc.insert(ignore_permissions=True)
		else:
			doc.save(ignore_permissions=True)
	finally:
		doc.flags.from_outreach = False
	return doc


# ---------------------------------------------------------------------------------------------- endpoints


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=120, seconds=60 * 60, ip_based=False, user_based=True)
def my_sessions() -> list[dict]:
	"""Open sessions (Planned, Active, Returned) the signed-in user is on, newest start first."""
	assert_online_sign_in()
	user = frappe.session.user
	names = frappe.get_all(
		OUTREACH_TEAM_DOCTYPE,
		filters={"parenttype": OUTREACH_SESSION_DOCTYPE, "user": user},
		pluck="parent",
	)
	if not names:
		return []
	rows = frappe.get_all(
		OUTREACH_SESSION_DOCTYPE,
		filters={"name": ["in", names], "status": ["in", list(SESSION_OPEN_STATUSES)]},
		fields=["name", "title", "facility", "start_date", "end_date", "catchment_area", "status"],
		order_by="start_date desc, name desc",
	)
	facility_names = dict(
		frappe.get_all(
			"Healthcare Service Unit",
			filters={"name": ["in", list({r.facility for r in rows}) or [""]]},
			fields=["name", "healthcare_service_unit_name"],
			as_list=True,
		)
	)
	return [
		{
			"name": r.name,
			"title": r.title,
			"facility": r.facility,
			"facility_name": facility_names.get(r.facility) or r.facility,
			"start_date": str(r.start_date) if r.start_date else None,
			"end_date": str(r.end_date) if r.end_date else None,
			"catchment_area": r.catchment_area,
			"status": r.status,
			"departments": session_departments(r.name),
		}
		for r in rows
	]


def _decode_data_key(data_key: str) -> str:
	value = (data_key or "").strip()
	try:
		raw = base64.b64decode(value, validate=True)
	except (binascii.Error, ValueError):
		raw = b""
	if len(raw) != DATA_KEY_BYTES:
		frappe.throw(_("The device data key must be a base64-encoded 256-bit key."), DeviceEnrolmentError)
	return value


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=30, seconds=60 * 60, ip_based=False, user_based=True)
def enrol(session: str, device_id: str, data_key: str, device_label: str | None = None) -> dict:
	"""Register this device for `session` and issue (or rotate) the user's sync grant. Protocol §2."""
	assert_online_sign_in()
	user = frappe.session.user
	assert_member(session, user)
	row = assert_syncable(session)  # Cancelled refused; Closed only renews (below)
	if ROLE_OUTREACH not in frappe.get_roles(user):
		frappe.throw(_("Only an {0} can enrol a device.").format(_(ROLE_OUTREACH)), frappe.PermissionError)
	device_id = (device_id or "").strip()
	if not DEVICE_ID_PATTERN.match(device_id):
		frappe.throw(_("Invalid device ID."), DeviceEnrolmentError)
	if row.status == SESSION_CLOSED and not frappe.db.exists(
		OUTREACH_GRANT_DOCTYPE, {"device": device_id, "user": user, "session": session, "revoked": 0}
	):
		frappe.throw(
			_(
				"Outreach session {0} is Closed: it is closed to new devices. Only a device already enrolled "
				"for it can sign in again, to send the records it still holds."
			).format(session),
			OutreachSessionClosedError,
			title=_("Session Closed"),
		)
	data_key = _decode_data_key(data_key)
	label = ((device_label or "").strip() or device_id)[:LABEL_MAX_LENGTH]
	now = now_datetime()

	# Device (row-locked when it exists, so two enrolments of one device serialise)
	if frappe.db.exists(OUTREACH_DEVICE_DOCTYPE, device_id):
		device = frappe.get_doc(OUTREACH_DEVICE_DOCTYPE, device_id, for_update=True)
		if device.status != DEVICE_ACTIVE:
			frappe.throw(
				_("This device was revoked. Ask the HIS Officer, or reinstall the outreach app."),
				frappe.PermissionError,
				title=_("Device Revoked"),
			)
	else:
		device = frappe.get_doc(
			{
				"doctype": OUTREACH_DEVICE_DOCTYPE,
				"device_id": device_id,
				"status": DEVICE_ACTIVE,
				"first_user": user,
				"enrolled_at": now,
			}
		)
	device.label = label
	device.last_user = user
	device.last_seen = now
	_save(device)

	# Grant: refresh the live one for (device, user, session), else issue a new one
	from onerc_healthcare.emr_outreach.doctype.emr_outreach_settings.emr_outreach_settings import get_settings

	settings = get_settings()
	live = frappe.db.get_value(
		OUTREACH_GRANT_DOCTYPE,
		{"device": device.name, "user": user, "session": session, "revoked": 0},
		"name",
		for_update=True,
	)
	grant = (
		frappe.get_doc(OUTREACH_GRANT_DOCTYPE, live)
		if live
		else frappe.get_doc(
			{"doctype": OUTREACH_GRANT_DOCTYPE, "device": device.name, "user": user, "session": session}
		)
	)
	secret = secrets.token_urlsafe(SECRET_BYTES)
	grant.secret_salt = secrets.token_hex(16)
	grant.secret_hash = hash_secret(secret, grant.secret_salt)
	grant.issued_at = now
	grant.expires_at = add_to_date(now, days=settings.offline_session_days)
	grant.data_key = data_key
	_save(grant)

	return {
		"grant": grant.name,
		"secret": secret,
		"expires_at": str(get_datetime(grant.expires_at).replace(microsecond=0)),
		"user": {"name": user, "full_name": frappe.db.get_value("User", user, "full_name") or user},
		"settings": {key: settings[key] for key in CLIENT_SETTINGS},
	}


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=10, seconds=60 * 60, ip_based=False, user_based=True)
def recover_key(grant: str) -> dict:
	"""The escrowed data key of the user's own grant (after a PIN lockout). Protocol §2."""
	assert_online_sign_in()
	user = frappe.session.user
	row = frappe.db.get_value(
		OUTREACH_GRANT_DOCTYPE, grant, ["name", "user", "device", "revoked"], as_dict=True
	)
	if not row or row.user != user:
		frappe.throw(_("Device grant {0} not found.").format(grant or ""), frappe.DoesNotExistError)
	if row.revoked or frappe.db.get_value(OUTREACH_DEVICE_DOCTYPE, row.device, "status") != DEVICE_ACTIVE:
		frappe.throw(
			_("This device grant was revoked; its data key cannot be recovered."),
			frappe.PermissionError,
			title=_("Grant Revoked"),
		)
	from frappe.utils.password import get_decrypted_password

	key = get_decrypted_password(OUTREACH_GRANT_DOCTYPE, row.name, "data_key", raise_exception=False)
	if not key:
		frappe.throw(_("No data key is held for this device grant."), frappe.DoesNotExistError)
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"reference_doctype": OUTREACH_GRANT_DOCTYPE,
			"reference_name": row.name,
			"content": _("Data key recovered by {0} (PIN reset).").format(user),
		}
	).insert(ignore_permissions=True)
	frappe.local.flags.commit = True  # a GET is rolled back otherwise; keep the audit comment
	return {"data_key": key}


def revoke_grants(filters: dict, reason: str, user: str | None = None) -> int:
	"""Revoke every live grant matching `filters` (server code). Returns the number revoked."""
	user = user or frappe.session.user
	now = now_datetime()
	count = 0
	for name in frappe.get_all(OUTREACH_GRANT_DOCTYPE, filters={**filters, "revoked": 0}, pluck="name"):
		grant = frappe.get_doc(OUTREACH_GRANT_DOCTYPE, name, for_update=True)
		grant.revoked = 1
		grant.revoked_by = user
		grant.revoked_at = now
		grant.revoke_reason = reason
		_save(grant)
		count += 1
	return count


@frappe.whitelist(methods=["POST"])
def revoke_device(device: str, reason: str) -> dict:
	"""Revoke a device and every grant on it (lost-device procedure, OUT-25). Protocol §2."""
	assert_online_sign_in()
	user = frappe.session.user
	if user != "Administrator" and not set(REVOKE_ROLES) & set(frappe.get_roles(user)):
		frappe.throw(
			_("Only the {0}, {1} or {2} can revoke a device.").format(*(_(r) for r in REVOKE_ROLES)),
			frappe.PermissionError,
		)
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Enter the reason for revoking the device."), title=_("Reason Required"))
	if len(reason) > REASON_MAX_LENGTH:
		frappe.throw(
			_("The reason is {0} characters long; the limit is {1}.").format(len(reason), REASON_MAX_LENGTH)
		)
	if not frappe.db.exists(OUTREACH_DEVICE_DOCTYPE, device):
		frappe.throw(_("Outreach Device {0} not found.").format(device), frappe.DoesNotExistError)
	doc = frappe.get_doc(OUTREACH_DEVICE_DOCTYPE, device, for_update=True)
	if doc.status != DEVICE_REVOKED:
		doc.status = DEVICE_REVOKED
		doc.revoked_by = user
		doc.revoked_at = now_datetime()
		doc.revoke_reason = reason
		_save(doc)
	revoked = revoke_grants({"device": doc.name}, reason, user)
	return {"device": doc.name, "status": doc.status, "grants_revoked": revoked}


# ------------------------------------------------------------------------------------- grant verification


def verify_grant(name: str, secret: str, methods=None) -> frappe._dict:
	"""The grant row when `secret` matches and the grant, its device and its session are usable now for
	`methods` (the whitelisted methods of the request): an open session, or a Closed one when every method is
	in `CLOSED_SESSION_METHODS` (protocol §6 F1). The user must be enabled, on the team and still hold the
	Outreach Team Member role (§6 F6).

	Raises frappe.AuthenticationError otherwise (HTTP 401; protocol §1). Unknown grant and wrong secret
	share one message, so the response never tells which grant names exist."""
	invalid = _("Invalid device credentials. Sign in online.")
	row = (
		frappe.db.get_value(
			OUTREACH_GRANT_DOCTYPE,
			name,
			[
				"name",
				"user",
				"device",
				"session",
				"expires_at",
				"revoked",
				"secret_hash",
				"secret_salt",
				"last_used_at",
			],
			as_dict=True,
		)
		if name and secret
		else None
	)
	expected = row.secret_hash if row and row.secret_hash else None
	given = hash_secret(secret or "", (row.secret_salt if row else None) or "")
	if not expected or not hmac.compare_digest(expected, given):
		frappe.throw(invalid, frappe.AuthenticationError)
	if row.revoked:
		frappe.throw(_("This device grant was revoked. Sign in online."), frappe.AuthenticationError)
	if not row.expires_at or get_datetime(row.expires_at) <= now_datetime():
		frappe.throw(_("The offline sign-in has expired. Sign in online."), frappe.AuthenticationError)
	if frappe.db.get_value(OUTREACH_DEVICE_DOCTYPE, row.device, "status") != DEVICE_ACTIVE:
		frappe.throw(_("This device was revoked. Sign in online."), frappe.AuthenticationError)
	if not frappe.db.get_value("User", row.user, "enabled"):
		frappe.throw(invalid, frappe.AuthenticationError)
	if not has_outreach_role(row.user):
		frappe.throw(
			_("You no longer hold the {0} role. Sign in online.").format(_(ROLE_OUTREACH)),
			frappe.AuthenticationError,
		)
	status = frappe.db.get_value(OUTREACH_SESSION_DOCTYPE, row.session, "status")
	sync_only = bool(methods) and all(m in CLOSED_SESSION_METHODS for m in methods)
	if status not in SESSION_OPEN_STATUSES and not (status == SESSION_CLOSED and sync_only):
		frappe.throw(
			# The status in English ("Closed" / "Cancelled"): the app tells them apart (fix round 1)
			_("Outreach session {0} is {1}: it is closed to this device. Sign in online.").format(
				row.session, status
			),
			frappe.AuthenticationError,
		)
	if not frappe.db.exists(
		OUTREACH_TEAM_DOCTYPE,
		{"parent": row.session, "parenttype": OUTREACH_SESSION_DOCTYPE, "user": row.user},
	):
		frappe.throw(invalid, frappe.AuthenticationError)
	return row


def touch_grant(row: frappe._dict, min_interval_seconds: int = 300) -> None:
	"""Record last use on the grant and device, at most once every `min_interval_seconds`."""
	now = now_datetime()
	if row.last_used_at and (now - get_datetime(row.last_used_at)).total_seconds() < min_interval_seconds:
		return
	frappe.db.set_value(OUTREACH_GRANT_DOCTYPE, row.name, "last_used_at", now, update_modified=False)
	frappe.db.set_value(OUTREACH_DEVICE_DOCTYPE, row.device, "last_seen", now, update_modified=False)
