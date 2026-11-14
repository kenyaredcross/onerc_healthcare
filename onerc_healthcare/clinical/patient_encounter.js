// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Patient Encounter form (B1): the KRCS consultation flow (FR-10 to FR-12, SDD §5.3).
// Server rules live in onerc_healthcare/clinical/*.py; this file only offers the actions.

const KRCS_CONSULT = "onerc_healthcare.clinical.consultation.";
const KRCS_HISTORY = "onerc_healthcare.clinical.history.";
// Marley buttons replaced by the EMR flow (orders, referral, inpatient: out of scope).
const KRCS_HIDDEN_BUTTONS = [
	["Refer Patient", "Create"],
	["Service Request", "Create"],
	["Medication Request", "Create"],
	["Clinical Procedure", "Create"],
	["Inpatient Medication Order", "Create"],
	["Schedule Admission", null],
];

frappe.ui.form.on("Patient Encounter", {
	setup(frm) {
		frm.set_query("code", "krcs_diagnoses", () => ({
			query: "onerc_healthcare.clinical.diagnosis.search_icd11",
		}));
		krcs_hide_note_delete(frm);
	},

	refresh(frm) {
		krcs_hide_marley_actions(frm);
		krcs_set_drug_query(frm);
		if (frm.is_new()) return;
		const draft = frm.doc.docstatus === 0;
		const can_consult = frappe.user.has_role(["Clinical Officer", "Mental Health Officer"]);

		if (draft && can_consult) {
			if (krcs_unsent_labs(frm).length) {
				frm.add_custom_button(__("Send Lab Orders"), () => krcs_send_labs(frm));
			}
			frm.add_custom_button(__("Refer"), () => krcs_refer(frm));
			if (!frm.is_dirty()) {
				frm.page.set_primary_action(__("Complete Consultation"), () => krcs_complete(frm));
			}
		}
		if (frm.doc.docstatus === 1 && can_consult) {
			frm.add_custom_button(__("Add Addendum"), () => krcs_addendum(frm));
		}
		if (frm.doc.krcs_visit) {
			frm.add_custom_button(
				__("Patient Visit"),
				() => frappe.set_route("Form", "Patient Visit", frm.doc.krcs_visit),
				__("View"),
			);
		}
		krcs_render_lab_results(frm);
		krcs_render_history(frm);
		krcs_listen_for_results(frm);
	},

	onload_post_render(frm) {
		krcs_hide_marley_actions(frm);
	},
});

// ------------------------------------------------------------------ queries

// Marley's drug_code query filters is_stock_item=1 and adds an "Actual Qty" column; the KRCS catalogue is
// non-stock (C-5, PHA-02). Runs on refresh, after Marley's own refresh sets its query.
function krcs_set_drug_query(frm) {
	frm.set_query("drug_code", "drug_prescription", (doc, cdt, cdn) => {
		const row = locals[cdt][cdn] || {};
		return {
			query: "onerc_healthcare.pharmacy.catalogue.dispensable_item_query",
			filters: row.medication ? { medication: row.medication } : {},
		};
	});
}

// ------------------------------------------------------------------ hide Marley / desk actions

function krcs_hide_marley_actions(frm) {
	KRCS_HIDDEN_BUTTONS.forEach(([label, group]) =>
		group ? frm.remove_custom_button(__(label), __(group)) : frm.remove_custom_button(__(label)),
	);
	// No desk Submit / Cancel / Amend: completion goes through Complete Consultation, and Phase 2 has no
	// cancel or amend (corrections are addenda). The server refuses them anyway.
	if (frm.doc.docstatus === 0 && !frm.is_new() && !frm.is_dirty()) {
		frm.page.clear_primary_action();
	}
	if (frm.doc.docstatus > 0) {
		frm.page.clear_primary_action();
		frm.page.clear_secondary_action();
	}
}

function krcs_hide_note_delete(frm) {
	if (frm.wrapper && !$(frm.wrapper).find("style.krcs-encounter-style").length) {
		$(frm.wrapper).append(
			`<style class="krcs-encounter-style">
				.clinical-notes-section .delete-note-btn,
				.orders .new-service-request-btn,
				.orders .new-medication-request-btn { display: none !important; }
			</style>`,
		);
	}
}

// ------------------------------------------------------------------ actions

function krcs_unsent_labs(frm) {
	return (frm.doc.lab_test_prescription || []).filter(
		(r) => (r.observation_template || r.lab_test_code) && !r.service_request,
	);
}

async function krcs_save_first(frm) {
	if (frm.is_dirty()) {
		await frm.save();
	}
}

