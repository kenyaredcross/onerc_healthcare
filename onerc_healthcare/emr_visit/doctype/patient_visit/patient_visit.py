# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient Visit: one attendance episode (FR-6 to FR-9, SDD §4.2, §4.4).

Integrity rules (SDD §4.4: status changes only through workflow actions and system events):

- On insert the server sets status, visit date, arrival time, timestamps, task flags, triage links,
  identifier, company, queue number and the first status-log row. Client values for these are ignored.
  Only server code that sets ``doc.flags.from_workflow`` (for example a future outreach sync) may keep a
  supplied visit date and arrival time.
- After insert, `engine.PROTECTED_FIELDS` change only through `onerc_healthcare.visit.engine`, which
  sets ``doc.flags.from_workflow``.
- Status-log rows are append-only: existing rows must stay identical and in order, and only the
  engine may append.
- `facility` is set once. `patient` may be corrected by a Nurse Receptionist while the visit is still
  Registered (a wrong-patient registration cannot otherwise be fixed, since visits cannot be deleted);
  after that it is locked. The change is kept in the document's version history.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_datetime, getdate, now_datetime, today

from onerc_healthcare.constants import (
	DEPT_RECEPTION,
	ROLE_RECEPTIONIST,
	VISIT_CLOSED_STATUSES,
	VISIT_DOCTYPE,
	VISIT_STATUS_REGISTERED,
)
from onerc_healthcare.setup.operations import get_company, get_operation
from onerc_healthcare.visit.engine import (
	ACTION_REGISTER,
	PROTECTED_FIELDS,
	STATUS_LOG_FIELDS,
	get_available_actions,
)
from onerc_healthcare.visit.patients import assert_patient_active, lock_patient
from onerc_healthcare.visit.queue import get_primary_identifier, next_queue_number

OUTREACH_SESSION_MAX_LENGTH = 140
STAGE_TIMESTAMPS = ("triage_started_at", "routed_at", "consultation_started_at", "closed_at")


class VisitStatusLockedError(frappe.ValidationError):
	"""Status, a protected field or the status history was edited outside the visit engine."""


