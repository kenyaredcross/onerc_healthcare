// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Maternity Admission list: status indicators (Phase 5M M1).

frappe.listview_settings["Maternity Admission"] = {
	add_fields: ["status", "krcs_archived"],
	get_indicator(doc) {
		if (doc.krcs_archived) return [__("Archived"), "gray", "krcs_archived,=,1"];
		const colors = { Admitted: "blue", "In Labour": "orange", Delivered: "purple", Discharged: "green" };
		return [__(doc.status), colors[doc.status] || "gray", `status,=,${doc.status}`];
	},
};