async function krcs_send_labs(frm) {
	await krcs_save_first(frm);
	const tests = krcs_unsent_labs(frm)
		.map((r) => frappe.utils.escape_html(r.observation_template || r.lab_test_code))
		.join(", ");
	frappe.confirm(__("Send these tests to the laboratory: {0}?", [tests]), async () => {
		const r = await frappe.call({
			method: KRCS_CONSULT + "send_lab_orders",
			type: "POST",
			args: { encounter: frm.doc.name },
			freeze: true,
			freeze_message: __("Sending laboratory orders"),
		});
		frappe.show_alert({
			message: __("{0} laboratory order(s) sent", [(r.message.service_requests || []).length]),
			indicator: "green",
		});
		frm.reload_doc();
	});
}

async function krcs_complete(frm) {
	await krcs_save_first(frm);
	const lab_open = frm.doc.krcs_visit
		? (await frappe.db.get_value("Patient Visit", frm.doc.krcs_visit, "lab_open")).message.lab_open
		: 0;
	const no_dx = !(frm.doc.krcs_diagnoses || []).length && !frm.doc.krcs_no_diagnosis_reason;
	const unsent = krcs_unsent_labs(frm);
	if (unsent.length) {
		frappe.msgprint(__("Send or remove the unsent laboratory tests before completing the consultation."));
		return;
	}
	const fields = [];
	if (no_dx) {
		fields.push({
			fieldname: "no_diagnosis_reason",
			fieldtype: "Small Text",
			label: __("Reason for No Diagnosis"),
			reqd: 1,
			description: __("No ICD-11 diagnosis is recorded. Add one, or say why there is none."),
		});
	}
	if (lab_open) {
		fields.push({
			fieldname: "reason",
			fieldtype: "Small Text",
			label: __("Reason to Complete Before Results"),
			reqd: 1,
			description: __("Laboratory results are still pending. You will still be notified when they are ready."),
		});
	}
	const run = (values) =>
		frappe
			.call({
				method: KRCS_CONSULT + "complete_consultation",
				type: "POST",
				args: Object.assign({ encounter: frm.doc.name }, values || {}),
				freeze: true,
				freeze_message: __("Completing consultation"),
			})
			.then((r) => {
				frappe.show_alert({
					message: __("Consultation completed. Visit: {0}", [__(r.message.visit_status)]),
					indicator: "green",
				});
				frm.reload_doc();
			});
	if (!fields.length) {
		frappe.confirm(
			__("Complete the consultation? Prescriptions are sent to the pharmacy and the note is locked."),
			() => run(),
		);
		return;
	}
	const d = new frappe.ui.Dialog({
		title: __("Complete Consultation"),
		fields,
		primary_action_label: __("Complete"),
		primary_action(values) {
			d.hide();
			run(values);
		},
	});
	d.show();
}

async function krcs_refer(frm) {
	await krcs_save_first(frm);
	const non_routable = await frappe.xcall("onerc_healthcare.visit.engine.get_non_routable_departments");
	const d = new frappe.ui.Dialog({
		title: __("Refer Patient"),
		size: "large",
		fields: [
			{
				fieldname: "referral_type",
				fieldtype: "Select",
				label: __("Referral Type"),
				options: "Internal\nExternal",
				default: "Internal",
				reqd: 1,
			},
			{
				fieldname: "to_department",
				fieldtype: "Link",
				options: "Medical Department",
				label: __("To Department"),
				depends_on: "eval:doc.referral_type=='Internal'",
				mandatory_depends_on: "eval:doc.referral_type=='Internal'",
				get_query: () => ({ filters: { name: ["not in", non_routable] } }),
			},
			{
				fieldname: "to_facility",
				fieldtype: "Link",
				options: "Referral Facility",
				label: __("To Facility"),
				depends_on: "eval:doc.referral_type=='External'",
				get_query: () => ({ filters: { is_active: 1 } }),
			},
			{
				fieldname: "to_facility_name",
				fieldtype: "Data",
				label: __("Other Facility"),
				depends_on: "eval:doc.referral_type=='External'",
				description: __("When the facility is not in the list"),
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "urgency",
				fieldtype: "Select",
				label: __("Urgency"),
				options: "Routine\nUrgent\nEmergency",
				default: "Routine",
				reqd: 1,
			},
			{
				fieldname: "send",
				fieldtype: "Check",
				label: __("Send now (completes this consultation and closes the visit)"),
				default: 1,
			},
			{ fieldtype: "Section Break" },
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason for Referral"), reqd: 1 },
			{
				fieldname: "clinical_summary",
				fieldtype: "Text",
				label: __("Clinical Summary"),
				default: frm.doc.krcs_management_plan || "",
			},
		],
		primary_action_label: __("Create Referral"),
		primary_action(values) {
			d.hide();
			frappe
				.call({
					method: "onerc_healthcare.clinical.referral.create_referral",
					type: "POST",
					args: Object.assign({ encounter: frm.doc.name }, values),
					freeze: true,
				})
				.then((r) => {
					frappe.show_alert({
						message: __("Referral {0}: {1}", [r.message.name, __(r.message.status)]),
						indicator: "green",
					});
					frappe.set_route("Form", "Referral", r.message.name);
				});
		},
	});
	d.show();
}

