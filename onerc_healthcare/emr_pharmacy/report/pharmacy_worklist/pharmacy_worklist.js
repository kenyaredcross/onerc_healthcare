// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Pharmacy Worklist (FR-20): open prescription lines; "Dispense" opens a pre-filled Dispensing Record.

frappe.query_reports["Pharmacy Worklist"] = {
	filters: [
		{
			fieldname: "facility",
			label: __("Facility"),
			fieldtype: "Link",
			options: "Healthcare Service Unit",
			get_query: () => ({ filters: { is_group: 1 } }),
		},
		{
			fieldname: "date",
			label: __("Order Date"),
			fieldtype: "Date",
		},
	],

	formatter(value, row, column, data, default_formatter) {
		if (column.fieldname === "action" && data && data.medication_request) {
			const mr = encodeURIComponent(data.medication_request);
			return `<button class="btn btn-xs btn-primary" data-krcs-dispense="${mr}">${__(
				"Dispense"
			)}</button>`;
		}
		value = default_formatter(value, row, column, data);
		if (
			column.fieldname === "visit_status" &&
			data &&
			["Discharged", "Referred"].includes(data.visit_status)
		) {
			value = `<span class="text-muted">${value} (${__("visit closed")})</span>`;
		}
		return value;
	},

	onload(report) {
		$(report.page.wrapper).on("click", "[data-krcs-dispense]", function () {
			const mr = decodeURIComponent($(this).attr("data-krcs-dispense"));
			frappe.new_doc("Dispensing Record", { medication_request: mr });
		});
	},
};
