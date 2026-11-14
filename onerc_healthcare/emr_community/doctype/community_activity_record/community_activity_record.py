# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Community Activity Record controller. Rules and lifecycle: `onerc_healthcare.community.activities`."""

from __future__ import annotations

from frappe.model.document import Document

from onerc_healthcare.community import activities


class CommunityActivityRecord(Document):
	def validate(self):
		activities.validate(self)

	def before_cancel(self):
		activities.before_cancel(self)
