# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Referral (FR-12, BR-14, SDD §4.2). Lifecycle and rules: `onerc_healthcare.clinical.referral`."""

from frappe.model.document import Document

from onerc_healthcare.clinical import referral


class Referral(Document):
	def onload(self):
		self.set_onload("available_actions", referral.get_available_actions(self))

	def before_insert(self):
		referral.before_insert(self)

	def validate(self):
		referral.set_source(self)
		if self.is_new():
			referral.validate_new(self)
		else:
			before = self.get_doc_before_save()
			if before:
				referral.guard_changes(self, before)
		if self.status == referral.STATUS_DRAFT:
			referral.validate_destination(self)