function krcs_addendum(frm) {
	frappe.prompt(
		{ fieldname: "text", fieldtype: "Text", label: __("Addendum"), reqd: 1 },
		(v) =>
			frappe
				.call({
					method: KRCS_CONSULT + "add_addendum",
					type: "POST",
					args: { encounter: frm.doc.name, text: v.text },
					freeze: true,
				})
				.then(() => frm.reload_doc()),
		__("Add Addendum"),
		__("Add"),
	);
}

function krcs_cancel_order(frm, service_request) {
	frappe.prompt(
		{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
		(v) =>
			frappe
				.call({
					method: KRCS_CONSULT + "cancel_lab_order",
					type: "POST",
					args: { service_request, reason: v.reason },
					freeze: true,
				})
				.then(() => frm.reload_doc()),
		__("Cancel Laboratory Order {0}", [service_request]),
		__("Cancel Order"),
	);
}

// ------------------------------------------------------------------ panels

const esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v));

function krcs_listen_for_results(frm) {
	if (frm.krcs_listening) return;
	frm.krcs_listening = true;
	// B2 publishes this when an encounter's results are approved (FR-17).
	frappe.realtime.on("krcs_lab_results_ready", (data) => {
		// payload {encounter, subject, event: "ready" | "rejected", service_request, visit}
		if (data && data.encounter === frm.doc.name && cur_frm === frm) {
			krcs_render_lab_results(frm);
		}
	});
}

async function krcs_render_lab_results(frm) {
	const field = frm.fields_dict.krcs_lab_results_html;
	if (!field) return;
	const $w = $(field.wrapper).empty();
	if (!(frm.doc.lab_test_prescription || []).length) {
		$w.html(`<div class="text-muted small">${__("No laboratory tests ordered.")}</div>`);
		return;
	}
	let data;
	try {
		data = (
			await frappe.call({
				method: KRCS_HISTORY + "get_encounter_lab_results",
				type: "GET",
				args: { encounter: frm.doc.name },
			})
		).message;
	} catch (e) {
		$w.html(`<div class="text-muted small">${__("Laboratory results could not be loaded.")}</div>`);
		return;
	}
	const can_cancel =
		frm.doc.docstatus === 0 && frappe.user.has_role(["Clinical Officer", "Mental Health Officer"]);
	const active = new Set((data.orders || []).filter((o) => o.docstatus === 1).map((o) => o.name));
	$w.html(krcs_results_html(data.available, data.results, data.orders, can_cancel ? active : null));
	$w.find(".krcs-cancel-order").on("click", (e) => krcs_cancel_order(frm, $(e.currentTarget).data("sr")));
}

