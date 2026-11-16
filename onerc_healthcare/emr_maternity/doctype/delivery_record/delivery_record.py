# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Delivery Record (INP-16). Rules, newborn registration and the addendum live in
`onerc_healthcare.maternity.delivery` (M3)."""

from frappe.model.document import Document

from onerc_healthcare.maternity import delivery


class DeliveryRecord(Document):
	def validate(self):
		delivery.validate(self)

	def before_submit(self):
		delivery.before_submit(self)

	def on_submit(self):
		delivery.on_submit(self)

	def before_cancel(self):
		delivery.before_cancel(self)

	def before_update_after_submit(self):
		delivery.before_update_after_submit(self)
