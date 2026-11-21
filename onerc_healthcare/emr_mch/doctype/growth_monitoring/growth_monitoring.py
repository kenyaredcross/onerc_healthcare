# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Growth Monitoring. Rules live in `onerc_healthcare.mch.growth` (see docs/MCH_CONTRACT.md)."""

from frappe.model.document import Document

from onerc_healthcare.mch import growth


class GrowthMonitoring(Document):
	def validate(self):
		growth.growth_validate(self)

	def before_submit(self):
		growth.growth_before_submit(self)

	def before_cancel(self):
		growth.growth_before_cancel(self)

	def before_update_after_submit(self):
		growth.growth_before_update_after_submit(self)
