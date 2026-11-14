// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt

frappe.query_reports["MOH Unmapped Diagnoses"] = {
	filters: [
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date", reqd: 1, default: frappe.datetime.month_start() },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date", reqd: 1, default: frappe.datetime.month_end() },
		{
			fieldname: "facility",
			label: __("Facility"),
			fieldtype: "Link",
			options: "Healthcare Service Unit",
			get_query: () => ({ filters: { is_group: 1 } }),
		},
	],
	get_datatable_options(options) {
		return Object.assign(options, { checkboxColumn: true });
	},
	onload(report) {
		if (!frappe.user.has_role("HIS Officer")) return;
		report.page.add_inner_button(__("Map selected codes"), () => {
			const dt = report.datatable;
			const checked = dt ? dt.rowmanager.getCheckedRows() : [];
			const codes = checked.map((i) => report.data[i] && report.data[i].code).filter(Boolean);
			if (!codes.length) {
				frappe.msgprint(__("Tick the codes to map first."));
				return;
			}
			const d = new frappe.ui.Dialog({
				title: __("Map {0} code(s)", [codes.length]),
				fields: [
					{ fieldname: "category", fieldtype: "Link", options: "MOH Disease Category", label: __("MOH Disease Category"), reqd: 1 },
					{ fieldname: "note", fieldtype: "Small Text", label: __("Note") },
					{ fieldname: "help", fieldtype: "HTML", options: `<p class="text-muted small">${__("Creates an Exact code mapping for each code. PROVISIONAL — HIS Officer to confirm (OI-5).")}</p>` },
				],
				primary_action_label: __("Map"),
				primary_action(values) {
					frappe.call({
						method: "onerc_healthcare.reporting.mapping.map_codes",
						type: "POST",
						args: { codes: JSON.stringify(codes), category: values.category, note: values.note },
						freeze: true,
						callback(r) {
							const m = r.message || {};
							d.hide();
							let msg = __("{0} mapping(s) created.", [(m.created || []).length]);
							if ((m.skipped || []).length) {
								msg += "<br>" + m.skipped.map((s) => `${frappe.utils.escape_html(s.code)}: ${frappe.utils.escape_html(s.reason)}`).join("<br>");
							}
							frappe.msgprint(msg);
							report.refresh();
						},
					});
				},
			});
			d.show();
		});
	},
};
