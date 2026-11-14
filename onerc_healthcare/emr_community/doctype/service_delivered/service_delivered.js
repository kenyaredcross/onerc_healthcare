// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Service Delivered (R3, SDD §5.12.7): recorded from a visit; voided with a reason, never deleted.

const KRCS_SERVICES = "onerc_healthcare.community.services";

frappe.ui.form.on("Service Delivered", {
	setup(frm) {
		frm.set_query("service_type", () => ({ filters: { enabled: 1 } }));
	},

	refresh(frm) {
		if (frm.is_new() || frm.doc.status !== "Active") {
			if (frm.doc.status === "Voided") frm.disable_form();
			return;
		}
		const user = frappe.session.user;
		const may_void =
			user === "Administrator" ||
			frappe.user.has_role("Medical In-Charge") ||
			(frappe.user.has_role(["Clinical Officer", "Mental Health Officer"]) &&
				[frm.doc.provided_by, frm.doc.owner].includes(user));
		if (!may_void) return;
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
