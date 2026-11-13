# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Consent Statement: versioned consent wording (FR-37, SDD §5.10). Lifecycle in privacy/statements.py."""

import frappe
from frappe import _
from frappe.model.document import Document

from onerc_healthcare.constants import CONSENT_STATEMENT_DOCTYPE
from onerc_healthcare.privacy.lifecycle import guard_fields, guard_log
from onerc_healthcare.privacy.statements import (
	LOCKED_WHEN_USED,
	PROTECTED_FIELDS,
	STATUS_ACTIVE,
	STATUS_DRAFT,
	ConsentStatementLockedError,
	get_available_actions,
	is_statement_used,
)


class ConsentStatement(Document):
	def onload(self):
		self.set_onload("available_actions", get_available_actions(self))
		self.set_onload("is_used", is_statement_used(self.name))

	def validate(self):
		self.version = (self.version or "").strip()
		if self.is_new():
			if not self.flags.from_workflow:
				# A new statement always starts as a Draft; Activate makes it current.
				self.status = STATUS_DRAFT
				self.activated_at = self.retired_at = None
				self.set("log", [])
		else:
			before = self.get_doc_before_save()
			if before:
				guard_fields(self, before, PROTECTED_FIELDS)
				guard_log(self, before)
				self.guard_used_wording(before)
		self.validate_single_active()

	def guard_used_wording(self, before):
		changed = [f for f in LOCKED_WHEN_USED if (self.get(f) or "") != (before.get(f) or "")]
		if changed and is_statement_used(self.name):
			frappe.throw(
				_(
					"Statement {0} has already been used for patient consent, so its {1} cannot change."
					" Create a new version instead."
				).format(frappe.bold(self.name), ", ".join(_(self.meta.get_label(f)) for f in changed)),
				ConsentStatementLockedError,
				title=_("Statement Locked"),
			)

	def validate_single_active(self):
		if self.status != STATUS_ACTIVE:
			return
		other = frappe.db.get_value(
			CONSENT_STATEMENT_DOCTYPE,
			{"status": STATUS_ACTIVE, "language": self.language, "name": ("!=", self.name)},
			"name",
		)
		if other:
			frappe.throw(
				_("Statement {0} is already Active for this language.").format(frappe.bold(other)),
				frappe.ValidationError,
			)
