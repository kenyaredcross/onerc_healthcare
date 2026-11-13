// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Referral form (B1): status actions from the server's state machine (clinical/referral.py).

frappe.ui.form.on("Referral", {
	setup(frm) {
		frm.krcs_non_routable = [];
		frappe
			.xcall("onerc_healthcare.visit.engine.get_non_routable_departments")
			.then((r) => (frm.krcs_non_routable = r || []));
		frm.set_query("to_department", () => ({
			filters: { name: ["not in", frm.krcs_non_routable] },
		}));
		frm.set_query("to_facility", () => ({ filters: { is_active: 1 } }));
		frm.set_query("source_encounter", () => ({ filters: { docstatus: 0 } }));
	},

	refresh(frm) {
		if (frm.is_new()) return;
		const colors = {
			Draft: "gray",
			Sent: "blue",
			Received: "orange",
			Completed: "green",
			"Lost to Follow-up": "red",
			Cancelled: "red",
		};
		frm.page.set_indicator(__(frm.doc.status), colors[frm.doc.status] || "gray");
		const actions = (frm.doc.__onload && frm.doc.__onload.available_actions) || [];
		actions.forEach((a) => {
			const btn = frm.add_custom_button(__(a.label), () => run_action(frm, a));
			if (a.action === "send") btn.addClass("btn-primary");
		});
		if (frm.doc.target_visit) {
			frm.add_custom_button(__("Target Visit"), () =>
				frappe.set_route("Form", "Patient Visit", frm.doc.target_visit),
			);
		}
	},
});

function run_action(frm, a) {
	const call = (args) =>
		frappe
			.call({
				method: "onerc_healthcare.clinical.referral.apply_action",
				type: "POST",
				args: Object.assign({ referral: frm.doc.name, action: a.action }, args || {}),
				freeze: true,
			})
			.then(() => frm.reload_doc());

	if (a.needs_outcome) {
		const d = new frappe.ui.Dialog({
			title: __(a.label),
			fields: [
				{ fieldname: "outcome", fieldtype: "Small Text", label: __("Outcome"), reqd: 1 },
				{
					fieldname: "outcome_date",
					fieldtype: "Date",
					label: __("Outcome Date"),
					default: frappe.datetime.get_today(),
				},
			],
			primary_action_label: __("Save"),
			primary_action(v) {
				d.hide();
				call(v);
			},
		});
		d.show();
	} else if (a.needs_comment) {
		frappe.prompt(
			{ fieldname: "comment", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
			(v) => call(v),
			__(a.label),
		);
	} else if (a.action === "send") {
		frappe.confirm(
			__("Sending completes the consultation and closes the visit as Referred. Continue?"),
			() => call(),
		);
	} else {
		frappe.confirm(__("{0}?", [__(a.label)]), () => call());
	}
}
