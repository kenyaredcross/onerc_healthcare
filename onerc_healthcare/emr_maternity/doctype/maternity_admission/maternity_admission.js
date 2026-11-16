// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Maternity Admission (Phase 5M M1, SDD §5.12.3). Server rules: onerc_healthcare/maternity/admission.py.
// A new form is filled and sent with "Admit" (the server opens / closes the visit); status changes only through
// the action buttons; a Discharged admission is read-only (the Medical In-Charge may archive it).

const KRCS_MAT = "onerc_healthcare.maternity.admission.";
const KRCS_MAT_INPUT_FIELDS = [
	"husband_name", "employer", "safe_motherhood_no", "gravida", "para", "anc_clinic", "lmp", "edd_basis", "edd",
	"medical_history", "family_history", "transfusion_history", "blood_group", "rhesus", "syphilis_rpr",
	"blood_group_result", "rhesus_result", "syphilis_result", "fundal_height_cm", "presentation", "engagement",
	"position", "foetal_heart_rate", "urine", "haemoglobin", "bp_systolic", "bp_diastolic", "temperature", "pulse",
	"respiratory_rate", "weight_kg", "vaginal_discharge", "genitals", "spleen", "abnormalities", "remarks",
];
const KRCS_MAT_TABLES = {
	obstetric_history: [
		"year", "place", "labour_duration", "delivery_type", "birth_weight_kg", "sex", "alive_or_dead", "feeding",
		"puerperium", "pelvic_assessment",
	],
	diagnoses: ["code", "case_type", "is_primary", "note"],
};
const KRCS_MAT_COLORS = { Admitted: "blue", "In Labour": "orange", Delivered: "purple", Discharged: "green" };
const KRCS_MAT_NAEGELE_DAYS = 280;

// EDD basis LMP (the default): the EDD is always LMP + 280 days (Naegele), as the server sets it. With basis
// Ultrasound / Clinical estimate the typed EDD is kept and the gestation is counted from it.
function krcs_mat_edd_from_lmp(frm) {
	if ((frm.doc.edd_basis || "LMP") === "LMP" && frm.doc.lmp) {
		frm.set_value("edd", frappe.datetime.add_days(frm.doc.lmp, KRCS_MAT_NAEGELE_DAYS));
	}
}

frappe.ui.form.on("Maternity Admission", {
	setup(frm) {
		frm.set_query("code", "diagnoses", () => ({ query: "onerc_healthcare.clinical.diagnosis.search_icd11" }));
		frm.set_query("ward", () => ({
			filters: { is_group: 1, parent_healthcare_service_unit: frm.doc.facility || "__none__" },
		}));
		frm.set_query("bed", () => ({
			filters: {
				parent_healthcare_service_unit: frm.doc.ward || "__none__",
				is_group: 0,
				service_unit_type: "Bed",
				occupancy_status: "Vacant",
			},
		}));
		frm.set_query("facility", () => ({ filters: { is_group: 1, parent_healthcare_service_unit: ["is", "set"] } }));
		["blood_group_result", "rhesus_result", "syphilis_result"].forEach((f) =>
			frm.set_query(f, () => ({ filters: { patient: frm.doc.patient || "__none__" } })),
		);
	},

	onload(frm) {
		if (frm.is_new() && frm.doc.patient && !frm.doc.ward) krcs_mat_default_ward(frm);
	},

	refresh(frm) {
		const onload = frm.doc.__onload || {};
		if (frm.is_new()) {
			krcs_mat_new_form(frm);
			return;
		}
		frm.page.set_indicator(__(frm.doc.status), KRCS_MAT_COLORS[frm.doc.status] || "gray");
		["facility", "ward", "bed", "patient"].forEach((f) => frm.set_df_property(f, "read_only", 1));
		if (onload.gestation_label) {
			frm.dashboard.set_headline(
				__("Gestation at admission: {0}", [frappe.utils.escape_html(onload.gestation_label)]),
			);
		}
		if (frm.doc.krcs_archived) {
			frm.set_intro(__("This admission is archived (kept for the record; it no longer counts as open)."), "orange");
		}
		if (frm.doc.status === "Discharged") krcs_mat_lock_form(frm);
		krcs_mat_actions(frm, onload);
		krcs_mat_render_observations(frm, onload);
	},

	patient(frm) {
		if (frm.is_new()) krcs_mat_default_ward(frm);
	},

	facility(frm) {
		if (!frm.is_new() || !frm.doc.facility) return;
		frappe
			.xcall(KRCS_MAT + "get_admit_defaults", { facility: frm.doc.facility }, "GET")
			.then((d) => frm.set_value("ward", d.ward || ""));
	},

	ward(frm) {
		if (frm.is_new()) frm.set_value("bed", "");
	},

	lmp(frm) {
		krcs_mat_edd_from_lmp(frm);
	},

	edd_basis(frm) {
		krcs_mat_edd_from_lmp(frm);
	},

	validate(frm) {
		if (!frm.is_new()) return;
		// A new admission is created by the server (admit), never by a plain insert.
		frappe.validated = false;
		krcs_mat_admit_from_form(frm);
	},
});

