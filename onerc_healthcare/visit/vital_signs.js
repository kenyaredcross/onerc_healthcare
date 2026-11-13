// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Vital Signs triage additions (FR-9): link to the patient's open visit and "Route to Department".
// Wired by hooks.py `doctype_js = {"Vital Signs": "visit/vital_signs.js"}`.

frappe.ui.form.on("Vital Signs", {
	setup(frm) {
		frm.set_query("krcs_visit", () => ({
			filters: {
				patient: frm.doc.patient,
				status: ["not in", ["Discharged", "Referred"]],
			},
		}));
	},

	patient(frm) {
		if (!frm.doc.patient || frm.doc.krcs_visit) return;
		frappe.db
			.get_list("Patient Visit", {
				filters: {
					patient: frm.doc.patient,
					status: ["not in", ["Discharged", "Referred"]],
				},
				fields: ["name"],
				order_by: "creation desc",
				limit: 1,
			})
			.then((rows) => {
				if (rows.length) frm.set_value("krcs_visit", rows[0].name);
			});
	},

	refresh(frm) {
		if (frm.doc.docstatus !== 1 || !frm.doc.krcs_visit) return;
		frappe.db.get_value("Patient Visit", frm.doc.krcs_visit, "status").then((r) => {
			const status = r.message && r.message.status;
			if (status !== "In Triage") return;
			frm.add_custom_button(__("Route to Department"), async () => {
				// Reception and Triage are intake departments, never routing targets.
				const exclude = await frappe.xcall(
					"onerc_healthcare.visit.engine.get_non_routable_departments"
				);
				const d = new frappe.ui.Dialog({
					title: __("Route to Department"),
					fields: [
						{
							fieldname: "department",
							fieldtype: "Link",
							options: "Medical Department",
							label: __("Department"),
							reqd: 1,
							get_query: () => ({ filters: { name: ["not in", exclude] } }),
						},
						{ fieldname: "comment", fieldtype: "Small Text", label: __("Comment") },
					],
					primary_action_label: __("Route"),
					primary_action(values) {
						d.hide();
						frappe
							.call({
								method: "onerc_healthcare.visit.engine.apply_action",
								type: "POST",
								args: {
									visit: frm.doc.krcs_visit,
									action: "route",
									department: values.department,
									comment: values.comment || null,
								},
								freeze: true,
							})
							.then((res) => {
								frappe.show_alert({
									message: __("Visit {0} routed to {1}", [
										frm.doc.krcs_visit,
										res.message.current_department,
									]),
									indicator: "green",
								});
								frm.reload_doc();
							});
					},
				});
				d.show();
			}).addClass("btn-primary");
		});
	},
});
