// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt

frappe.query_reports["Access Audit"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -7),
			reqd: 1,
		},
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date", default: frappe.datetime.get_today(), reqd: 1 },
		{ fieldname: "patient", label: __("Patient ID"), fieldtype: "Data" },
		{ fieldname: "user", label: __("User"), fieldtype: "Link", options: "User" },
		{
			fieldname: "action",
			label: __("Action"),
			fieldtype: "Select",
			options: [
				"",
				"Open",
				"Open (API)",
				"Read (API)",
				"List",
				"Print",
				"Export",
				"Report",
				"Download",
				"Create",
				"Change",
			].join("\n"),
		},
		{ fieldname: "reference_doctype", label: __("Record Type"), fieldtype: "Link", options: "DocType" },
		{ fieldname: "sensitive_only", label: __("Sensitive Only"), fieldtype: "Check", default: 0 },
	],
};
