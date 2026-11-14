# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Outreach Device Grant: one user's sync credential for one device and one session (OUT-07/08, D58).

Holds only the salted SHA-256 hash of the sync secret (the secret is shown once, by `enrol`), its expiry and the
escrowed device data key (Password field, encrypted at rest). Created, rotated and revoked only by
`outreach.devices`; every field is server-managed and a hand edit is refused. Never deleted.
"""

from __future__ import annotations

from frappe.model.document import Document

from onerc_healthcare.outreach.devices import guard_server_fields

PROTECTED_FIELDS = (
	"user",
	"device",
	"session",
	"issued_at",
	"expires_at",
	"last_used_at",
	"revoked",
	"revoked_by",
	"revoked_at",
	"revoke_reason",
	"secret_hash",
	"secret_salt",
)


class OutreachDeviceGrant(Document):
	def validate(self):
		guard_server_fields(self, PROTECTED_FIELDS, password_fields=("data_key",))
