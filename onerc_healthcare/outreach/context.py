# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""The outreach context (Phase 4 W0 contract; D58).

The Outreach Team Member has **no DocPerms** on clinical records: `desk_access=0` does not block
`/api/resource`, so a DocPerm would expose every patient to a tablet account. Offline records instead reach
the server through `outreach.sync`, which checks the device grant and session membership and then applies each
record through the same pipelines as the desk (registration, visit engine, consultation, lab, pharmacy).

Those pipelines have role gates of their own (`clinical.utils.require_roles`, `clinical.practitioner`). While
the sync code holds this context, the gates accept the context user (and only that user): the record is still
validated exactly as at the desk, and the team member is still its author (`owner`, status log, practitioner).

Usage (sync code only, after its own checks)::

    with outreach_actor(user, session):
        apply_visit(...)

The context lives on `frappe.local`, so it never outlives the request, and it is cleared on exit even when
the body raises.
"""

from contextlib import contextmanager

import frappe

_KEY = "krcs_outreach_actor"


@contextmanager
def outreach_actor(user: str, session: str):
	"""Let `user` pass the clinical role gates while applying records of outreach `session`."""
	previous = getattr(frappe.local, _KEY, None)
	setattr(frappe.local, _KEY, frappe._dict(user=user, session=session))
	try:
		yield
	finally:
		setattr(frappe.local, _KEY, previous)


def current() -> frappe._dict | None:
	"""The active outreach context ({user, session}) or None."""
	return getattr(frappe.local, _KEY, None)


def active() -> bool:
	return current() is not None


def allows(user: str | None = None) -> bool:
	"""True while an outreach context is active for `user` (default: session user)."""
	ctx = current()
	return bool(ctx) and ctx.user == (user or frappe.session.user)


def session() -> str | None:
	ctx = current()
	return ctx.session if ctx else None
