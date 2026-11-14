# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Device sync authentication (OUT-07/08, NFR-8, D58; protocol §1). O1 owns this file.

`authenticate_device` is in `hooks.auth_hooks`, so `frappe.auth.validate_auth` calls it on **every** request.
It does nothing unless the `Authorization` header uses the `OUTREACH_AUTH_SCHEME` scheme
(`Authorization: krcs-outreach <grant>:<secret>`). For such a request:

1. Every method the request would run must be one of `ALLOWED_METHODS` (sync.push, sync.status,
   package.get_reference, package.get_patients). The method comes from the path (`/api/method/<m>`,
   `/api/v1/method/<m>`, `/api/v2/method/<m>`) and from `cmd` in the form dict, which Frappe runs before the
   path when present; any other target, or none, is refused.
2. `devices.verify_grant` checks the secret (salted SHA-256, constant-time compare), that the grant is not
   revoked or expired, the device is Active, the user enabled and still on the team, and the session open.
3. The request then runs as the grant's user (`frappe.set_user`), and `frappe.local.krcs_outreach_grant` holds
   the grant name so sync and package code can match the session and device.

Every refusal is `frappe.AuthenticationError` (HTTP 401): the app then asks for an online sign-in.

CSRF: Frappe checks the CSRF token only for a cookie session that has one, before this hook. A device request
sends no cookie, so it is a Guest request until this hook sets the user and needs no token. A request that
carries both a cookie session and a grant must be for the same user, otherwise it is refused.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.database.utils import commit_after_response

from onerc_healthcare.constants import OUTREACH_AUTH_SCHEME

ALLOWED_METHODS = frozenset(
	(
		"onerc_healthcare.outreach.sync.push",
		"onerc_healthcare.outreach.sync.status",
		"onerc_healthcare.outreach.package.get_reference",
		"onerc_healthcare.outreach.package.get_patients",
	)
)
METHOD_PATH_PREFIXES = ("/api/method/", "/api/v1/method/", "/api/v2/method/")
GRANT_LOCAL_KEY = "krcs_outreach_grant"


def _header() -> str:
	request = getattr(frappe.local, "request", None)
	if not request:
		return ""
	return (request.headers.get("Authorization") or "").strip()


def parse_header(header: str) -> tuple[str, str] | None:
	"""(grant, secret) when `header` uses the outreach scheme, else None. Malformed credentials give
	("", "")."""
	scheme, _sep, credentials = header.partition(" ")
	if scheme.strip().lower() != OUTREACH_AUTH_SCHEME:
		return None
	grant, sep, secret = credentials.strip().partition(":")
	if not sep:
		return "", ""
	return grant.strip(), secret.strip()


def requested_methods() -> list[str]:
	"""Every whitelisted method this request would run: the `cmd` (run first by Frappe when present) and the
	method named by an API method path. A path that is not an API method path counts as a non-method
	target ("")."""
	request = frappe.local.request
	targets = []
	cmd = (getattr(frappe.local, "form_dict", None) or {}).get("cmd")
	if cmd:
		targets.append(str(cmd).strip())
	path = (getattr(request, "path", "") or "").rstrip("/")
	for prefix in METHOD_PATH_PREFIXES:
		if path.startswith(prefix):
			targets.append(path[len(prefix) :])
			break
	else:
		if not (cmd and path in ("", "/api/method", "/api/v1/method")):
			targets.append("")
	return targets


def authenticate_device() -> None:
	"""auth_hooks entry (see module docstring)."""
	parsed = parse_header(_header())
	if parsed is None:
		return
	setattr(frappe.local, GRANT_LOCAL_KEY, None)
	targets = requested_methods()
	if not targets or any(t not in ALLOWED_METHODS for t in targets):
		frappe.throw(
			_("A device sign-in can only sync and download the outreach package."), frappe.AuthenticationError
		)
	grant, secret = parsed
	from onerc_healthcare.outreach.devices import touch_grant, verify_grant

	row = verify_grant(grant, secret)
	current = frappe.session.user if getattr(frappe.local, "session", None) else None
	if current not in (None, "", "Guest") and current != row.user:
		frappe.throw(
			_("The device grant belongs to another user. Sign out and sign in again."),
			frappe.AuthenticationError,
		)
	form_dict = frappe.local.form_dict  # set_user clears it (as frappe.auth's API-key login does)
	frappe.set_user(row.user)
	frappe.local.form_dict = form_dict
	setattr(frappe.local, GRANT_LOCAL_KEY, row.name)
	commit_after_response(lambda: touch_grant(row))
