// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Due Immunisations (CON-16): KEPI doses due or overdue in a window, for defaulter tracing.

frappe.query_reports["Due Immunisations"] = {
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
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), 7),
			reqd: 1,
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: ["Due and Overdue", "Due", "Overdue"],
			default: "Due and Overdue",
		},
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (!data || column.fieldname !== "status") return value;
		const colour = { Due: "orange", Overdue: "red" }[data.status];
		return colour ? `<span class="indicator-pill ${colour}">${value}</span>` : value;
	},
};
