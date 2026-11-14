# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Outreach Device: one tablet installation of the outreach app (OUT-07/08, D58).

Created and refreshed only by `outreach.devices.enrol`; revoked only by `outreach.devices.revoke_device`
(lost-device procedure, OUT-25). Every field is server-managed: a desk or API save that changes one is refused.
Nobody deletes a device (no delete DocPerm).
"""

from __future__ import annotations

from frappe.model.document import Document

from onerc_healthcare.outreach.devices import guard_server_fields

PROTECTED_FIELDS = (
	"device_id",
	"label",
	"status",
	"first_user",
	"last_user",
	"enrolled_at",
	"last_seen",
	"revoked_by",
	"revoked_at",
	"revoke_reason",
)


class OutreachDevice(Document):
	def validate(self):
		guard_server_fields(self, PROTECTED_FIELDS)
