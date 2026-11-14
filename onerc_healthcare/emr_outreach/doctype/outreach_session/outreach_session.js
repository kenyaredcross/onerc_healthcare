// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Outreach Session (OUT-03): planned by the HIS Officer / Medical In-Charge; status changes through actions only.

const KRCS_OUTREACH_SESSION = "onerc_healthcare.outreach.session";
const KRCS_NOT_OFFERED = ["Mental Health", "Reception", "Triage", "Laboratory", "Pharmacy", "Health Information"];

frappe.ui.form.on("Outreach Session", {
	setup(frm) {
		frm.set_query("catchment_area", () => ({ filters: { disabled: 0 } }));
		frm.set_query("user", "team", () => ({ filters: { enabled: 1, user_type: "System User" } }));
		frm.set_query("department", "departments", () => ({
			filters: { name: ["not in", KRCS_NOT_OFFERED] },
		}));
	},

	catchment_area(frm) {
		if (!frm.doc.catchment_area) return;
		frappe.db.get_value("Catchment Area", frm.doc.catchment_area, "facility").then((r) => {
			if (r.message && r.message.facility) frm.set_value("facility", r.message.facility);
		});
	},

	refresh(frm) {
		if (frm.is_new()) return;
		const locked = !["Planned", "Active"].includes(frm.doc.status);
		["team", "departments", "start_date", "end_date"].forEach((f) => frm.set_df_property(f, "read_only", locked));
		["facility", "catchment_area"].forEach((f) =>
			frm.set_df_property(f, "read_only", frm.doc.status !== "Planned")
		);
		const actions = (frm.doc.__onload && frm.doc.__onload.allowed_actions) || [];
		actions.forEach((a) => {
			frm.add_custom_button(a.label, () => krcs_session_action(frm, a), __("Actions"));
		});
	},
});

function krcs_session_action(frm, a) {
	const run = (reason) =>
		frappe
			.call({
				method: KRCS_OUTREACH_SESSION + ".apply_action",
				type: "POST",
				args: { session: frm.doc.name, action: a.action, reason: reason || null },
				freeze: true,
			})
			.then(() => frm.reload_doc());
	if (!a.needs_reason) {
		frappe.confirm(__("{0} for {1}?", [a.label, frm.doc.name]), () => run());
		return;
	}
	const d = new frappe.ui.Dialog({
		title: __("{0}: {1}", [a.label, frm.doc.name]),
		fields: [{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 }],
		primary_action_label: a.label,
		primary_action(values) {
			d.hide();
			run(values.reason);
		},
	});
	d.show();
}
