// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Screening Record (FR-42): starts with every active parameter; flags are set by the server on save.

frappe.ui.form.on("Screening Record", {
	setup(frm) {
		frm.set_query("facility", () => ({ filters: { is_group: 1 } }));
		frm.set_query("visit", () => ({
			filters: frm.doc.patient ? { patient: frm.doc.patient } : {},
		}));
		frm.set_query("parameter", "results", () => ({ filters: { active: 1 } }));
	},

	onload(frm) {
		if (frm.is_new() && !(frm.doc.results || []).length) {
			frappe
				.xcall("onerc_healthcare.frontdesk.screening.get_active_parameters")
				.then((params) => {
					(params || []).forEach((p) => {
						const row = frm.add_child("results", { parameter: p.name });
						Object.assign(row, {
							label: p.label,
							unit: p.unit,
							param_type: p.param_type,
						});
					});
					frm.refresh_field("results");
				});
		}
	},

	refresh(frm) {
		if (frm.doc.outcome) {
			const out = frm.doc.outcome === "Normal";
			frm.page.set_indicator(__(frm.doc.outcome), out ? "green" : "red");
		}
	},

	person_type(frm) {
		frm.set_value({ patient: "", visit: "", visitor_record: "" });
	},
});
