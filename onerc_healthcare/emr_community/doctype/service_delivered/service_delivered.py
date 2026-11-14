# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Service Delivered controller. Rules and lifecycle: `onerc_healthcare.community.services`."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_datetime, getdate

from onerc_healthcare.community import services

# Changed only on insert (from the visit / the recorder) or by the lifecycle.
PROTECTED_FIELDS = (
	"visit",
	"patient",
	"facility",
	"provided_by",
	"status",
	"void_reason",
	"voided_by",
	"voided_at",
)
EDITABLE_FIELDS = ("service_type", "service_date", "notes")
# Security setup's archive flag (NFR-9) may change on any record.
ARCHIVE_FIELDS = ("krcs_archived",)


class ServiceDeliveredLockedError(frappe.ValidationError):
	"""A protected field, or a Voided record, edited outside the lifecycle."""


class ServiceDelivered(Document):
	def validate(self):
		if self.is_new():
			services.validate_new(self)
			return
		before = self.get_doc_before_save()
		if not before or self.flags.from_workflow:
			return
		changed = [
			f for f in PROTECTED_FIELDS if _norm(self, f, self.get(f)) != _norm(self, f, before.get(f))
		]
		if changed:
			self._locked(
				_("{0} cannot be changed here; use Void Service to withdraw a service.").format(
					", ".join(_(self.meta.get_label(f)) for f in changed)
				)
			)
		edited = [f for f in EDITABLE_FIELDS if _norm(self, f, self.get(f)) != _norm(self, f, before.get(f))]
		if not edited:
			return
		if before.status != services.STATUS_ACTIVE:
			self._locked(_("A voided service cannot be changed."))
		if not services.may_edit(before):
			frappe.throw(
				_("Only the person who recorded the service, or the {0}, can change it.").format(
					_(services.ROLE_MIC)
				),
				frappe.PermissionError,
			)
		services.validate_values(self)

	def _locked(self, message):
		frappe.throw(message, ServiceDeliveredLockedError, title=_("Record Locked"))


def _norm(doc, fieldname, value):
	df = doc.meta.get_field(fieldname)
	fieldtype = df.fieldtype if df else "Data"
	if fieldtype in ("Check", "Int"):
		return cint(value)
	if fieldtype == "Datetime":
		return get_datetime(value) if value else None
	if fieldtype == "Date":
		return getdate(value) if value else None
	return value or None
