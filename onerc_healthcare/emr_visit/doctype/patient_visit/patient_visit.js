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
					outcome: values.outcome || null,
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

	const asks_comment = a.needs_comment || a.needs_closure_reason;
	if (!a.needs_department && !asks_comment) {
		call({});
		return;
	}
	const exclude = [...(opts.exclude_departments || [])];
	if (a.action === "reroute" && opts.current_department) exclude.push(opts.current_department);
	const fields = [];
	const ref = {}; // the dialog, once built (field callbacks may fire while it is being made)
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
		fields.push(
			{
				fieldname: "closure_reason",
				fieldtype: "Select",
				label: __("Reason"),
				options: ["", ...(opts.closure_reasons || [])].join("\n"),
				default: opts.default_closure_reason || "",
				reqd: 1,
				onchange: () =>
					ref.dialog && onerc_healthcare.visit.update_outcome_field(ref.dialog, visit, a.action),
			},
			// Phase 3: the closure reason implies the outcome; "Other" lets the user choose.
			{ fieldname: "outcome_hint", fieldtype: "HTML" },
			{
				fieldname: "outcome",
				fieldtype: "Select",
				label: __("Outcome"),
				options: "",
				hidden: 1,
			}
		);
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
	ref.dialog = d;
	d.show();
	if (a.needs_closure_reason) onerc_healthcare.visit.update_outcome_field(d, visit, a.action);
};

// Close Visit dialog: show the outcome the reason implies, or the choice for "Other" (engine rules).
onerc_healthcare.visit.update_outcome_field = function (d, visit, action) {
	const reason = d.get_value("closure_reason");
	const outcome = d.get_field("outcome");
	const hint = d.get_field("outcome_hint");
	if (!reason) {
		d.set_df_property("outcome", "hidden", 1);
		hint.$wrapper.html("");
		return;
	}
	frappe
		.call({
			method: "onerc_healthcare.visit.outcome.get_outcome_choices",
			type: "GET",
			args: { visit: visit, action: action, closure_reason: reason },
		})
		.then((r) => {
			const info = r.message || {};
			const choices = info.choices || [];
			if (d.get_value("closure_reason") !== reason) return; // changed meanwhile
			if (choices.length > 1) {
				d.set_df_property("outcome", "options", ["", ...choices].join("\n"));
				d.set_df_property("outcome", "hidden", 0);
				d.set_df_property("outcome", "description", info.keeps_current
					? __("Leave empty to keep the recorded outcome ({0}).", [__(info.current)])
					: __("Leave empty for {0}.", [__(info.default || "Not applicable")]));
				d.set_value("outcome", "");
				hint.$wrapper.html("");
			} else {
				d.set_value("outcome", "");
				d.set_df_property("outcome", "hidden", 1);
				const shown = info.keeps_current ? info.current : choices[0];
				hint.$wrapper.html(
					shown
						? `<p class="text-muted">${__("Outcome: {0}", [frappe.utils.escape_html(__(shown))])}</p>`
						: ""
				);
			}
		});
};

// Attendance, referral-in and outcome at a glance (Phase 3).
onerc_healthcare.visit.show_capture_indicators = function (frm) {
	if (frm.doc.attendance_type) {
		frm.dashboard.add_indicator(
			__("Attendance: {0} ({1})", [__(frm.doc.attendance_type), __(frm.doc.attendance_basis || "")]),
			frm.doc.attendance_type === "New" ? "green" : "blue"
		);
	}
	if (frm.doc.referred_in) {
		frm.dashboard.add_indicator(
			frm.doc.referred_in_from
				? __("Referred in from {0}", [frm.doc.referred_in_from])
				: __("Referred in"),
			"orange"
		);
	}
	if (frm.doc.outcome) {
		frm.dashboard.add_indicator(__("Outcome: {0}", [__(frm.doc.outcome)]), "gray");
	}
};

// Corrections and Services buttons (visibility from the server; every endpoint checks again).
onerc_healthcare.visit.add_capture_buttons = function (frm) {
	const name = frm.doc.name;
	frappe
		.call({
			method: "onerc_healthcare.visit.outcome.get_visit_capture",
			type: "GET",
			args: { visit: frm.doc.name },
		})
		.then((r) => {
			const c = r.message || {};
			if (frm.doc.name !== name) return; // another record opened meanwhile
			if (c.can_correct_attendance) {
				frm.add_custom_button(
					__("Correct Attendance"),
					() => onerc_healthcare.visit.correct_attendance(frm),
					__("Corrections")
				);
			}
			if (c.can_correct_referred_in) {
				frm.add_custom_button(
					__("Correct Referred In"),
					() => onerc_healthcare.visit.correct_referred_in(frm),
					__("Corrections")
				);
			}
			if (c.can_view_services) {
				frm.add_custom_button(__("Services"), () =>
					onerc_healthcare.visit.show_services(frm, c.can_record_services)
				);
			}
		});
};

onerc_healthcare.visit.correct_attendance = function (frm) {
	const d = new frappe.ui.Dialog({
		title: __("Correct Attendance"),
		fields: [
			{
				fieldname: "attendance_type",
				fieldtype: "Select",
				label: __("Attendance"),
				options: "New\nRe-attendance",
				default: frm.doc.attendance_type === "New" ? "Re-attendance" : "New",
				reqd: 1,
			},
			{
				fieldname: "reason",
				fieldtype: "Small Text",
				label: __("Reason"),
				reqd: 1,
				description: __("For example: seen here earlier this year on paper or in SmartCare."),
			},
		],
		primary_action_label: __("Correct"),
		primary_action(values) {
			d.hide();
			frappe
				.call({
					method: "onerc_healthcare.visit.attendance.correct_attendance",
					type: "POST",
					args: { visit: frm.doc.name, attendance_type: values.attendance_type, reason: values.reason },
					freeze: true,
				})
				.then(() => frm.reload_doc());
		},
	});
	d.show();
};

