# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Outreach Session (OUT-03). Rules and lifecycle live in `onerc_healthcare.outreach.session`."""

from frappe.model.document import Document

from onerc_healthcare.outreach import session as session_rules


class OutreachSession(Document):
	def onload(self):
		self.set_onload("allowed_actions", session_rules.allowed_actions(self))

	def validate(self):
		session_rules.validate_session(self)

	def on_update(self):
		session_rules.on_session_update(self)
