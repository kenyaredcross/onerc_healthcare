# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Outreach Sync Record: one offline record received from a device (OUT-14..16).

The record is named by its client UUID (unique), which makes it the idempotency key of `outreach.sync`. It also
stores the payload of records that are held or in conflict until a Sync Conflict is resolved, and keeps it
afterwards (nothing is deleted, NFR-9). Written only by `outreach.sync` / `outreach.conflicts`; the HIS
Officer and the Medical In-Charge read it (the payload at permlevel 1).

Status changes (`outreach.sync.RECORD_TRANSITIONS` lists them)."""

from frappe.model.document import Document


class OutreachSyncRecord(Document):
	pass
