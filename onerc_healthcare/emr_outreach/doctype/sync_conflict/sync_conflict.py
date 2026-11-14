# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Sync Conflict (FR-26, OUT-19..23). Raised and resolved only by `onerc_healthcare.outreach.conflicts`
(lifecycle table and decision table there). Every field is read-only: a desk or API save that changes the
conflict outside that module is refused, and the log rows are append-only."""

import json

import frappe
from frappe import _
from frappe.model.document import Document

# Fields only `outreach.conflicts` may change after insert.
PROTECTED_FIELDS = (
	"conflict_type",
	"status",
	"review_only",
	"summary",
	"patient",
	"session",
	"sync_record",
	"raised_by",
	"raised_at",
	"facility_version",
	"outreach_version",
	"candidates",
	"decision",
	"chosen_patient",
	"chosen_fields",
	"resolved_by",
	"resolved_at",
	"reason",
	"result_note",
)
LOG_FIELDS = ("action", "from_status", "to_status", "decision", "user", "timestamp", "reason", "note")


class SyncConflictLockedError(frappe.ValidationError):
	"""The conflict was edited outside `outreach.conflicts`."""


class SyncConflict(Document):
	def onload(self):
		from onerc_healthcare.outreach.conflicts import form_context

		self.set_onload("krcs_conflict", form_context(self))

	def validate(self):
		if self.is_new() or self.flags.krcs_via_conflicts:
			return
		before = self.get_doc_before_save()
		if not before:
			return
		changed = [f for f in PROTECTED_FIELDS if _norm(self.get(f)) != _norm(before.get(f))]
		if changed:
			frappe.throw(
				_(
					"A sync conflict is resolved only with its decision buttons ({0} cannot be edited)."
				).format(", ".join(_(self.meta.get_label(f)) for f in changed)),
				SyncConflictLockedError,
				title=_("Not Allowed"),
			)
		old = [_log(r) for r in before.get("log") or []]
		new = [_log(r) for r in self.get("log") or []]
		if new != old:
			frappe.throw(_("The conflict history cannot be edited."), SyncConflictLockedError)


def _norm(value):
	"""Comparable form: JSON text and parsed JSON compare equal; empty, 0 and None are the same."""
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			return value.strip() or None
	if not value:
		return None
	if isinstance(value, dict | list):
		return json.dumps(value, sort_keys=True, default=str)
	return str(value)


def _log(row) -> tuple:
	return tuple(str(row.get(f) or "") for f in LOG_FIELDS)
