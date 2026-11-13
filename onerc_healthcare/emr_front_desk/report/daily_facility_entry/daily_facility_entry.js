// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Daily Facility Entry (BR-13): everyone present on a date. The Head of Operations sees counts only.

frappe.query_reports["Daily Facility Entry"] = {
	filters: [
		{
			fieldname: "date",
			label: __("Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "facility",
			label: __("Facility"),
			fieldtype: "Link",
			options: "Healthcare Service Unit",
			get_query: () => ({ filters: { is_group: 1 } }),
		},
	],

	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (
			column.fieldname === "screening_outcome" &&
			data &&
			data.screening_outcome === "Outside Expected Range"
		) {
			value = `<span class="text-danger">${value}</span>`;
		}
		return value;
	},
};
