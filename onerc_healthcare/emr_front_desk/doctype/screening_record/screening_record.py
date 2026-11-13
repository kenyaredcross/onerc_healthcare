# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Screening Record: entrance screening of one person (FR-42, BR-13, SDD §5.11).

- Person screened: a Patient (optionally on a visit), a Visitor (Visitor Record or a name), Staff or Other.
  Fields that do not belong to the chosen type are cleared.
- Results: one row per parameter, snapshotted from Screening Parameter while the record is a draft
  (see `frontdesk.screening`). A new record starts with every active parameter.
- Submit: required parameters need a value; `outcome`, `screened_by` and `screened_at` are set by the
  server. Submitted records are permanent: no cancel, no amend, never deleted (NFR-9).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from onerc_healthcare.constants import SCREENING_PARAMETER_DOCTYPE as PARAMETER_DOCTYPE
from onerc_healthcare.constants import VISIT_DOCTYPE, VISITOR_DOCTYPE
from onerc_healthcare.frontdesk import screening

PERSON_PATIENT = "Patient"
PERSON_VISITOR = "Visitor"
PERSON_STAFF = "Staff"
PERSON_OTHER = "Other"
PERSON_TYPES = (PERSON_PATIENT, PERSON_VISITOR, PERSON_STAFF, PERSON_OTHER)


class ScreeningRecord(Document):
	def validate(self):
		self.validate_person()
		self.validate_results()

	def before_submit(self):
		missing = [r.label or r.parameter for r in self.results if r.required and not r.value]
		missing += [
			p["label"]
			for p in screening.active_parameters()
			if p.get("required") and p["name"] not in {r.parameter for r in self.results}
		]
		if missing:
			frappe.throw(
				_("Enter a value for: {0}.").format(", ".join(missing)), title=_("Screening Incomplete")
			)
		if not any(r.value for r in self.results):
			frappe.throw(_("Enter at least one screening result."), title=_("Screening Incomplete"))
		self.screened_by = frappe.session.user
		self.screened_at = now_datetime()

	def before_cancel(self):
		frappe.throw(_("Screening records are permanent and cannot be cancelled."), frappe.PermissionError)

	# ------------------------------------------------------------------

	def validate_person(self):
		if self.person_type not in PERSON_TYPES:
			frappe.throw(_("Choose who was screened: {0}.").format(", ".join(_(t) for t in PERSON_TYPES)))
		self.person_name = (self.person_name or "").strip() or None
		if self.person_type == PERSON_PATIENT:
			self.visitor_record = None
			self.validate_patient()
		elif self.person_type == PERSON_VISITOR:
			self.patient = self.visit = None
			if self.visitor_record:
				visitor = frappe.db.get_value(
					VISITOR_DOCTYPE, self.visitor_record, ["visitor_name", "facility"], as_dict=True
				)
				if not visitor:
					frappe.throw(_("Visitor Record {0} does not exist.").format(self.visitor_record))
				self.person_name = visitor.visitor_name
				self.match_facility(visitor.facility, self.visitor_record)
			elif not self.person_name:
				frappe.throw(_("Select the Visitor Record or enter the visitor's name."))
		else:
			self.patient = self.visit = self.visitor_record = None
			if not self.person_name:
				frappe.throw(_("Enter the name of the person screened."))
		if not self.facility:
			frappe.throw(_("Select the facility."))

	def validate_patient(self):
		from onerc_healthcare.visit.patients import assert_patient_active

		if not self.patient and self.visit:
			self.patient = frappe.db.get_value(VISIT_DOCTYPE, self.visit, "patient")
		if not self.patient:
			frappe.throw(_("Select the patient screened."))
		if self.is_new() or self.has_value_changed("patient"):
			assert_patient_active(self.patient)
		self.person_name = frappe.db.get_value("Patient", self.patient, "patient_name")
		if self.visit:
			visit = frappe.db.get_value(VISIT_DOCTYPE, self.visit, ["patient", "facility"], as_dict=True)
			if not visit:
				frappe.throw(_("Patient Visit {0} does not exist.").format(self.visit))
			if visit.patient != self.patient:
				frappe.throw(_("Visit {0} belongs to a different patient.").format(self.visit))
			self.match_facility(visit.facility, self.visit)

	def match_facility(self, facility: str | None, source: str) -> None:
		if not facility:
			return
		if not self.facility:
			self.facility = facility
		elif self.facility != facility:
			frappe.throw(_("{0} is at facility {1}, not {2}.").format(source, facility, self.facility))

	def validate_results(self):
		if self.is_new() and not self.results:
			for p in screening.active_parameters():
				self.append("results", {"parameter": p["name"]})
		seen = set()
		for row in self.results:
			if row.parameter in seen:
				frappe.throw(_("Row {0}: {1} is entered twice.").format(row.idx, row.parameter))
			seen.add(row.parameter)
			parameter = frappe.db.get_value(
				PARAMETER_DOCTYPE, row.parameter, ["name", "active", *screening.SNAPSHOT_FIELDS], as_dict=True
			)
			if not parameter:
				frappe.throw(
					_("Row {0}: Screening Parameter {1} does not exist.").format(row.idx, row.parameter)
				)
			if not parameter.active:
				frappe.throw(
					_("Row {0}: {1} is no longer an active parameter.").format(row.idx, row.parameter)
				)
			screening.snapshot(row, parameter)
			row.flag = screening.evaluate_row(row)
		self.outcome = screening.outcome_of([r.flag for r in self.results])
