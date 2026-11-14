// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt

frappe.listview_settings["MOH Report Run"] = {
	add_fields: ["status", "generation_status"],
	get_indicator(doc) {
		if (doc.generation_status === "Queued") return [__("Being generated"), "orange", "generation_status,=,Queued"];
		const colours = { Draft: "red", Generated: "blue", Submitted: "green", Superseded: "gray", Void: "darkgrey" };
		return [__(doc.status), colours[doc.status] || "gray", `status,=,${doc.status}`];
	},
};