class PatientVisit(Document):
	def onload(self):
		self.set_onload("available_actions", get_available_actions(self))

	def validate(self):
		self.validate_required()
		self.validate_facility()
		self.validate_outreach()
		if self.is_new():
			self.set_registration_values()
			self.validate_patient()
			return
		before = self.get_doc_before_save()
		if not before:
			return
		self.guard_protected_fields(before)
		self.guard_status_log(before)
		if self.patient != before.patient:
			self.validate_patient_change(before)

	def before_save(self):
		if self.is_new():
			# Always server-assigned; a client-supplied queue number is ignored.
			self.queue_number = next_queue_number(self.facility, self.visit_date)

	def validate_required(self):
		"""Clear messages for the two values a receptionist must enter (NFR-10)."""
		missing = [_(self.meta.get_label(f)) for f in ("patient", "facility") if not self.get(f)]
		if missing:
			frappe.throw(
				_("Select the {0} for this visit.").format(_(" and ").join(missing)),
				frappe.MandatoryError,
				title=_("Missing Value"),
			)

	def validate_outreach(self):
		"""An outreach visit names its session (NFR-10); a facility visit has none."""
		self.outreach_session = (self.outreach_session or "").strip() or None
		if not self.is_outreach:
			self.outreach_session = None
			return
		if not self.outreach_session:
			frappe.throw(
				_("Enter the Outreach Session for an outreach visit."),
				frappe.MandatoryError,
				title=_("Missing Value"),
			)
		if len(self.outreach_session) > OUTREACH_SESSION_MAX_LENGTH:
			frappe.throw(
				_("Outreach Session must be at most {0} characters.").format(OUTREACH_SESSION_MAX_LENGTH),
				title=_("Value Too Long"),
			)

	def validate_facility(self):
		unit = frappe.db.get_value(
			"Healthcare Service Unit",
			self.facility,
			["is_group", "parent_healthcare_service_unit", "company"],
			as_dict=True,
		)
		if not unit or not unit.is_group or not unit.parent_healthcare_service_unit:
			frappe.throw(
				_("Facility must be a facility group in Healthcare Service Units, not {0}.").format(
					frappe.bold(self.facility)
				),
				title=_("Invalid Facility"),
			)
		self._facility_company = unit.company

	# ------------------------------------------------------------------ insert

	def set_registration_values(self):
		"""Overwrite every server-owned field; client values are ignored (SDD §4.4)."""
		now = now_datetime()
		if self.flags.from_workflow:
			self.visit_date = self.visit_date or today()
			self.arrived_at = self.arrived_at or now
		else:
			self.visit_date = today()
			self.arrived_at = now
		self.status = VISIT_STATUS_REGISTERED
		self.status_changed_at = self.arrived_at
		self.current_department = (
			DEPT_RECEPTION if frappe.db.exists("Medical Department", DEPT_RECEPTION) else None
		)
		self.lab_open = self.pharmacy_open = 0
		for field in STAGE_TIMESTAMPS:
			self.set(field, None)
		self.triage_level = self.triage_vital_signs = None
		self.queue_number = None
		self.company = self._facility_company or (get_company() if get_operation() else None)
		self.patient_identifier = get_primary_identifier(self.patient) if self.patient else None
		self.set("status_log", [])
		self.append(
			"status_log",
			{
				"from_status": "",
				"to_status": VISIT_STATUS_REGISTERED,
				"action": ACTION_REGISTER,
				"user": frappe.session.user,
				"timestamp": now,
				"department": self.current_department,
			},
		)

	def validate_patient(self):
		"""Active patient, and one open visit per patient per day, checked under a Patient row lock."""
		lock_patient(self.patient)
		assert_patient_active(self.patient)
		existing = frappe.db.get_value(
			VISIT_DOCTYPE,
			{
				"patient": self.patient,
				"visit_date": self.visit_date,
				"status": ["not in", list(VISIT_CLOSED_STATUSES)],
				"name": ["!=", self.name or ""],
			},
			"name",
		)
		if existing:
			frappe.throw(
				_("Patient {0} already has an open visit today: {1}.").format(
					frappe.bold(self.patient_name or self.patient), frappe.bold(existing)
				),
				title=_("Open Visit Exists"),
			)

	# ------------------------------------------------------------------ update

	def guard_protected_fields(self, before):
		if self.flags.from_workflow:
			return
		changed = [
			f for f in PROTECTED_FIELDS if _norm(self, f, self.get(f)) != _norm(self, f, before.get(f))
		]
		if changed:
			labels = ", ".join(_(self.meta.get_label(f)) for f in changed)
			self._locked(
				_("{0} can only change through visit actions (Start Triage, Route, Discharge, ...).").format(
					labels
				)
			)

	def guard_status_log(self, before):
		"""Existing log rows must be unchanged and in order; only the engine may append rows."""
		old = [_log_row(r) for r in before.get("status_log") or []]
		new = [_log_row(r) for r in self.get("status_log") or []]
		if new[: len(old)] != old:
			self._locked(_("The status history cannot be edited."))
		if len(new) != len(old) and not self.flags.from_workflow:
			self._locked(_("Status history rows are added only by visit actions."))

	def validate_patient_change(self, before):
		if not self.flags.from_workflow:
			if before.status != VISIT_STATUS_REGISTERED:
				self._locked(_("The patient cannot be changed after the visit has left Registered."))
			if frappe.session.user != "Administrator" and ROLE_RECEPTIONIST not in frappe.get_roles():
				self._locked(_("Only a Nurse Receptionist can correct the patient of a visit."))
		self.validate_patient()
		self.patient_identifier = get_primary_identifier(self.patient)

	def _locked(self, message):
		frappe.throw(message, VisitStatusLockedError, title=_("Status Locked"))


def _norm(doc, fieldname, value):
	"""Comparable value of a field (form saves send dates/datetimes as strings, checks as str/int)."""
	fieldtype = doc.meta.get_field(fieldname).fieldtype
	if fieldtype in ("Check", "Int"):
		return cint(value)
	if fieldtype == "Datetime":
		return get_datetime(value) if value else None
	if fieldtype == "Date":
		return getdate(value) if value else None
	return value or None


def _log_row(row) -> tuple:
	values = []
	for field in STATUS_LOG_FIELDS:
		value = row.get(field)
		if field == "timestamp":
			value = get_datetime(value) if value else None
		else:
			value = value or ""
		values.append(value)
	return tuple(values)
