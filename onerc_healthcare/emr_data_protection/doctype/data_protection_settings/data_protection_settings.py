# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Data Protection Settings (FR-38): the response period for data access requests (provisional, OI-14)."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class DataProtectionSettings(Document):
	def validate(self):
		if cint(self.response_days) < 1:
			frappe.throw(_("The response period must be at least one day."), title=_("Invalid Value"))
