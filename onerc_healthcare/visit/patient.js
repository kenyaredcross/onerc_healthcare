// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Patient form: start the visit without leaving the patient (registration in one place).
// Registration runs: save the new patient -> record consent (privacy/patient.js) -> start the visit (here).
// The visit is inserted with the user's own permissions, so every server rule still applies (one open visit
// per patient, facility, attendance, intake answers).

frappe.provide("krcs.visit");

krcs.visit.can_start = (frm) =>
	!frm.is_new() &&
	!frm.doc.krcs_merged_into &&
	frm.doc.status !== "Disabled" &&
	frappe.model.can_create("Patient Visit");

krcs.visit.open_visit = (frm) =>
	frappe.xcall("onerc_healthcare.visit.queue.get_open_visit", { patient: frm.doc.name }, "GET");

krcs.visit.start_visit_dialog = function (frm, intro) {
	krcs.visit.open_visit(frm).then((open) => {
		if (open) {
			frappe.msgprint({
				title: __("Visit Already Open"),
				indicator: "orange",
				message: __("{0} already has an open visit {1} ({2}). Continue that visit, or close it first.", [
					frappe.utils.escape_html(frm.doc.patient_name || frm.doc.name),
					`<a href="/app/patient-visit/${encodeURIComponent(open.name)}">${open.name}</a>`,
					__(open.status),
				]),
			});
			return;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Start Visit"),
			fields: [
				{ fieldtype: "HTML", fieldname: "intro" },
				{
					fieldname: "facility",
					fieldtype: "Link",
					options: "Healthcare Service Unit",
					label: __("Facility"),
					reqd: 1,
					default:
						frm.doc.krcs_registration_facility ||
						frappe.defaults.get_user_default("Healthcare Service Unit"),
					get_query: () => ({ filters: { is_group: 1, parent_healthcare_service_unit: ["is", "set"] } }),
				},
				{
					fieldname: "seen_in_smartcare",
					fieldtype: "Check",
					label: __("Seen in SmartCare before"),
					description: __("Tick when the patient was seen here this year before the EMR (counts as a re-attendance)."),
				},
				{ fieldname: "referred_in", fieldtype: "Check", label: __("Referred in from another facility") },
				{
					fieldname: "referred_in_from",
					fieldtype: "Link",
					options: "Referral Facility",
					label: __("Referred in from"),
					depends_on: "eval:doc.referred_in",
					mandatory_depends_on: "eval:doc.referred_in",
				},
			],
			primary_action_label: __("Start Visit"),
			primary_action(values) {
				dialog.disable_primary_action();
				frappe
					.xcall("frappe.client.insert", {
						doc: {
							doctype: "Patient Visit",
							patient: frm.doc.name,
							facility: values.facility,
							seen_in_smartcare: values.seen_in_smartcare ? 1 : 0,
							referred_in: values.referred_in ? 1 : 0,
							referred_in_from: values.referred_in ? values.referred_in_from : null,
						},
					})
					.then((visit) => {
						dialog.hide();
						frappe.show_alert(
							{
								message: __("Visit {0} started. Queue number {1}.", [visit.name, visit.queue_number || "-"]),
								indicator: "green",
							},
							8
						);
						frm.reload_doc();
					})
					.finally(() => dialog.enable_primary_action());
			},
			secondary_action_label: __("Not now"),
			secondary_action() {
				dialog.hide();
			},
		});
		dialog.fields_dict.intro.$wrapper.html(
			intro ? `<p class="text-muted">${frappe.utils.escape_html(intro)}</p>` : ""
		);
		dialog.show();
	});
};

frappe.ui.form.on("Patient", {
	refresh(frm) {
		if (!krcs.visit.can_start(frm)) return;
		krcs.visit.open_visit(frm).then((open) => {
			if (open) {
				frm.dashboard.add_indicator(__("Open visit {0}: {1}", [open.name, __(open.status)]), "blue");
				frm.add_custom_button(__("Open Visit"), () => frappe.set_route("Form", "Patient Visit", open.name));
			} else {
				frm.add_custom_button(__("Start Visit"), () => krcs.visit.start_visit_dialog(frm)).addClass(
					"btn-primary"
				);
			}
		});
	},

	// Last step of registering a new patient: triggered by privacy/patient.js once consent is recorded or
	// skipped.
	krcs_registration_next(frm) {
		if (krcs.visit.can_start(frm)) {
			krcs.visit.start_visit_dialog(frm, __("Patient registered. Start their visit now?"));
		}
	},
});