// `results`: lab.api.get_encounter_results (B2): one entry per order with nested approved result rows.
// `orders`: fallback list of the encounter's Service Requests while the lab module is unavailable.
function krcs_results_html(available, results, orders, cancellable) {
	const cancel_btn = (sr) =>
		cancellable && cancellable.has(sr)
			? ` <button class="btn btn-xs btn-default krcs-cancel-order" data-sr="${esc(sr)}">${__("Cancel")}</button>`
			: "";
	if (!available || !Array.isArray(results)) {
		const rows = (orders || [])
			.map(
				(o) => `<tr><td>${esc(o.test)}</td><td>${esc(o.name)}</td>
				<td>${o.docstatus === 2 ? __("Cancelled") : __("Sent")}${cancel_btn(o.name)}</td></tr>`,
			)
			.join("");
		return rows
			? `<table class="table table-sm table-bordered small"><tbody>${rows}</tbody></table>`
			: `<div class="text-muted small">${__("No laboratory orders sent yet.")}</div>`;
	}
	if (!results.length) {
		return `<div class="text-muted small">${__("No laboratory orders sent yet.")}</div>`;
	}
	return results
		.map((t) => {
			const rows = (t.results || [])
				.map((r) => {
					const color = r.flag_color ? ` style="color:${esc(r.flag_color)}"` : "";
					return `<tr>
						<td>${esc(r.test)}${r.panel ? ` <span class="text-muted">(${esc(r.panel)})</span>` : ""}</td>
						<td><b${color}>${r.result == null ? `<span class="text-muted">${esc(t.status || __("Pending"))}</span>` : esc(r.result)}</b></td>
						<td>${esc(r.unit)}</td><td>${esc(r.reference)}</td>
						<td${color}>${esc(r.flag)}</td><td>${esc(r.interpretation)}</td>
					</tr>`;
				})
				.join("");
			const head = `<div class="mb-1"><b>${esc(t.test)}</b> <span class="text-muted">${esc(t.service_request)}</span>
				— ${esc(t.status || t.sample_status)}${t.approved ? ` <span class="indicator-pill green">${__("Approved")}</span>` : ""}
				${cancel_btn(t.service_request)}</div>`;
			const table = rows
				? `<table class="table table-sm table-bordered small"><thead><tr><th>${__("Test")}</th><th>${__("Result")}</th>
					<th>${__("Unit")}</th><th>${__("Reference")}</th><th>${__("Flag")}</th><th>${__("Interpretation")}</th>
					</tr></thead><tbody>${rows}</tbody></table>`
				: "";
			return `<div class="mb-3">${head}${table}</div>`;
		})
		.join("");
}

async function krcs_render_history(frm) {
	const field = frm.fields_dict.krcs_history_html;
	if (!field || !frm.doc.patient) return;
	const $w = $(field.wrapper).empty();
	let data;
	try {
		data = (
			await frappe.call({
				method: KRCS_HISTORY + "get_patient_history",
				type: "GET",
				args: { patient: frm.doc.patient, exclude: frm.doc.name },
			})
		).message;
	} catch (e) {
		return;
	}
	const encounters = data.encounters || [];
	if (!encounters.length) {
		$w.html(`<div class="text-muted">${__("No previous consultations.")}</div>`);
		return;
	}
	const html = encounters
		.map((e) => {
			const dx = (e.diagnoses || [])
				.map(
					(d) =>
						`<li>${d.is_primary ? "<b>" : ""}${esc(d.code_value)} ${esc(d.title)}${d.is_primary ? "</b>" : ""}
						<span class="text-muted">(${esc(__(d.case_type))})</span></li>`,
				)
				.join("");
			const rx = (e.prescriptions || [])
				.map(
					(p) =>
						`<li>${esc(p.medication || p.drug_name || p.drug_code)} ${esc(p.dosage)} ${esc(p.period)}
						<span class="text-muted">— ${esc(__(p.dispense_status))}</span></li>`,
				)
				.join("");
			const refs = (e.referrals || [])
				.map(
					(r) =>
						`<li><a href="/app/referral/${encodeURIComponent(r.name)}">${esc(r.name)}</a>
						${esc(r.to_department || r.to_facility || r.to_facility_name)} — ${esc(__(r.status))}</li>`,
				)
				.join("");
			return `<div class="krcs-history-item mb-3 p-3 border rounded">
				<div><a href="/app/patient-encounter/${encodeURIComponent(e.name)}"><b>${esc(
					frappe.datetime.str_to_user(e.encounter_date),
				)}</b></a> · ${esc(e.department)} · ${esc(e.practitioner_name)}</div>
				${e.presenting_complaint ? `<div class="small">${__("Complaint")}: ${esc(e.presenting_complaint)}</div>` : ""}
				${dx ? `<div class="small mt-1">${__("Diagnoses")}<ul>${dx}</ul></div>` : e.no_diagnosis_reason ? `<div class="small">${__("No diagnosis")}: ${esc(e.no_diagnosis_reason)}</div>` : ""}
				${e.management_plan ? `<div class="small">${__("Plan")}: ${esc(e.management_plan)}</div>` : ""}
				${data.lab_results_available && (e.lab_results || []).length ? `<div class="small mt-1">${__("Results")}${krcs_results_html(true, e.lab_results)}</div>` : ""}
				${rx ? `<div class="small mt-1">${__("Prescriptions")}<ul>${rx}</ul></div>` : ""}
				${refs ? `<div class="small mt-1">${__("Referrals")}<ul>${refs}</ul></div>` : ""}
				${e.addendum ? `<div class="small mt-1">${__("Addendum")}: <pre class="small">${esc(e.addendum)}</pre></div>` : ""}
			</div>`;
		})
		.join("");
	$w.html(html);
}
