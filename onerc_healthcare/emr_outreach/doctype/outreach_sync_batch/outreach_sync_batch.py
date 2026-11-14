# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Outreach Sync Batch: one `outreach.sync.push` call (OUT-17). Written only by `outreach.sync`; read-only log
for the HIS Officer and the Medical In-Charge. A re-sent call (same `batch_id`, same user) reuses its row."""

import frappe
from frappe.model.document import Document

BATCH_UNIQUE_CONSTRAINT = "krcs_sync_batch_user_batch"


class OutreachSyncBatch(Document):
	pass


def on_doctype_update():
	"""`batch_id` is unique per user (a client UUID of the call)."""
	frappe.db.add_unique("Outreach Sync Batch", ["user", "batch_id"], constraint_name=BATCH_UNIQUE_CONSTRAINT)
