# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Outreach Session helpers. W0 contract: O2 (sync) relies on these signatures; O1 owns the file and adds the
session lifecycle (Planned -> Active -> Returned -> Closed / Cancelled) and controller rules here."""

import frappe
from frappe import _

from onerc_healthcare.constants import (
	OUTREACH_SESSION_DOCTYPE,
	OUTREACH_TEAM_DOCTYPE,
	SESSION_OPEN_STATUSES,
)


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


def assert_open(session: str) -> frappe._dict:
	"""The session row ({name, status, facility, company, start_date, end_date, catchment_area}); refuses a
	Closed or Cancelled session."""
	row = frappe.db.get_value(
		OUTREACH_SESSION_DOCTYPE,
		session,
		["name", "status", "facility", "company", "start_date", "end_date", "catchment_area"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("Outreach Session {0} not found.").format(session or ""), frappe.DoesNotExistError)
	if row.status not in SESSION_OPEN_STATUSES:
		frappe.throw(
			_("Outreach session {0} is {1}; it no longer accepts records.").format(session, _(row.status)),
			frappe.ValidationError,
			title=_("Session Closed"),
		)
	return row
