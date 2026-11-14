# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MOH Report Run: one generated MOH report for a facility and period (FR-30). Lifecycle in
reporting/run.py (D8).

On insert the server sets status Draft, the company, the period label and the first history row; client
values for protected fields are ignored. After insert the parameters are fixed, protected fields change
only through reporting.run (``flags.from_workflow``), the history is append-only, and a Submitted,
Superseded or Void run is frozen. Editing the narrative or the compiled-by details of a Generated run
rebuilds its XLSX and PDF from the stored snapshot.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, get_datetime, getdate

from onerc_healthcare.constants import MOH_REPORT_DOCTYPE
from onerc_healthcare.reporting import engine
from onerc_healthcare.reporting.period import period_label, validate_period
from onerc_healthcare.reporting.run import (
	ACTION_CREATE,
	EDITABLE_FIELDS,
	EXCEPTION_FIELDS,
	FROZEN_STATUSES,
	LOG_FIELDS,
	PARAMETER_FIELDS,
	STATUS_DRAFT,
	STATUS_GENERATED,
	TRANSITIONS,
	LockedFieldError,
	append_log,
	assert_generatable,
	assert_single_active,
	check_role,
	get_available_actions,
	is_queued,
	protected_field_names,
	table_key,
)


class MOHReportRun(Document):
	def onload(self):
		self.set_onload("available_actions", get_available_actions(self))
		self.set_onload("is_queued", is_queued(self))
		self.set_onload("preview", self.preview_html())

	def preview_html(self) -> str | None:
		snapshot = engine.snapshot_of(self)
		if not snapshot:
			return None
		from onerc_healthcare.reporting.export_pdf import render_html

		return render_html(self, snapshot, preview=True)

	def before_insert(self):
		if not self.flags.ignore_permissions and not self.flags.from_workflow:
			check_role(TRANSITIONS[ACTION_CREATE])

	def validate(self):
		if self.is_new():
			self.set_new_values()
			return
		before = self.get_doc_before_save()
		if not before:
			return
		self.guard(before)

	def set_new_values(self):
		for f in protected_field_names(self):
			if f != "supersedes" or not self.flags.from_workflow:
				self.set(f, None)
		self.status = STATUS_DRAFT
		self.set("exceptions", [])
		self.validate_parameters()
		self.period_label = period_label(self.from_date, self.to_date, self.period_type)
		assert_single_active(self)
		self.set("log", [])
		append_log(self, None, STATUS_DRAFT, ACTION_CREATE, self.flags.creation_comment)

	def validate_parameters(self):
		report = frappe.get_doc(MOH_REPORT_DOCTYPE, self.report)
		assert_generatable(report)
		self.period_type = self.period_type or report.period_type
		validate_period(self.from_date, self.to_date, self.period_type)
		row = frappe.db.get_value(
			"Healthcare Service Unit", self.facility, ["is_group", "company"], as_dict=True
		)
		if not row or not row.is_group:
			frappe.throw(
				_("Choose a facility (a Healthcare Service Unit group), not {0}.").format(self.facility),
				frappe.ValidationError,
			)
		self.company = row.company

	def guard(self, before):
		from_workflow = bool(self.flags.from_workflow)
		changed = []
		if not from_workflow:
			changed += [f for f in protected_field_names(self) if _norm(self, f) != _norm(before, f)]
			if table_key(self.exceptions, EXCEPTION_FIELDS) != table_key(before.exceptions, EXCEPTION_FIELDS):
				changed.append("exceptions")
		changed += [f for f in PARAMETER_FIELDS if _norm(self, f) != _norm(before, f)]
		if before.status in FROZEN_STATUSES and not from_workflow:
			changed += [f for f in EDITABLE_FIELDS if _norm(self, f) != _norm(before, f)]
		if changed:
			frappe.throw(
				_("{0} cannot be changed on {1} ({2}).").format(
					", ".join(frappe.bold(_(self.meta.get_label(f))) for f in changed),
					self.name,
					_(before.status),
				),
				LockedFieldError,
				title=_("Not Editable"),
			)
		old, new = table_key(before.log, LOG_FIELDS), table_key(self.log, LOG_FIELDS)
		if new[: len(old)] != old or (len(new) > len(old) and not from_workflow):
			frappe.throw(_("The status history cannot be edited."), LockedFieldError, title=_("Not Editable"))

	def on_update(self):
		if self.flags.from_workflow or self.status != STATUS_GENERATED:
			return
		if any(self.has_value_changed(f) for f in EDITABLE_FIELDS):
			from onerc_healthcare.reporting.export_pdf import attach_pdf
			from onerc_healthcare.reporting.export_xlsx import attach_xlsx

			attach_xlsx(self)
			attach_pdf(self)
			self.db_set({"xlsx_file": self.xlsx_file, "pdf_file": self.pdf_file}, update_modified=False)


def _norm(doc, fieldname: str):
	"""Comparable value: form saves send dates, datetimes and numbers as strings."""
	value = doc.get(fieldname)
	df = doc.meta.get_field(fieldname)
	fieldtype = df.fieldtype if df else None
	if fieldtype in ("Check", "Int"):
		return cint(value)
	if not value:
		return None
	if fieldtype == "Date":
		return getdate(value)
	if fieldtype == "Datetime":
		return get_datetime(value)
	return cstr(value).strip() or None
