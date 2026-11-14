// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Community Activity Record (R3, SDD §5.12.7): group counts only. Cancelling needs a reason.

const KRCS_ACTIVITIES = "onerc_healthcare.community.activities";

frappe.ui.form.on("Community Activity Record", {
	setup(frm) {
		frm.set_query("activity_type", () => ({ filters: { enabled: 1 } }));
		frm.set_query("facility", () => ({
			filters: { is_group: 1, parent_healthcare_service_unit: ["is", "set"] },
		}));
	},

	onload(frm) {
		if (frm.is_new() && !frm.doc.lead_staff) frm.set_value("lead_staff", frappe.session.user);
	},

	refresh(frm) {
		// The standard Cancel has no reason; ask for one and cancel on the server.
		frm.savecancel = function () {
			const d = new frappe.ui.Dialog({
				title: __("Cancel {0}", [frm.doc.name]),
				fields: [{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 }],
				primary_action_label: __("Cancel Record"),
				primary_action(values) {
					d.hide();
					frappe
						.call({
							method: KRCS_ACTIVITIES + ".cancel_activity",
							type: "POST",
							args: { name: frm.doc.name, reason: values.reason },
							freeze: true,
						})
						.then(() => frm.reload_doc());
				},
			});
			d.show();
		};
	},

	participants_male(frm) {
		krcs_fill_total(frm);
	},
	participants_female(frm) {
		krcs_fill_total(frm);
	},
});

function krcs_fill_total(frm) {
	const counted = (frm.doc.participants_male || 0) + (frm.doc.participants_female || 0);
	if ((frm.doc.participants_total || 0) < counted) frm.set_value("participants_total", counted);
}