onerc_healthcare.visit.correct_referred_in = function (frm) {
	const d = new frappe.ui.Dialog({
		title: __("Correct Referred In"),
		fields: [
			{
				fieldname: "referred_in",
				fieldtype: "Check",
				label: __("Referred in from another facility"),
				default: frm.doc.referred_in ? 1 : 0,
			},
			{
				fieldname: "referred_in_from",
				fieldtype: "Link",
				options: "Referral Facility",
				label: __("Referred From"),
				default: frm.doc.referred_in_from || "",
				depends_on: "eval:doc.referred_in",
			},
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
		],
		primary_action_label: __("Correct"),
		primary_action(values) {
			d.hide();
			frappe
				.call({
					method: "onerc_healthcare.visit.attendance.correct_referred_in",
					type: "POST",
					args: {
						visit: frm.doc.name,
						referred_in: values.referred_in ? 1 : 0,
						referred_in_from: values.referred_in ? values.referred_in_from || null : null,
						reason: values.reason,
					},
					freeze: true,
				})
				.then(() => frm.reload_doc());
		},
	});
	d.show();
};

// Service Delivered is its own record, readable only by the clinical service roles (never a visit field).
onerc_healthcare.visit.show_services = function (frm, can_record) {
	frappe
		.call({
			method: "onerc_healthcare.community.services.get_visit_services",
			type: "GET",
			args: { visit: frm.doc.name },
		})
		.then((r) => {
			const info = r.message || {};
			const esc = frappe.utils.escape_html;
			const rows = (info.services || [])
				.map(
					(s) => `<tr>
						<td><a href="/app/service-delivered/${encodeURIComponent(s.name)}">${esc(s.name)}</a></td>
						<td>${esc(__(s.service_type || ""))}</td>
						<td>${frappe.datetime.str_to_user(s.service_date)}</td>
						<td>${esc(s.provided_by || "")}</td>
						<td>${esc(__(s.status || ""))}</td>
					</tr>`
				)
				.join("");
			const d = new frappe.ui.Dialog({
				title: __("Services for visit {0}", [frm.doc.name]),
				size: "large",
				fields: [
					{
						fieldname: "list",
						fieldtype: "HTML",
						options: rows
							? `<table class="table table-bordered table-sm">
								<thead><tr><th>${__("Record")}</th><th>${__("Service")}</th><th>${__("Date")}</th>
								<th>${__("Provided By")}</th><th>${__("Status")}</th></tr></thead>
								<tbody>${rows}</tbody></table>`
							: `<p class="text-muted">${__("No services recorded for this visit.")}</p>`,
					},
				],
			});
			if (can_record && info.can_record) {
				d.set_primary_action(__("Record Service"), () => {
					d.hide();
					frappe.new_doc("Service Delivered", { visit: frm.doc.name });
				});
			}
			d.show();
		});
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

// Latest screening result of the visit (L2): outcome, time and a link when the user may open it.
onerc_healthcare.visit.screening_text = function (s) {
	if (!s) return "";
	const label = s.can_open
		? `<a href="/app/screening-record/${encodeURIComponent(s.name)}">${frappe.utils.escape_html(
				s.name
		  )}</a>`
		: frappe.utils.escape_html(s.name);
	return __("Screening {0}: {1} ({2}).", [
		label,
		__(s.outcome || "No outcome"),
		frappe.datetime.str_to_user(s.screened_at),
	]);
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

	referred_in(frm) {
		if (frm.is_new() && !frm.doc.referred_in) frm.set_value("referred_in_from", "");
	},

	refresh(frm) {
		// Intake answers (Phase 3) are entered at registration only; corrections go through the
		// Corrections menu (engine-protected after insert).
		["seen_in_smartcare", "referred_in", "referred_in_from"].forEach((f) =>
			frm.set_df_property(f, "read_only", frm.is_new() ? 0 : 1)
		);
		if (frm.is_new()) return;
		onerc_healthcare.visit.show_capture_indicators(frm);
		onerc_healthcare.visit.add_capture_buttons(frm);
		const onload = frm.doc.__onload || {};
		// Refer, Complete Consultation and Start Consultation are server-only (api_only) and never in
		// available_actions; they run from the consultation / referral code.
		const actions = onload.available_actions || [];
		if (onload.can_start_consultation) {
			// Starting a consultation creates (or reopens) the visit's Patient Encounter (B1).
			frm.add_custom_button(__("Start Consultation"), () =>
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
			).addClass("btn-primary");
		}
		actions.forEach((a) => {
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

		if (onload.can_record_screening) {
			frm.add_custom_button(__("Record Screening"), () =>
				frappe.new_doc("Screening Record", {
					person_type: "Patient",
					patient: frm.doc.patient,
					visit: frm.doc.name,
					facility: frm.doc.facility,
				})
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
			const screening = onerc_healthcare.visit.screening_text(onload.latest_screening);
			if (screening) parts.push(screening);
			const flagged =
				onload.latest_screening && onload.latest_screening.outcome === "Outside Expected Range";
			frm.set_intro(parts.join(" "), flagged ? "orange" : "blue");
		} else {
			const screening = onerc_healthcare.visit.screening_text(onload.latest_screening);
			if (screening) frm.set_intro(screening, "blue");
		}
	},
});
