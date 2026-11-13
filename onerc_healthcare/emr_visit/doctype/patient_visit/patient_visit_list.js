// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Reception queue (FR-6, BR-2): status indicator with waiting time. The list refreshes on the built-in
// `list_update` realtime event; a one-minute timer keeps the waiting times current.

const PATIENT_VISIT_COLORS = {
	Registered: "orange",
	"In Triage": "yellow",
	"Routed to Department": "blue",
	"In Consultation": "green",
	"Awaiting Laboratory": "purple",
	"Awaiting Pharmacy": "red",
	Discharged: "gray",
	Referred: "gray",
};

frappe.listview_settings["Patient Visit"] = {
	add_fields: [
		"status",
		"arrived_at",
		"status_changed_at",
		"closed_at",
		"lab_open",
		"pharmacy_open",
		"queue_number",
		"current_department",
		"visit_date",
	],
	hide_name_column: false,

	get_indicator(doc) {
		const color = PATIENT_VISIT_COLORS[doc.status] || "gray";
		const filter = `status,=,${doc.status}`;
		if (["Discharged", "Referred"].includes(doc.status)) {
			return [__(doc.status), color, filter];
		}
		// Open visit left over from an earlier day: shown separately, never auto-closed.
		if (doc.visit_date && doc.visit_date < frappe.datetime.get_today()) {
			return [
				__("{0} · open since {1}", [__(doc.status), frappe.datetime.str_to_user(doc.visit_date)]),
				"darkgrey",
				`visit_date,<,${frappe.datetime.get_today()}`,
			];
		}
		const since = doc.arrived_at;
		const minutes = since
			? Math.max(0, Math.floor(moment().diff(frappe.datetime.str_to_obj(since), "minutes")))
			: 0;
		let label = __("{0} · {1} min", [__(doc.status), minutes]);
		const tasks = [];
		if (doc.lab_open) tasks.push(__("Lab"));
		if (doc.pharmacy_open) tasks.push(__("Pharmacy"));
		if (tasks.length) label += ` (${tasks.join(" + ")})`;
		return [label, color, filter];
	},

	onload(listview) {
		if (listview.__visit_timer) return;
		listview.__visit_timer = setInterval(() => {
			if (frappe.get_route_str().startsWith("List/Patient Visit")) {
				listview.refresh();
			}
		}, 60 * 1000);
	},
};
