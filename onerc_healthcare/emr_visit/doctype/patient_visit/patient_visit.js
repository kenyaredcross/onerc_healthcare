// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt

frappe.provide("onerc_healthcare.visit");

onerc_healthcare.visit.APPLY_ACTION = "onerc_healthcare.visit.engine.apply_action";

// Run a visit action; route asks for the department, discharge/refer ask for confirmation.
onerc_healthcare.visit.run_action = function (visit, action, label, needs_department, after) {
	const call = (values) =>
		frappe
			.call({
				method: onerc_healthcare.visit.APPLY_ACTION,
				type: "POST",
				args: {
					visit: visit,
					action: action,
					department: values.department || null,
					comment: values.comment || null,
				},
				freeze: true,
				freeze_message: __("Updating visit..."),
			})
			.then((r) => {
				if (r.message) {
					frappe.show_alert({
						message: __("Visit {0}: {1}", [visit, __(r.message.status)]),
						indicator: "green",
					});
				}
				after && after(r.message);
			});

	if (needs_department) {
		const d = new frappe.ui.Dialog({
			title: __(label),
			fields: [
				{
					fieldname: "department",
					fieldtype: "Link",
					options: "Medical Department",
					label: __("Department"),
					reqd: 1,
				},
				{ fieldname: "comment", fieldtype: "Small Text", label: __("Comment") },
			],
			primary_action_label: __("Route"),
			primary_action(values) {
				d.hide();
				call(values);
			},
		});
		d.show();
		return;
	}
	if (["discharge", "refer"].includes(action)) {
		const d = new frappe.ui.Dialog({
			title: __(label),
			fields: [
				{
					fieldname: "comment",
					fieldtype: "Small Text",
					label: action === "refer" ? __("Referral reason") : __("Comment"),
					reqd: action === "refer" ? 1 : 0,
				},
			],
			primary_action_label: __(label),
			primary_action(values) {
				d.hide();
				call(values);
			},
		});
		d.show();
		return;
	}
	call({});
};

onerc_healthcare.visit.minutes_since = function (value) {
	if (!value) return 0;
	return Math.max(0, Math.floor(moment().diff(frappe.datetime.str_to_obj(value), "minutes")));
};

frappe.ui.form.on("Patient Visit", {
	setup(frm) {
		frm.set_query("facility", () => ({
			filters: { is_group: 1, parent_healthcare_service_unit: ["is", "set"] },
		}));
	},

	refresh(frm) {
		if (frm.is_new()) return;
		const actions = (frm.doc.__onload || {}).available_actions || [];
		actions.forEach((a) => {
			const btn = frm.add_custom_button(
				__(a.label),
				() =>
					onerc_healthcare.visit.run_action(
						frm.doc.name,
						a.action,
						a.label,
						a.needs_department,
						() => frm.reload_doc()
					),
				a.is_primary ? null : __("Actions")
			);
			if (a.is_primary) btn.addClass("btn-primary");
		});

		const triage_actions = actions.filter((a) => ["start_triage", "route"].includes(a.action));
		if (triage_actions.length) {
			frm.add_custom_button(__("Record Vital Signs"), () => {
				frappe.new_doc("Vital Signs", {
					patient: frm.doc.patient,
					krcs_visit: frm.doc.name,
					company: frm.doc.company,
				});
			});
		}

		const closed = ["Discharged", "Referred"].includes(frm.doc.status);
		if (!closed) {
			const parts = [
				__("Waiting {0} min since arrival; {1} min in {2}.", [
					onerc_healthcare.visit.minutes_since(frm.doc.arrived_at),
					onerc_healthcare.visit.minutes_since(frm.doc.status_changed_at || frm.doc.arrived_at),
					__(frm.doc.status),
				]),
			];
			if (frm.doc.lab_open) parts.push(__("Laboratory task open."));
			if (frm.doc.pharmacy_open) parts.push(__("Pharmacy task open."));
			frm.set_intro(parts.join(" "), "blue");
		}
	},
});
