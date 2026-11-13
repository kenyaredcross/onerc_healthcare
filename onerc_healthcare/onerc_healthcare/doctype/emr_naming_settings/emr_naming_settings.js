// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt

frappe.ui.form.on("EMR Naming Settings", {
	setup(frm) {
		frm.set_query("document_type", "rules", () => ({
			filters: { istable: 0, issingle: 0 },
		}));
	},
});
