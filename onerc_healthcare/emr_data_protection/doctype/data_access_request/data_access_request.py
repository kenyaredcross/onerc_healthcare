# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Data Access Request (FR-38, BR-12). Lifecycle and transition table in privacy/dar.py.

On insert the server sets status Received, the received and due dates and the first history row;
client values for lifecycle fields are ignored. After insert, lifecycle fields change only through
privacy.dar (``flags.from_workflow``), intake fields only while the request is Received, and the
history is append-only.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, getdate, today

from onerc_healthcare.privacy.dar import (
	ACTION_CREATE,
	INTAKE_FIELDS,
	PROTECTED_FIELDS,
	REQUESTER_PATIENT,
	REQUESTER_REPRESENTATIVE,
	STATUS_RECEIVED,
	TRANSITIONS,
	get_available_actions,
	get_response_days,
	is_overdue,
	option_values,
)
from onerc_healthcare.privacy.lifecycle import (
	append_log,
	check_role,
	clean_text,
	guard_fields,
	guard_log,
)
from onerc_healthcare.visit.patients import assert_patient_active


class DataAccessRequest(Document):
	def onload(self):
		self.set_onload("available_actions", get_available_actions(self))
		self.set_onload("is_overdue", is_overdue(self))
		self.set_onload("identity_methods", option_values("identity_method"))
		self.set_onload("response_methods", option_values("response_method"))

	def before_insert(self):
		if not self.flags.ignore_permissions and not self.flags.from_workflow:
			check_role(TRANSITIONS[ACTION_CREATE])

	def validate(self):
		self.validate_intake()
		if self.is_new():
			self.set_received_values()
			assert_patient_active(self.patient)
			return
		before = self.get_doc_before_save()
		if not before:
			return
		guard_fields(self, before, PROTECTED_FIELDS)
		guard_log(self, before)
		if before.status != STATUS_RECEIVED:
			guard_fields(self, before, INTAKE_FIELDS)
		if self.patient != before.patient:
			assert_patient_active(self.patient)

	def set_received_values(self):
		for f in PROTECTED_FIELDS:
			self.set(f, None)
		self.status = STATUS_RECEIVED
		self.received_date = getdate(today())
		self.due_date = add_days(self.received_date, get_response_days())
		self.set("log", [])
		append_log(self, None, STATUS_RECEIVED, ACTION_CREATE)

	def validate_intake(self):
		self.requester_type = self.requester_type or REQUESTER_PATIENT
		self.requester_name = clean_text(self.requester_name, _("Requester Name"), required=True)
		self.details = clean_text(self.details, _("Details"), required=True)
		self.contact = (self.contact or "").strip() or None
		self.relationship = (self.relationship or "").strip() or None
		if self.requester_type == REQUESTER_REPRESENTATIVE and not self.relationship:
			frappe.throw(
				_("Enter the representative's relationship to the patient."),
				frappe.MandatoryError,
				title=_("Missing Value"),
			)
		if self.requester_type == REQUESTER_PATIENT:
			self.relationship = None
