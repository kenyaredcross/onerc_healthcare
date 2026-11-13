// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Data Access Request list: overdue indicator (FR-38). Overdue = due date before today and not Closed.

frappe.listview_settings["Data Access Request"] = {
	add_fields: ["status", "due_date"],
	get_indicator(doc) {
		const today = frappe.datetime.get_today();
		if (doc.status !== "Closed" && doc.due_date && doc.due_date < today) {
			return [__("Overdue"), "red", "status,!=,Closed"];
		}
		const colours = {
			Received: "orange",
			"Identity Verified": "yellow",
			Approved: "blue",
			Rejected: "grey",
			"Response Ready": "purple",
			Closed: "green",
		};
		return [__(doc.status), colours[doc.status] || "grey", `status,=,${doc.status}`];
	},
	onload(listview) {
		listview.page.add_inner_button(__("Overdue"), () => {
			listview.filter_area.clear(false).then(() => {
				listview.filter_area.add([
					["Data Access Request", "status", "!=", "Closed"],
					["Data Access Request", "due_date", "<", frappe.datetime.get_today()],
				]);
			});
		});
	},
};
