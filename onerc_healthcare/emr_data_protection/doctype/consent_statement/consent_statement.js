// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Consent Statement: Activate button (FR-37). Status changes only through privacy.statements.

frappe.ui.form.on("Consent Statement", {
	refresh(frm) {
		const onload = frm.doc.__onload || {};
		if (onload.can_archive) {
			const archived = !!frm.doc.krcs_archived;
			frm.add_custom_button(archived ? __("Unarchive") : __("Archive"), () =>
				frappe
					.xcall("onerc_healthcare.privacy.archive.archive_record", {
						doctype: frm.doctype,
						name: frm.doc.name,
						archived: archived ? 0 : 1,
					})
					.then(() => frm.reload_doc())
			);
		}
		if (onload.is_used) {
			frm.set_df_property("text", "read_only", 1);
			frm.set_df_property("language", "read_only", 1);
			frm.set_intro(
				__("This statement has been used for patient consent. Its wording is locked; create a new version to change it."),
				"blue"
			);
		}
		if (frm.doc.is_provisional && frm.doc.provisional_note) {
			frm.dashboard.add_indicator(__("Provisional"), "orange");
		}
		(onload.available_actions || []).forEach((a) => {
			if (a.action !== "activate" || frm.is_new()) return;
			frm.add_custom_button(__(a.label), () => {
				frappe.confirm(
					__("Activate {0}? The Active statement of the same language will be retired.", [frm.doc.name]),
					() =>
						frappe
							.xcall("onerc_healthcare.privacy.statements.activate_statement", {
								statement: frm.doc.name,
							})
							.then(() => frm.reload_doc())
				);
			}).addClass("btn-primary");
		});
	},
});