function krcs_mat_default_ward(frm) {
	frappe
		.xcall(KRCS_MAT + "get_admit_defaults", { patient: frm.doc.patient || null, visit: frm.doc.visit || null }, "GET")
		.then((d) => {
			if (d.facility && !frm.doc.facility) frm.set_value("facility", d.facility);
			if (d.ward && !frm.doc.ward) frm.set_value("ward", d.ward);
		});
}

function krcs_mat_new_form(frm) {
	frm.set_intro(
		__(
			"Choose the patient (and the ward and bed), fill in what is known, then press Admit. Without a visit, " +
				"the patient's waiting visit is used, or a visit is opened and closed as Admitted.",
		),
		"blue",
	);
	frm.page.set_primary_action(__("Admit"), () => krcs_mat_admit_from_form(frm));
}

function krcs_mat_admit_from_form(frm) {
	if (!frm.doc.patient) {
		frappe.msgprint(__("Choose the patient to admit."));
		return;
	}
	const values = {};
	KRCS_MAT_INPUT_FIELDS.forEach((f) => {
		const v = frm.doc[f];
		if (v !== undefined && v !== null && v !== "" && v !== 0) values[f] = v;
	});
	Object.entries(KRCS_MAT_TABLES).forEach(([table, fields]) => {
		const rows = (frm.doc[table] || []).map((row) => {
			const out = {};
			fields.forEach((f) => {
				if (row[f] !== undefined && row[f] !== null && row[f] !== "") out[f] = row[f];
			});
			return out;
		});
		if (rows.length) values[table] = rows;
	});
	if (frm.doc.facility && !frm.doc.visit) values.facility = frm.doc.facility;
	frappe
		.call({
			method: KRCS_MAT + "admit",
			type: "POST",
			args: {
				patient: frm.doc.patient,
				visit: frm.doc.visit || null,
				ward: frm.doc.ward || null,
				bed: frm.doc.bed || null,
				values,
			},
			freeze: true,
			freeze_message: __("Admitting..."),
		})
		.then((r) => {
			const old = frm.docname;
			frappe.show_alert({ message: __("Admitted: {0}", [r.message.name]), indicator: "green" });
			frappe.set_route("Form", "Maternity Admission", r.message.name).then(() => {
				frappe.model.remove_from_locals("Maternity Admission", old);
			});
		});
}

function krcs_mat_lock_form(frm) {
	const can_archive = frappe.user.has_role("Medical In-Charge");
	frm.fields.forEach((field) => {
		const df = field.df;
		if (!df || !df.fieldname || ["krcs_archived", "krcs_archive_section"].includes(df.fieldname)) return;
		frm.set_df_property(df.fieldname, "read_only", 1);
	});
	frm.set_df_property("krcs_archived", "read_only", can_archive ? 0 : 1);
	frm.set_intro(
		__("Discharged {0}: {1}. The admission is read-only.", [
			frappe.datetime.str_to_user(frm.doc.discharged_at),
			__(frm.doc.discharge_outcome || ""),
		]),
		frm.doc.discharge_outcome === "Died" ? "red" : "green",
	);
}

function krcs_mat_actions(frm, onload) {
	(onload.allowed_actions || []).forEach((a) => {
		if (a.action === "start_labour") {
			frm.add_custom_button(__("Start Labour"), () =>
				frappe.confirm(__("Mark {0} as in labour?", [frappe.utils.escape_html(frm.doc.patient_name)]), () =>
					krcs_mat_apply(frm, "start_labour", {}),
				),
			);
		} else if (a.action === "discharge") {
			frm.add_custom_button(__("Discharge"), () => krcs_mat_discharge(frm, onload));
		}
	});
	if (onload.partograph || onload.can_open_partograph) {
		frm.add_custom_button(__("Open Partograph"), () => {
			if (onload.partograph && !onload.can_open_partograph) {
				frappe.set_route("Form", "Partograph", onload.partograph);
				return;
			}
			frappe
				.call({
					method: "onerc_healthcare.maternity.partograph.get_or_create",
					type: "POST",
					args: { admission: frm.doc.name },
					freeze: true,
				})
				.then((r) => frappe.set_route("Form", "Partograph", r.message));
		});
	}
	const deliveries = onload.deliveries || [];
	deliveries.forEach((d) =>
		frm.add_custom_button(
			d.krcs_archived
				? __("Superseded Delivery Record {0}", [d.name])
				: d.docstatus === 1
					? __("Delivery Record {0}", [d.name])
					: __("Draft Delivery Record {0}", [d.name]),
			() => frappe.set_route("Form", "Delivery Record", d.name),
			__("View"),
		),
	);
	if (onload.can_record_delivery && !deliveries.length) {
		frm.add_custom_button(__("Record Delivery"), () =>
			frappe.new_doc("Delivery Record", { admission: frm.doc.name }),
		);
	}
	if (onload.can_record_observations) {
		frm.add_custom_button(__("Record Observations"), () => krcs_mat_observation_dialog(frm));
	}
	if (frm.doc.visit) {
		frm.add_custom_button(
			__("Admitting Visit"),
			() => frappe.set_route("Form", "Patient Visit", frm.doc.visit),
			__("View"),
		);
	}
}

