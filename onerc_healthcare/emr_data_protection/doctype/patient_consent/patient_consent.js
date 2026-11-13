// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Patient Consent (FR-37): only Active statements; show the statement text (sanitised by the server on save).

frappe.ui.form.on("Patient Consent", {
	setup(frm) {
		frm.set_query("statement", () => ({ filters: { status: "Active" } }));
	},

	onload(frm) {
		if (frm.is_new() && !frm.doc.statement) {
			frappe.db
				.get_value("Consent Statement", { status: "Active", language: frappe.boot.lang || "en" }, "name")
				.then((r) => {
					const name = r && r.message && r.message.name;
					if (name && !frm.doc.statement) frm.set_value("statement", name);
				});
		}
	},

	refresh(frm) {
		const text = (frm.doc.__onload || {}).statement_text;
		if (text) frm.events.render_statement_text(frm, text);
	},

	statement(frm) {
		if (!frm.doc.statement) return frm.get_field("statement_text").$wrapper.empty();
		frappe.db.get_value("Consent Statement", frm.doc.statement, "text").then((r) => {
			frm.events.render_statement_text(frm, (r.message || {}).text || "");
		});
	},

	render_statement_text(frm, text) {
		frm.get_field("statement_text").$wrapper.html(
			`<div class="krcs-consent-text" style="max-height: 300px; overflow-y: auto;">${text}</div>`
		);
	},
});
