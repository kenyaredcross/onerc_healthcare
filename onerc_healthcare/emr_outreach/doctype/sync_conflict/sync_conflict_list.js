// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt

frappe.listview_settings["Sync Conflict"] = {
	add_fields: ["status", "review_only", "conflict_type"],
	get_indicator(doc) {
		if (doc.status === "Resolved") return [__("Resolved"), "green", "status,=,Resolved"];
		if (doc.review_only) return [__("Review"), "blue", "status,=,Open|review_only,=,1"];
		return [__("Open"), "orange", "status,=,Open"];
	},
};
