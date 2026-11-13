# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Visitor Record: a non-patient visitor's entry and exit (FR-41, BR-13, C-3). Lifecycle in
`onerc_healthcare.frontdesk.visitor` (Inside -> Exited through `record_exit`)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime

from onerc_healthcare.constants import VISIT_DOCTYPE
from onerc_healthcare.frontdesk import visitor as lifecycle

PROTECTED_FIELDS = ("status", "exit_time")
# Fields that may still change once the visitor has exited (the NFR-9 archive flag added by security setup).
EDITABLE_AFTER_EXIT = {"krcs_archived"}
LOG_FIELDS = ("from_status", "to_status", "action", "user", "timestamp", "comment")


class VisitorRecordLockedError(frappe.ValidationError):
	"""Status, exit time or history edited outside the lifecycle, or an Exited record edited."""


class VisitorRecord(Document):
	def validate(self):
		self.visitor_name = (self.visitor_name or "").strip()
		if not self.visitor_name:
			frappe.throw(_("Enter the visitor's name."))
		self.identifier_value = (self.identifier_value or "").strip() or None
		if bool(self.identifier_type) != bool(self.identifier_value):
			frappe.throw(_("Enter both the identifier type and the identifier, or neither."))
		if not self.entry_time:
			self.entry_time = now_datetime()
		if lifecycle.is_future(self.entry_time):
			frappe.throw(_("The entry time cannot be in the future."))
		self.validate_patient()
		if self.is_new():
			self.start()
			return
		before = self.get_doc_before_save()
		if before:
			self.guard(before)

	def start(self):
		self.status = lifecycle.STATUS_INSIDE
		self.exit_time = None
		self.set("status_log", [])
		self.append(
			"status_log",
			{
				"from_status": None,
				"to_status": lifecycle.STATUS_INSIDE,
				"action": lifecycle.ACTION_RECORD_ENTRY,
				"user": frappe.session.user,
				"timestamp": now_datetime(),
			},
		)

	def validate_patient(self):
		from onerc_healthcare.visit.patients import assert_patient_active

		if self.visit and not self.patient:
			self.patient = frappe.db.get_value(VISIT_DOCTYPE, self.visit, "patient")
		if not self.patient:
			self.visit = None
			return
		if self.is_new() or self.has_value_changed("patient"):
			assert_patient_active(self.patient)
		if self.visit:
			visit = frappe.db.get_value(VISIT_DOCTYPE, self.visit, ["patient", "facility"], as_dict=True)
			if not visit or visit.patient != self.patient:
				frappe.throw(_("Visit {0} is not a visit of patient {1}.").format(self.visit, self.patient))
			if visit.facility and visit.facility != self.facility:
				frappe.throw(
					_("Visit {0} is at facility {1}, not {2}.").format(
						self.visit, visit.facility, self.facility
					)
				)

	def guard(self, before):
		if self.flags.from_workflow:
			self.guard_log(before, allow_append=True)
			return
		if before.status == lifecycle.STATUS_EXITED and self.changed_fields(before) - EDITABLE_AFTER_EXIT:
			self._locked(_("The visitor has exited; this record can no longer be changed."))
		changed = [f for f in PROTECTED_FIELDS if _norm(f, self.get(f)) != _norm(f, before.get(f))]
		if changed:
			self._locked(
				_("{0} change only through Record Exit.").format(
					", ".join(_(self.meta.get_label(f)) for f in changed)
				)
			)
		self.guard_log(before, allow_append=False)

	def changed_fields(self, before) -> set[str]:
		skip = ("Section Break", "Column Break", "Tab Break", "Table", "HTML", "Button")
		return {
			df.fieldname
			for df in self.meta.fields
			if df.fieldtype not in skip
			and _norm(df.fieldname, self.get(df.fieldname)) != _norm(df.fieldname, before.get(df.fieldname))
		}

	def guard_log(self, before, allow_append: bool):
		old = [_log_row(r) for r in before.get("status_log") or []]
		new = [_log_row(r) for r in self.get("status_log") or []]
		if new[: len(old)] != old:
			self._locked(_("The status history cannot be edited."))
		if len(new) != len(old) and not allow_append:
			self._locked(_("Status history rows are added only by visitor actions."))

	def _locked(self, message):
		frappe.throw(message, VisitorRecordLockedError, title=_("Record Locked"))


DATETIME_FIELDS = ("exit_time", "entry_time", "timestamp")


def _norm(fieldname: str, value):
	"""Comparable value (form saves send datetimes as strings)."""
	if value in (None, ""):
		return None
	return get_datetime(value) if fieldname in DATETIME_FIELDS else str(value)


def _log_row(row) -> tuple:
	return tuple(_norm(f, row.get(f)) for f in LOG_FIELDS)
