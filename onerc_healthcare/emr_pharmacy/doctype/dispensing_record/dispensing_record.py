# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Dispensing Record: one dispensing event against one prescription line (FR-21, FR-22, BR-6, SDD §5.5).

- The Medication Request must be submitted, active (not on hold, stopped or cancelled by the clinician) and
  its line still open (Pending or Partly Dispensed).
- Outcome Dispensed: quantity > 0, and the running total may not exceed the prescribed quantity
  (`total_dispensable_quantity`). The dispensed medicine defaults to the prescribed one; a different
  Item is a substitution and needs a reason. The prescribed medicine stays on the record (FR-22).
- Outcome Not Dispensed: a reason is required, the quantity is 0 and the record is always final.
- `final` closes the line even when less than the prescribed quantity was given.
- Submit and cancel recompute the line (`pharmacy.dispensing.recompute_line`) and raise the visit event.
- Cancel: the Pharmacist who dispensed it, with a reason, while the visit is still open.
- Held or stopped lines (prescriber's hold, `pharmacy.api`) accept no new record.
- Locking (M7 / L1): submit and cancel lock the Patient Visit, then the Medication Request
  (`dispensing.lock_line`); the totals and the visit's other lines are then read with `FOR UPDATE`.
- No medicine stock, ledger or price is touched (C-5).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

from onerc_healthcare.constants import ROLE_PHARMACIST, VISIT_CLOSED_STATUSES
from onerc_healthcare.pharmacy import dispensing as line
from onerc_healthcare.pharmacy.catalogue import items_for_medication, medication_for_item

QTY_TOLERANCE = 1e-9


class DispensingRecord(Document):
	def validate(self):
		self._krcs_locked = self._action == "submit"
		self._mr_row = self._visit_row = None
		if self._krcs_locked:
			# Lock order Visit -> Medication Request; every read below is then a locking read.
			self._visit_row, self._mr_row = line.lock_line(self.medication_request)
		if self.docstatus == 0:
			self.dispensed_by = self.dispensed_at = None  # set by the server at submit
		self.set_prescription()
		self.validate_outcome()
		self.validate_running_total()

	def before_submit(self):
		self.dispensed_by = frappe.session.user
		self.dispensed_at = now_datetime()

	def on_submit(self):
		line.recompute_line(self.medication_request)
		line.after_line_change(self.visit, self.name)

	def before_cancel(self):
		user = frappe.session.user
		if user != "Administrator":
			if ROLE_PHARMACIST not in frappe.get_roles(user):
				frappe.throw(_("Only a Pharmacist can cancel a dispensing record."), frappe.PermissionError)
			if self.dispensed_by != user:
				frappe.throw(
					_("You can cancel only the dispensing records you recorded yourself."),
					frappe.PermissionError,
				)
		if not (self.cancel_reason or "").strip():
			frappe.throw(
				_("Enter the reason for cancelling this dispensing record."), title=_("Reason Required")
			)
		# Lock order Visit -> Medication Request (M7 / L1); the visit status comes from the locked row.
		visit_row = line.lock_visit(self.visit)
		line.lock_request(self.medication_request)
		if visit_row and visit_row.status in VISIT_CLOSED_STATUSES:
			frappe.throw(
				_(
					"Visit {0} is closed. A dispensing record can be cancelled only while the visit is open."
				).format(self.visit),
				title=_("Visit Closed"),
			)

	def on_cancel(self):
		line.recompute_line(self.medication_request)
		line.after_line_change(self.visit, self.name, cancelled=True)

	# ------------------------------------------------------------------

	def set_prescription(self):
		"""Copy the prescription line from the Medication Request (server values win over the form)."""
		from onerc_healthcare.visit.patients import assert_patient_active
		from onerc_healthcare.visit.tasks import visit_for_encounter

		mr = self._mr_row or frappe.db.get_value(
			line.MR_DOCTYPE, self.medication_request, list(line.MR_LOCK_FIELDS), as_dict=True
		)
		if not mr:
			frappe.throw(_("Medication Request {0} does not exist.").format(self.medication_request))
		if mr.docstatus != 1:
			frappe.throw(
				_("Medication Request {0} is not a released prescription (it is draft or cancelled).").format(
					self.medication_request
				),
				title=_("Not Dispensable"),
			)
		self.patient = mr.patient
		self.encounter = mr.order_group
		self.prescribed_medication = mr.medication
		self.prescribed_item = mr.medication_item
		self.prescribed_dosage = mr.dosage
		self.prescribed_period = mr.period
		self.prescribed_quantity = line.dispensable_quantity(mr)
		if self._krcs_locked:
			self.visit = self._visit_row.name if self._visit_row else None
		else:
			self.visit = visit_for_encounter(mr.order_group) if mr.order_group else None
		assert_patient_active(self.patient)
		self._mr_status = mr.status
		self._hold = mr.get(line.HOLD_FIELD) or line.HOLD_NONE

	def validate_outcome(self):
		if self.outcome == line.OUTCOME_NOT_DISPENSED:
			self.final = 1
			self.quantity = 0
			self.dispensed_medication = self.dispensed_item = None
			self.is_substitution = 0
			self.substitution_reason = None
			if not (self.not_dispensed_reason or "").strip():
				frappe.throw(
					_("Enter the reason the medicine was not dispensed."), title=_("Reason Required")
				)
			return
		if self.outcome != line.OUTCOME_DISPENSED:
			frappe.throw(_("Outcome must be Dispensed or Not Dispensed."))
		self.not_dispensed_reason = None
		if flt(self.quantity) <= 0:
			frappe.throw(_("Enter the quantity dispensed (more than 0)."), title=_("Quantity Required"))
		self.resolve_dispensed_item()
		prescribed = self.prescribed_item or None
		if prescribed:
			self.is_substitution = int(self.dispensed_item != prescribed)
		else:
			self.is_substitution = int(self.dispensed_medication != self.prescribed_medication)
		if self.is_substitution:
			if not (self.substitution_reason or "").strip():
				frappe.throw(
					_("{0} is a substitute for {1}. Enter the reason for the substitution.").format(
						frappe.bold(self.dispensed_item),
						frappe.bold(prescribed or self.prescribed_medication),
					),
					title=_("Substitution Reason Required"),
				)
		else:
			self.substitution_reason = None

	def resolve_dispensed_item(self):
		"""Default the dispensed medicine to the prescribed one; keep Item and Medication consistent."""
		if not self.dispensed_item and not self.dispensed_medication:
			self.dispensed_medication = self.prescribed_medication
			self.dispensed_item = self.prescribed_item
		elif self.dispensed_medication and not self.dispensed_item:
			if self.dispensed_medication == self.prescribed_medication and self.prescribed_item:
				self.dispensed_item = self.prescribed_item
			else:
				items = items_for_medication(self.dispensed_medication)
				if not items:
					frappe.throw(
						_("Medication {0} has no enabled item to dispense.").format(self.dispensed_medication)
					)
				self.dispensed_item = items[0]
		if not self.dispensed_item:
			frappe.throw(_("Select the item dispensed."), title=_("Item Required"))
		linked = medication_for_item(self.dispensed_item)
		if not self.dispensed_medication:
			self.dispensed_medication = linked
		elif linked and linked != self.dispensed_medication:
			frappe.throw(
				_("Item {0} belongs to medication {1}, not {2}.").format(
					frappe.bold(self.dispensed_item),
					frappe.bold(linked),
					frappe.bold(self.dispensed_medication),
				)
			)
		if frappe.db.get_value("Item", self.dispensed_item, "disabled"):
			frappe.throw(_("Item {0} is disabled and cannot be dispensed.").format(self.dispensed_item))

	def validate_running_total(self):
		dispensed, final = line.line_totals(
			self.medication_request, exclude=self.name, for_update=self._krcs_locked
		)
		state = line.compute_line_state(self.prescribed_quantity, dispensed, final)
		if state in line.RESOLVED_LINE_STATES:
			frappe.throw(
				_("Prescription line {0} is already {1}. Nothing more can be recorded against it.").format(
					self.medication_request, frappe.bold(_(state))
				),
				line.LineResolvedError,
				title=_("Line Resolved"),
			)
		if self._hold:
			frappe.throw(
				_("The prescriber has {0} prescription line {1}. It cannot be dispensed.").format(
					frappe.bold(_("stopped") if self._hold == line.HOLD_STOPPED else _("put on hold")),
					self.medication_request,
				),
				line.LineHeldError,
				title=_("Line On Hold"),
			)
		if self._mr_status not in line.DISPENSABLE_MR_STATUSES:
			frappe.throw(
				_("Medication Request {0} is {1} and cannot be dispensed.").format(
					self.medication_request, frappe.bold(self._mr_status or _("without status"))
				),
				title=_("Not Dispensable"),
			)
		self.previously_dispensed = dispensed
		if self.outcome == line.OUTCOME_DISPENSED:
			remaining = flt(self.prescribed_quantity) - dispensed
			if flt(self.quantity) > remaining + QTY_TOLERANCE:
				frappe.throw(
					_(
						"Only {0} remain to be dispensed on this line (prescribed {1}, already dispensed {2})."
					).format(frappe.bold(remaining), self.prescribed_quantity, dispensed),
					line.OverDispenseError,
					title=_("More Than Prescribed"),
				)
