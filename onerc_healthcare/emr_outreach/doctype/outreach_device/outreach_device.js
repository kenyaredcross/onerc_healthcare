// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Outreach Device: registered by enrolment; the HIS Officer, Medical In-Charge or EMR System Administrator
// revokes a lost or retired device (OUT-25), which ends every sync grant on it.

frappe.ui.form.on("Outreach Device", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.status !== "Active") return;
		const roles = ["HIS Officer", "Medical In-Charge", "EMR System Administrator"];
		if (!roles.some((r) => frappe.user.has_role(r)) && frappe.session.user !== "Administrator") return;
		frm.add_custom_button(__("Revoke Device"), () => {
			const d = new frappe.ui.Dialog({
				title: __("Revoke {0}", [frm.doc.label || frm.doc.name]),
				fields: [
					{
						fieldtype: "HTML",
						options: `<p>${__(
							"The device can no longer sync or download a package: every team member's grant on it is revoked. Records it has not synced cannot reach the server."
						)}</p>`,
					},
					{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
				],
				primary_action_label: __("Revoke"),
				primary_action(values) {
					d.hide();
					frappe
						.call({
							method: "onerc_healthcare.outreach.devices.revoke_device",
							type: "POST",
							args: { device: frm.doc.name, reason: values.reason },
							freeze: true,
						})
						.then(() => frm.reload_doc());
				},
			});
			d.show();
		});
	},
});
