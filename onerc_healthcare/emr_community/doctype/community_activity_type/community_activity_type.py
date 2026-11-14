# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Master list (R3, SDD §5.12.7). Seeded by `community.setup.install`; the HIS Officer and the Medical
In-Charge correct it in the desk. Seeding never overwrites an existing row."""

from frappe.model.document import Document


class CommunityActivityType(Document):
	def validate(self):
		self.activity_type_name = (self.activity_type_name or "").strip()
		self.moh_line = (self.moh_line or "").strip() or None