function krcs_mat_apply(frm, action, values) {
	return frappe
		.call({
			method: KRCS_MAT + "apply_action",
			type: "POST",
			args: { admission: frm.doc.name, action, values },
			freeze: true,
		})
		.then(() => frm.reload_doc());
}

function krcs_mat_discharge(frm, onload) {
	const outcomes = ["Discharged home", "Referred out", "Died", "Absconded"].filter(
		(o) => o !== "Died" || frappe.user.has_role(onload.died_roles || []),
	);
	const d = new frappe.ui.Dialog({
		title: __("Discharge {0}", [frappe.utils.escape_html(frm.doc.patient_name || frm.doc.patient)]),
		fields: [
			{
				fieldname: "discharge_outcome",
				fieldtype: "Select",
				label: __("Outcome"),
				options: [""].concat(outcomes).join("\n"),
				reqd: 1,
			},
			{
				fieldname: "death_reason",
				fieldtype: "Small Text",
				label: __("Cause of Death"),
				depends_on: "eval:doc.discharge_outcome=='Died'",
				mandatory_depends_on: "eval:doc.discharge_outcome=='Died'",
			},
			{ fieldname: "discharge_condition", fieldtype: "Small Text", label: __("Condition at Discharge") },
			{ fieldname: "discharge_notes", fieldtype: "Small Text", label: __("Discharge Notes") },
		],
		primary_action_label: __("Discharge"),
		primary_action(values) {
			d.hide();
			const payload = {};
			Object.entries(values).forEach(([k, v]) => {
				if (v) payload[k] = v;
			});
			krcs_mat_apply(frm, "discharge", payload);
		},
	});
	d.show();
}

function krcs_mat_observation_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Ward Observations: {0}", [frappe.utils.escape_html(frm.doc.patient_name || frm.doc.patient)]),
		fields: [
			{ fieldname: "temperature", fieldtype: "Data", label: __("Temperature (°C)") },
			{ fieldname: "pulse", fieldtype: "Data", label: __("Pulse (/min)") },
			{ fieldname: "respiratory_rate", fieldtype: "Data", label: __("Respiration (/min)") },
			{ fieldtype: "Column Break" },
			{ fieldname: "bp_systolic", fieldtype: "Data", label: __("BP Systolic (mmHg)") },
			{ fieldname: "bp_diastolic", fieldtype: "Data", label: __("BP Diastolic (mmHg)") },
			{ fieldtype: "Section Break" },
			{ fieldname: "vital_signs_note", fieldtype: "Small Text", label: __("Note") },
		],
		primary_action_label: __("Record"),
		primary_action(values) {
			d.disable_primary_action();
			frappe
				.call({
					method: KRCS_MAT + "record_observation",
					type: "POST",
					args: { admission: frm.doc.name, values },
					freeze: true,
				})
				.then(() => {
					d.hide();
					frappe.show_alert({ message: __("Observations recorded"), indicator: "green" });
					frm.reload_doc();
				})
				.always(() => d.enable_primary_action());
		},
	});
	d.show();
}

function krcs_mat_render_observations(frm, onload) {
	const rows = onload.observations;
	const esc = frappe.utils.escape_html;
	let body;
	if (rows === null || rows === undefined) {
		body = `<div class="text-muted small">${__("You cannot read vital signs.")}</div>`;
	} else if (!rows.length) {
		body = `<div class="text-muted small">${__("No ward observations yet.")}</div>`;
	} else {
		const tr = rows
			.map((r) => {
				const when = frappe.datetime.str_to_user(`${r.signs_date} ${r.signs_time || ""}`.trim());
				const bp = r.bp_systolic && r.bp_diastolic ? `${esc(r.bp_systolic)}/${esc(r.bp_diastolic)}` : "";
				const state = r.docstatus === 1 ? "" : ` <span class="indicator-pill orange">${__("Draft")}</span>`;
				return `<tr>
					<td><a href="/app/vital-signs/${encodeURIComponent(r.name)}">${esc(when)}</a>${state}</td>
					<td>${esc(r.temperature || "")}</td><td>${esc(r.pulse || "")}</td>
					<td>${esc(r.respiratory_rate || "")}</td><td>${bp}</td>
					<td>${esc(r.vital_signs_note || "")}</td></tr>`;
			})
			.join("");
		body = `<table class="table table-sm table-bordered small" style="margin:0">
			<thead><tr><th>${__("Time")}</th><th>${__("T (°C)")}</th><th>${__("Pulse")}</th>
			<th>${__("Resp.")}</th><th>${__("BP")}</th><th>${__("Note")}</th></tr></thead>
			<tbody>${tr}</tbody></table>`;
	}
	frm.dashboard.add_section(`<div class="krcs-ward-observations">${body}</div>`, __("Ward Observations"));
	frm.dashboard.show();
}
