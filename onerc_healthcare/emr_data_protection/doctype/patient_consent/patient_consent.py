# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient Consent (FR-37, BR-12, C-3; SDD §5.10).

Rules:
- Submitted once, never cancelled or amended (no role holds cancel; `before_cancel` refuses it for
  everyone). A change of mind is a new record (e.g. Withdrawn), and the latest submitted record is the
  patient's current consent.
- `statement` must be an Active Consent Statement when saved and when submitted; its version and
  language are snapshotted, so the record keeps the version it was given under.
- `given_by` Representative requires `representative_name` and `relationship`; for Patient they are
  cleared.
- `reason` is required unless the outcome is Given.
- Withdrawn requires the patient's current consent to be Given.
- `recorded_by` / `recorded_at` are set by the server on every save and at submit.
- The patient must be active (not disabled, not merged).
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from onerc_healthcare.constants import CONSENT_STATEMENT_DOCTYPE
from onerc_healthcare.privacy.consent import (
	GIVEN_BY_PATIENT,
	GIVEN_BY_REPRESENTATIVE,
	OUTCOME_GIVEN,
	OUTCOME_WITHDRAWN,
	OUTCOMES,
	get_current_consent,
)
from onerc_healthcare.privacy.lifecycle import MAX_COMMENT_LENGTH
from onerc_healthcare.privacy.statements import STATUS_ACTIVE
from onerc_healthcare.visit.patients import assert_patient_active


class PatientConsentError(frappe.ValidationError):
	"""A Patient Consent rule was broken."""


class PatientConsent(Document):
	def onload(self):
		if self.statement:
			self.set_onload(
				"statement_text", frappe.db.get_value(CONSENT_STATEMENT_DOCTYPE, self.statement, "text")
			)

	def validate(self):
		if self.is_new() or self.has_value_changed("patient") or self._action == "submit":
			assert_patient_active(self.patient)
		self.validate_statement()
		self.validate_outcome()
		self.validate_given_by()
		self.validate_reason()
		self.validate_withdrawal()
		self.recorded_by = frappe.session.user
		self.recorded_at = now_datetime()

	def before_cancel(self):
		frappe.throw(
			_("A Patient Consent cannot be cancelled. Record a new consent (for example Withdrawn) instead."),
			frappe.PermissionError,
			title=_("Not Allowed"),
		)

	def validate_statement(self):
		row = frappe.db.get_value(
			CONSENT_STATEMENT_DOCTYPE, self.statement, ["name", "status", "version", "language"], as_dict=True
		)
		if not row:
			frappe.throw(
				_("Consent Statement {0} does not exist.").format(self.statement), PatientConsentError
			)
		if row.status != STATUS_ACTIVE:
			frappe.throw(
				_("Consent Statement {0} is {1}. Use the Active statement.").format(
					frappe.bold(row.name), _(row.status)
				),
				PatientConsentError,
				title=_("Statement Not Active"),
			)
		self.statement_version = row.version
		self.statement_language = row.language

	def validate_outcome(self):
		if self.outcome not in OUTCOMES:
			frappe.throw(
				_("Choose the consent outcome: {0}.").format(", ".join(_(o) for o in OUTCOMES)),
				PatientConsentError,
				title=_("Missing Value"),
			)

	def validate_given_by(self):
		self.given_by = self.given_by or GIVEN_BY_PATIENT
		if self.given_by not in (GIVEN_BY_PATIENT, GIVEN_BY_REPRESENTATIVE):
			frappe.throw(_("Given By must be Patient or Representative."), PatientConsentError)
		self.representative_name = (self.representative_name or "").strip() or None
		self.relationship = (self.relationship or "").strip() or None
		if self.given_by == GIVEN_BY_PATIENT:
			self.representative_name = self.relationship = None
			return
		missing = [
			_(self.meta.get_label(f)) for f in ("representative_name", "relationship") if not self.get(f)
		]
		if missing:
			frappe.throw(
				_("Enter the representative's {0}.").format(_(" and ").join(missing)),
				PatientConsentError,
				title=_("Missing Value"),
			)

	def validate_reason(self):
		self.reason = (self.reason or "").strip() or None
		if self.outcome != OUTCOME_GIVEN and not self.reason:
			frappe.throw(
				_("Enter the reason consent is {0}. Care continues; the reason is recorded.").format(
					_(self.outcome).lower()
				),
				PatientConsentError,
				title=_("Reason Required"),
			)
		if self.reason and len(self.reason) > MAX_COMMENT_LENGTH:
			frappe.throw(
				_("The reason is {0} characters long; the limit is {1}.").format(
					len(self.reason), MAX_COMMENT_LENGTH
				),
				PatientConsentError,
			)

	def validate_withdrawal(self):
		if self.outcome != OUTCOME_WITHDRAWN:
			return
		current = get_current_consent(self.patient, exclude=None if self.is_new() else self.name)
		if not current or current.outcome != OUTCOME_GIVEN:
			frappe.throw(
				_("Consent can only be withdrawn when the patient's current consent is Given."),
				PatientConsentError,
				title=_("Nothing to Withdraw"),
			)
