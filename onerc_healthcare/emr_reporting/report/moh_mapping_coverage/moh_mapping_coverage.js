// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt

frappe.query_reports["MOH Mapping Coverage"] = {
	filters: [
		{
			fieldname: "view",
			label: __("View"),
			fieldtype: "Select",
			options: ["Categories", "Codes used"].join("\n"),
			default: "Categories",
		},
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
};
