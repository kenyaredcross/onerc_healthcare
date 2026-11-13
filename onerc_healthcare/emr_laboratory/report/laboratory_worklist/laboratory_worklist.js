// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Laboratory Worklist (FR-16): pending lab requests; sample and result status come from the lab records.

frappe.query_reports["Laboratory Worklist"] = {
	filters: [
		{
			fieldname: "facility",
			label: __("Facility"),
			fieldtype: "Link",
			options: "Healthcare Service Unit",
			get_query: () => ({ filters: { is_group: 1 } }),
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -30),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (!data) return value;
		const colours = {
			sample_status: { "Awaiting collection": "orange", Collected: "green" },
			result_status: { "Repeat pending": "red", "Awaiting approval": "blue" },
		};
		const colour = (colours[column.fieldname] || {})[data[column.fieldname]];
		if (colour) {
			return `<span class="indicator-pill ${colour}">${value}</span>`;
		}
		return value;
	},
};
