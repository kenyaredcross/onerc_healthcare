// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Phase 4 (FR-26): a patient whose outreach records wait on a Sync Conflict is marked on the record.
frappe.ui.form.on("Patient", {
	refresh(frm) {
		const open = cint(frm.doc.krcs_open_sync_conflicts);
		if (!open || frm.is_new()) return;
		const link = `<a href="/app/sync-conflict?patient=${encodeURIComponent(frm.doc.name)}&status=Open">${__(
			"Review"
		)}</a>`;
		frm.dashboard.set_headline_alert(
			__("{0} outreach record(s) for this patient wait on a sync conflict. {1}", [open, link]),
			"orange"
		);
	},
});
