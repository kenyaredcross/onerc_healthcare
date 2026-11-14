// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Service Delivered (R3, SDD §5.12.7): recorded from a visit; voided with a reason, never deleted.

const KRCS_SERVICES = "onerc_healthcare.community.services";

frappe.ui.form.on("Service Delivered", {
	setup(frm) {
		frm.set_query("service_type", () => ({ filters: { enabled: 1 } }));
	},

	refresh(frm) {
		// The archive flag changes only through the Archive action, with a reason (M6).
		if (frm.fields_dict.krcs_archived) frm.set_df_property("krcs_archived", "read_only", 1);
		if (!frm.is_new() && frm.fields_dict.krcs_archived && krcs_may_act_on_service(frm)) {
			const archived = !!frm.doc.krcs_archived;
			frm.add_custom_button(archived ? __("Un-archive") : __("Archive"), () => {
				const d = new frappe.ui.Dialog({
					title: archived ? __("Un-archive {0}", [frm.doc.name]) : __("Archive {0}", [frm.doc.name]),
					fields: [{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 }],
					primary_action_label: archived ? __("Un-archive") : __("Archive"),
					primary_action(values) {
						d.hide();
						frappe
							.call({
								method: KRCS_SERVICES + ".set_service_archived",
								type: "POST",
								args: { name: frm.doc.name, archived: archived ? 0 : 1, reason: values.reason },
								freeze: true,
							})
							.then(() => frm.reload_doc());
					},
				});
				d.show();
			});
		}
		if (frm.is_new() || frm.doc.status !== "Active") {
			if (frm.doc.status === "Voided") frm.disable_form();
			return;
		}
		if (!krcs_may_act_on_service(frm)) return;
		frm.add_custom_button(__("Void Service"), () => {
			const d = new frappe.ui.Dialog({
				title: __("Void Service {0}", [frm.doc.name]),
				fields: [{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 }],
				primary_action_label: __("Void"),
				primary_action(values) {
					d.hide();
					frappe
						.call({
							method: KRCS_SERVICES + ".void_service",
							type: "POST",
							args: { name: frm.doc.name, reason: values.reason },
							freeze: true,
						})
						.then(() => frm.reload_doc());
				},
			});
			d.show();
		});
	},
});

// The recorder (Clinical Officer / Mental Health Officer) or the Medical In-Charge; the server checks again.
function krcs_may_act_on_service(frm) {
	const user = frappe.session.user;
	return (
		user === "Administrator" ||
		frappe.user.has_role("Medical In-Charge") ||
		(frappe.user.has_role(["Clinical Officer", "Mental Health Officer"]) &&
			[frm.doc.provided_by, frm.doc.owner].includes(user))
	);
}
