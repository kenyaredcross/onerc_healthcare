// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt

frappe.query_reports["Consent Exceptions"] = {
	filters: [
		{
			fieldname: "outcome",
			label: __("Consent"),
			fieldtype: "Select",
			options: ["", "No consent recorded", "Withheld", "Unable to Consent", "Withdrawn"].join("\n"),
		},
		{ fieldname: "patient", label: __("Patient"), fieldtype: "Link", options: "Patient" },
		{ fieldname: "visited_since", label: __("Visited Since"), fieldtype: "Date" },
		{ fieldname: "with_visits_only", label: __("Patients With Visits Only"), fieldtype: "Check", default: 0 },
	],
};
