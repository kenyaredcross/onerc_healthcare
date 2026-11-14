// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt

frappe.provide("onerc_healthcare.moh");

// "Test code": which MOH category an ICD-11 code is counted under, and why (GET, HIS Officer / MIC).
onerc_healthcare.moh.test_code_dialog = function () {
	const d = new frappe.ui.Dialog({
		title: __("Test an ICD-11 code"),
		fields: [
			{
				fieldname: "code",
				fieldtype: "Link",
				options: "Code Value",
				label: __("ICD-11 code"),
				reqd: 1,
				get_query: () => ({ filters: { code_system: "ICD-11" } }),
			},
			{ fieldname: "result", fieldtype: "HTML" },
		],
		primary_action_label: __("Test"),
		primary_action(values) {
			frappe.call({
				method: "onerc_healthcare.reporting.mapping.test_code",
				type: "GET",
				args: { code: values.code },
				callback(r) {
					const m = r.message || {};
					const esc = frappe.utils.escape_html;
					const rows = [
						[__("Code"), `${esc(m.code_value || "")} ${esc(m.code_title || "")}`],
						[__("Category"), `${esc(m.category || __("none"))} ${m.category_title ? "(" + esc(m.category_title) + ")" : ""}`],
						[__("Form"), esc(m.form || "")],
						[__("Matched by"), `${esc(m.matched_by || "")} ${esc(m.value || "")}`],
						[__("Blocks"), esc((m.blocks || []).join(", "))],
					];
					if (m.requires_lab_confirmation) {
						rows.push([__("Laboratory"), __("Counts here only with an approved positive test on the encounter or its internal-referral chain; otherwise under {0}", [esc(m.fallback_category || "")])]);
					}
					if (m.excluded) rows.push([__("Note"), __("Excluded: not counted")]);
					if (m.unmapped) rows.push([__("Note"), __("No mapping: counted under the catch-all and listed as an exception")]);
					d.fields_dict.result.$wrapper.html(
						`<table class="table table-bordered small">${rows.map((x) => `<tr><th>${x[0]}</th><td>${x[1]}</td></tr>`).join("")}</table>`
					);
				},
			});
		},
	});
	d.show();
};

frappe.ui.form.on("MOH Disease Category", {
	refresh(frm) {
		frm.set_intro(__("PROVISIONAL — HIS Officer to confirm (OI-5)."));
		frm.add_custom_button(__("Test code"), () => onerc_healthcare.moh.test_code_dialog());
		if (!frm.is_new()) {
			frm.add_custom_button(__("Mappings"), () =>
				frappe.set_route("List", "MOH Diagnosis Mapping", { category: frm.doc.name })
			);
		}
	},
});
