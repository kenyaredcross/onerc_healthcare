// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt

frappe.provide("onerc_healthcare.visit");

onerc_healthcare.visit.APPLY_ACTION = "onerc_healthcare.visit.engine.apply_action";

// Run a visit action. `a` is an entry of `available_actions` ({action, label, needs_department,
// needs_comment}); `opts.exclude_departments` are the intake departments hidden from the picker.
onerc_healthcare.visit.run_action = function (visit, a, after, opts = {}) {
	const call = (values) =>
		frappe
			.call({
				method: onerc_healthcare.visit.APPLY_ACTION,
				type: "POST",
				args: {
					visit: visit,
					action: a.action,
					department: values.department || null,
					comment: values.comment || null,
					without_vitals_reason: values.without_vitals_reason || null,
					closure_reason: values.closure_reason || null,
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

	const asks_comment =
		a.needs_comment || a.needs_closure_reason || ["discharge", "refer"].includes(a.action);
	if (!a.needs_department && !asks_comment) {
		call({});
		return;
	}
	const exclude = [...(opts.exclude_departments || [])];
	if (a.action === "reroute" && opts.current_department) exclude.push(opts.current_department);
	const fields = [];
	if (a.needs_department) {
		fields.push({
			fieldname: "department",
			fieldtype: "Link",
			options: "Medical Department",
			label: __("Department"),
			reqd: 1,
			get_query: () => ({ filters: { name: ["not in", exclude] } }),
		});
	}
	// FR-9: routing needs submitted triage vitals, or an emergency reason.
	if (a.action === "route" && opts.has_triage_vitals === false) {
		fields.push(
			{
				fieldname: "no_vitals_note",
				fieldtype: "HTML",
				options: `<div class="alert alert-warning" style="margin-bottom: 0">${__(
					"No vital signs recorded. Record Vital Signs first, or give a reason to route without them."
				)}</div>`,
			},
			{
				fieldname: "without_vitals_reason",
				fieldtype: "Small Text",
				label: __("Reason for routing without vital signs"),
				reqd: 1,
			}
		);
	}
	if (a.needs_closure_reason) {
		fields.push({
			fieldname: "closure_reason",
			fieldtype: "Select",
			label: __("Reason"),
			options: ["", ...(opts.closure_reasons || [])].join("\n"),
			default: opts.default_closure_reason || "",
			reqd: 1,
		});
	}
	fields.push({
		fieldname: "comment",
		fieldtype: "Small Text",
		label: a.needs_closure_reason ? __("Details") : a.needs_comment ? __("Reason") : __("Comment"),
		reqd: a.needs_comment ? 1 : 0,
	});
	const d = new frappe.ui.Dialog({
		title: __(a.label),
		fields: fields,
		primary_action_label: __(a.label),
		primary_action(values) {
			d.hide();
			call(values);
		},
	});
	d.show();
};

// New visit: the patient may still have an open visit (any date). Offer to close it, then retry the save.
onerc_healthcare.visit.offer_close_previous = function (frm) {
	if (!frm.doc.patient) return;
	frappe
		.xcall("onerc_healthcare.visit.queue.get_open_visit", { patient: frm.doc.patient })
		.then((open) => {
			if (!open) return;
			const when = frappe.datetime.str_to_user(open.visit_date);
			if (!open.can_close) {
				frappe.msgprint(
					__("This patient already has an open visit {0} ({1}, {2}). Ask a colleague to close it.", [
						open.name,
						__(open.status),
						when,
					])
				);
				return;
			}
			onerc_healthcare.visit.run_action(
				open.name,
				{
					action: "close_visit",
					label: __("Close previous visit {0} ({1}, {2})", [open.name, __(open.status), when]),
					needs_comment: true,
					needs_closure_reason: true,
				},
				() => {
					if (frm.doc.facility) frm.save();
				},
				{
					closure_reasons: open.closure_reasons || [],
					default_closure_reason: open.default_closure_reason,
				}
			);
		});
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

	patient(frm) {
		if (frm.is_new()) onerc_healthcare.visit.offer_close_previous(frm);
	},

	refresh(frm) {
		if (frm.is_new()) return;
		// Refer is done from the consultation (it creates the Referral record, BR-14), not the visit.
		const actions = ((frm.doc.__onload || {}).available_actions || []).filter(
			(a) => a.action !== "refer"
		);
		actions.forEach((a) => {
			if (a.action === "start_consultation") {
				// Starting a consultation creates (or reopens) the visit's Patient Encounter (B1).
				const btn = frm.add_custom_button(__(a.label), () =>
					frappe
						.call({
							method: "onerc_healthcare.clinical.consultation.start_consultation",
							type: "POST",
							args: { visit: frm.doc.name },
							freeze: true,
						})
						.then((r) => {
							const enc = r.message && (r.message.encounter || r.message.name || r.message);
							if (enc) frappe.set_route("Form", "Patient Encounter", enc);
						})
				);
				btn.addClass("btn-primary");
				return;
			}
			const btn = frm.add_custom_button(
				__(a.label),
				() =>
					onerc_healthcare.visit.run_action(frm.doc.name, a, () => frm.reload_doc(), {
						exclude_departments: (frm.doc.__onload || {}).non_routable_departments || [],
						current_department: frm.doc.current_department,
						has_triage_vitals: !!(frm.doc.__onload || {}).has_triage_vitals,
						closure_reasons: (frm.doc.__onload || {}).closure_reasons || [],
					}),
				a.is_primary ? null : __("Actions")
			);
			if (a.is_primary) btn.addClass("btn-primary");
		});

		if (frm.doc.encounter) {
			frm.add_custom_button(__("Open Consultation"), () =>
				frappe.set_route("Form", "Patient Encounter", frm.doc.encounter)
			);
		}

		if ((frm.doc.__onload || {}).can_recheck_tasks) {
			frm.add_custom_button(
				__("Recheck Tasks"),
				() =>
					frappe
						.call({
							method: "onerc_healthcare.visit.tasks.recheck_tasks",
							type: "POST",
							args: { visit: frm.doc.name },
							freeze: true,
						})
						.then((r) => {
							const raised = (r.message && r.message.raised) || [];
							frappe.show_alert({
								message: raised.length
									? __("Updated: {0}", [raised.join(", ")])
									: __("No task changes"),
								indicator: raised.length ? "green" : "blue",
							});
							frm.reload_doc();
						}),
				__("Actions")
			);
		}

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
