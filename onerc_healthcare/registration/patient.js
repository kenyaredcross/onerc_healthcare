// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// KRCS EMR Patient form (A1): identifiers, duplicate check, walk-in, Link Identifier, merge.
// FR-1 to FR-4, SDD 4.3. Loaded via hooks.py doctype_js {"Patient": "registration/patient.js"}.

frappe.provide("krcs.registration");

krcs.registration.API = "onerc_healthcare.registration.api";
krcs.registration.MERGE_ROLES = ["Medical In-Charge", "HIS Officer", "System Manager"];
krcs.registration.FACILITY_TYPE = "Facility-generated";

krcs.registration.can_merge = function () {
	return (
		frappe.session.user === "Administrator" ||
		krcs.registration.MERGE_ROLES.some((role) => frappe.user.has_role(role))
	);
};

frappe.ui.form.on("Patient", {
	setup(frm) {
		frm.set_query("krcs_registration_facility", () => ({
			filters: { is_group: 1, krcs_facility_code: ["is", "set"] },
		}));
		// Facility-generated identifiers are assigned by the system only (walk-in), never typed.
		frm.set_query("identifier_type", "krcs_identifiers", () => ({
			filters: { enabled: 1, name: ["!=", krcs.registration.FACILITY_TYPE] },
		}));
	},

	onload(frm) {
		if (!frm.is_new()) return;
		// Decide before any async work: values already filled (link, route options) skip the search.
		const prefilled = krcs.registration.is_prefilled(frm);
		frappe
			.xcall(`${krcs.registration.API}.get_registration_defaults`)
			.then((r) => {
				if (!r) return;
				if (r.registration_facility && !frm.doc.krcs_registration_facility) {
					frm.set_value("krcs_registration_facility", r.registration_facility);
				}
				if (r.search_first_on_new_patient && !prefilled && !frm.__krcs_search_shown) {
					frm.__krcs_search_shown = true;
					krcs.registration.search_first_dialog(frm, r.default_identifier_type);
				}
			})
			.catch(() => {});
	},

	refresh(frm) {
		krcs.registration.show_dob_estimated(frm);
		if (frm.doc.krcs_merged_into) {
			frm.dashboard.set_headline_alert(
				__("This record was merged into {0} and is kept read-only (NFR-9).", [
					`<a href="/app/patient/${encodeURIComponent(frm.doc.krcs_merged_into)}">${frappe.utils.escape_html(
						frm.doc.krcs_merged_into
					)}</a>`,
				]),
				"orange"
			);
			frm.disable_form();
			return;
		}
		if (frm.is_new()) return;

		if (frm.perm[0] && frm.perm[0].write) {
			frm.add_custom_button(
				__("Link Identifier"),
				() => krcs.registration.link_identifier_dialog(frm),
				__("Identity")
			);
		}
		if (krcs.registration.can_merge()) {
			frm.add_custom_button(
				__("Merge into Another Patient"),
				() => krcs.registration.merge_dialog(frm),
				__("Identity")
			);
		}
	},

	validate(frm) {
		// Possible-duplicate check before a new patient is first saved (FR-2, SDD 4.3).
		if (!frm.is_new() || frm.doc.krcs_duplicate_override_reason || !frm.doc.first_name) return;
		return frappe
			.xcall(`${krcs.registration.API}.find_similar_patients`, {
				first_name: frm.doc.first_name,
				last_name: frm.doc.last_name || null,
				sex: frm.doc.sex || null,
				dob: frm.doc.dob || null,
				residence: frm.doc.krcs_residence || null,
				dob_estimated: frm.doc.krcs_dob_estimated ? 1 : 0,
			})
			.then((matches) => {
				if (matches && matches.length) {
					frappe.validated = false;
					krcs.registration.show_matches(frm, matches);
				}
			});
	},
});

// ---------------------------------------------------------------------------
// Phase 3 (R3): exact date of birth or an estimated age (registration/dob.py). The server recomputes
// the estimate; the form shows it at once. Typing an age ticks "DOB Estimated" and fills the DOB;
// typing a DOB yourself clears the age.
// ---------------------------------------------------------------------------

frappe.ui.form.on("Patient", {
	krcs_estimated_age_years(frm) {
		krcs.registration.apply_estimated_age(frm);
	},
	krcs_estimated_age_months(frm) {
		krcs.registration.apply_estimated_age(frm);
	},
	dob(frm) {
		if (frm.__krcs_setting_dob) return;
		if (frm.doc.krcs_dob_estimated && frm.doc.dob) {
			// An exact date of birth replaces the estimate.
			frm.__krcs_setting_age = true;
			Promise.all([
				frm.set_value("krcs_dob_estimated", 0),
				frm.set_value("krcs_estimated_age_years", 0),
				frm.set_value("krcs_estimated_age_months", 0),
			]).finally(() => {
				frm.__krcs_setting_age = false;
				krcs.registration.show_dob_estimated(frm);
			});
		}
	},
});

// Same convention as the server: estimate date minus (years * 12 + months) months, clamped at month end.
krcs.registration.estimate_dob = function (estimated_on, years, months) {
	const total = (parseInt(years, 10) || 0) * 12 + (parseInt(months, 10) || 0);
	return moment(estimated_on || frappe.datetime.get_today())
		.subtract(total, "months")
		.format("YYYY-MM-DD");
};

krcs.registration.apply_estimated_age = function (frm) {
	if (frm.__krcs_setting_age) return;
	const years = parseInt(frm.doc.krcs_estimated_age_years, 10) || 0;
	const months = parseInt(frm.doc.krcs_estimated_age_months, 10) || 0;
	frm.__krcs_setting_dob = true;
	let done;
	if (years || months) {
		const on = frm.doc.krcs_dob_estimated ? frm.doc.krcs_dob_estimated_on : null;
		done = Promise.all([
			frm.set_value("krcs_dob_estimated", 1),
			frm.set_value("dob", krcs.registration.estimate_dob(on, years, months)),
		]);
	} else if (frm.doc.krcs_dob_estimated) {
		// Age removed: no estimate left, so the date of birth must be entered.
		done = Promise.all([frm.set_value("krcs_dob_estimated", 0), frm.set_value("dob", "")]);
	} else {
		done = Promise.resolve();
	}
	done.finally(() => {
		frm.__krcs_setting_dob = false;
		krcs.registration.show_dob_estimated(frm);
	});
};

krcs.registration.show_dob_estimated = function (frm) {
	const estimated = !!frm.doc.krcs_dob_estimated;
	frm.set_df_property(
		"dob",
		"label",
		estimated ? __("Date of birth (estimated)") : __("Date of birth")
	);
	frm.set_df_property(
		"dob",
		"description",
		estimated
			? __("Calculated from the estimated age given on {0}. Enter the exact date if it becomes known.", [
					frappe.datetime.str_to_user(frm.doc.krcs_dob_estimated_on || frappe.datetime.get_today()),
			  ])
			: __("If the date of birth is not known, enter the estimated age instead.")
	);
};

krcs.registration.show_matches = function (frm, matches) {
	const esc = frappe.utils.escape_html;
	const rows = matches
		.map(
			(m) => `
		<tr>
			<td>${
				m.image
					? `<img src="${esc(m.image)}" style="width:40px;height:40px;object-fit:cover;border-radius:4px">`
					: ""
			}</td>
			<td><b>${esc(m.patient_name || "")}</b><br><span class="text-muted">${esc(m.name)}</span></td>
			<td>${esc(m.sex || "")}<br>${m.dob ? frappe.datetime.str_to_user(m.dob) : ""}${
				m.dob_estimated ? ` <span class="text-muted">${__("(estimated)")}</span>` : ""
			}</td>
			<td>${(m.identifiers || []).map(esc).join("<br>")}${
				m.residence ? `<br><span class="text-muted">${esc(m.residence)}</span>` : ""
			}</td>
			<td class="text-muted small">${(m.reasons || []).map(esc).join(", ")}</td>
			<td><button class="btn btn-xs btn-default krcs-open-match" data-name="${esc(
				m.name
			)}">${__("Open")}</button></td>
		</tr>`
		)
		.join("");

	const dialog = new frappe.ui.Dialog({
		title: __("Possible existing records"),
		size: "extra-large",
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "matches",
				options: `
					<p>${__(
						"These patients look similar. Open the existing record if it is the same person. Register a new patient only if you are sure, and record why."
					)}</p>
					<table class="table table-bordered table-sm">
						<thead><tr><th></th><th>${__("Patient")}</th><th>${__("Sex / DOB")}</th>
						<th>${__("Identifiers / Residence")}</th><th>${__("Why it matched")}</th><th></th></tr></thead>
						<tbody>${rows}</tbody>
					</table>`,
			},
			{
				fieldtype: "Small Text",
				fieldname: "reason",
				label: __("Reason for registering a new patient"),
				reqd: 1,
			},
		],
		primary_action_label: __("Register as New Patient"),
		primary_action(values) {
			dialog.hide();
			frm.set_value("krcs_duplicate_override_reason", values.reason).then(() => frm.save());
		},
	});
	dialog.$wrapper.on("click", ".krcs-open-match", function () {
		const name = $(this).attr("data-name");
		dialog.hide();
		frappe.set_route("Form", "Patient", name);
	});
	dialog.show();
};

krcs.registration.link_identifier_dialog = function (frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Link Identifier"),
		fields: [
			{
				fieldtype: "Link",
				fieldname: "identifier_type",
				label: __("Identifier Type"),
				options: "Patient Identifier Type",
				reqd: 1,
				get_query: () => ({
					filters: { enabled: 1, name: ["!=", krcs.registration.FACILITY_TYPE] },
				}),
			},
			{ fieldtype: "Data", fieldname: "identifier_value", label: __("Identifier Value"), reqd: 1 },
			{
				fieldtype: "Select",
				fieldname: "verification_status",
				label: __("Verification Status"),
				options: "Unverified\nVerified\nRejected",
				default: "Unverified",
			},
			{
				fieldtype: "Data",
				fieldname: "verification_source",
				label: __("Verification Source"),
				mandatory_depends_on: "eval:doc.verification_status=='Verified'",
			},
		],
		primary_action_label: __("Link"),
		primary_action(values) {
			frappe
				.xcall(`${krcs.registration.API}.link_identifier`, {
					patient: frm.doc.name,
					...values,
				})
				.then((r) => {
					dialog.hide();
					if (r.status === "linked") {
						frappe.show_alert({ message: __("Identifier linked"), indicator: "green" });
						frm.reload_doc();
					} else if (r.status === "exists") {
						frappe.show_alert({ message: __("This patient already holds that identifier") });
					} else if (r.status === "conflict") {
						krcs.registration.show_conflict(frm, r);
					}
				});
		},
	});
	dialog.show();
};

krcs.registration.show_conflict = function (frm, r) {
	const dialog = new frappe.ui.Dialog({
		title: __("Identifier belongs to another patient"),
		fields: [{ fieldtype: "HTML", fieldname: "message", options: `<p>${r.message}</p>` }],
		primary_action_label: __("Open {0}", [r.merge_target]),
		primary_action() {
			dialog.hide();
			frappe.set_route("Form", "Patient", r.merge_target);
		},
	});
	if (r.can_merge) {
		dialog.set_secondary_action_label(__("Merge this record into {0}", [r.merge_target]));
		dialog.set_secondary_action(() => {
			dialog.hide();
			krcs.registration.merge_dialog(frm, r.merge_target);
		});
	}
	dialog.show();
};

krcs.registration.merge_dialog = function (frm, target) {
	const dialog = new frappe.ui.Dialog({
		title: __("Merge {0} into another patient", [frm.doc.name]),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "help",
				options: `<p class="text-muted">${__(
					"All records of this patient move to the surviving patient. This record is kept, disabled and marked as merged. Nothing is deleted."
				)}</p>`,
			},
			{
				fieldtype: "Link",
				fieldname: "target",
				label: __("Surviving Patient"),
				options: "Patient",
				reqd: 1,
				default: target,
				get_query: () => ({
					filters: {
						name: ["!=", frm.doc.name],
						status: "Active",
						krcs_merged_into: ["is", "not set"],
					},
				}),
			},
			{ fieldtype: "Small Text", fieldname: "reason", label: __("Reason"), reqd: 1 },
		],
		primary_action_label: __("Merge"),
		primary_action(values) {
			frappe.confirm(
				__("Merge {0} into {1}? This cannot be undone from the form.", [
					frm.doc.name,
					values.target,
				]),
				() => {
					frappe
						.xcall(`${krcs.registration.API}.merge_patients`, {
							source: frm.doc.name,
							target: values.target,
							reason: values.reason,
						})
						.then(() => {
							dialog.hide();
							frappe.show_alert({ message: __("Patients merged"), indicator: "green" });
							frappe.set_route("Form", "Patient", values.target);
						});
				}
			);
		},
	});
	dialog.show();
};

// ---------------------------------------------------------------------------
// REG-24 (SDD 4.3, 5.1; FR-1, FR-2): registration starts with a search by identifier, then the
// duplicate check (similarity dialog on save), then capture.
// ---------------------------------------------------------------------------

krcs.registration.is_prefilled = function (frm) {
	const d = frm.doc;
	return Boolean(
		d.first_name ||
			d.last_name ||
			d.patient_name ||
			d.krcs_is_walk_in ||
			(d.krcs_identifiers || []).some((r) => r.identifier_value)
	);
};

krcs.registration.search_first_dialog = function (frm, default_type) {
	const dialog = new frappe.ui.Dialog({
		title: __("Find patient by identifier"),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "intro",
				options: `<p class="text-muted">${__(
					"Search for the patient first so the same person is not registered twice. Close this window to skip."
				)}</p>`,
			},
			{
				fieldtype: "Link",
				fieldname: "identifier_type",
				label: __("Identifier Type"),
				options: "Patient Identifier Type",
				reqd: 1,
				default: default_type || "UNHCR ID",
				get_query: () => ({
					filters: { enabled: 1, name: ["!=", krcs.registration.FACILITY_TYPE] },
				}),
			},
			{ fieldtype: "Data", fieldname: "identifier_value", label: __("Identifier Value"), reqd: 1 },
			{ fieldtype: "HTML", fieldname: "results" },
		],
		primary_action_label: __("Search"),
		primary_action(values) {
			frappe
				.xcall(`${krcs.registration.API}.find_patient_by_identifier`, {
					identifier_value: values.identifier_value,
					identifier_type: values.identifier_type,
				})
				.then((found) => {
					if (found && found.length) {
						krcs.registration.show_search_results(frm, dialog, found);
					} else {
						dialog.hide();
						krcs.registration.prefill_identifier(
							frm,
							values.identifier_type,
							values.identifier_value
						);
					}
				});
		},
		secondary_action_label: __("New walk-in (no identifier)"),
		secondary_action() {
			dialog.hide();
			frm.set_value("krcs_is_walk_in", 1).then(() => krcs.registration.focus_first_name(frm));
		},
	});
	dialog.show();
	setTimeout(() => dialog.get_field("identifier_value").$input.trigger("focus"), 300);
};

krcs.registration.show_search_results = function (frm, dialog, found) {
	const esc = frappe.utils.escape_html;
	// One line per surviving record (a merged holder resolves to the record it was merged into).
	const seen = new Set();
	const rows = found
		.filter((f) => !seen.has(f.surviving_patient) && seen.add(f.surviving_patient))
		.map(
			(f) => `
		<tr>
			<td><b>${esc(f.surviving_patient_name || "")}</b><br>
				<span class="text-muted">${esc(f.surviving_patient)}</span>${
				f.surviving_status === "Disabled"
					? ` <span class="indicator-pill red">${__("Disabled")}</span>`
					: ""
			}</td>
			<td>${esc(f.surviving_sex || "")}<br>${
				f.surviving_dob ? frappe.datetime.str_to_user(f.surviving_dob) : ""
			}</td>
			<td>${esc(f.identifier_type)}: ${esc(f.identifier_value)}${
				f.merged_into
					? `<br><span class="text-muted small">${__("held by merged record {0}", [
							esc(f.patient),
					  ])}</span>`
					: ""
			}</td>
			<td><button class="btn btn-xs btn-primary krcs-open-found" data-name="${esc(
				f.surviving_patient
			)}">${__("Open record")}</button></td>
		</tr>`
		)
		.join("");
	dialog.get_field("results").$wrapper.html(`
		<p><b>${__("Already registered:")}</b></p>
		<table class="table table-bordered table-sm">
			<thead><tr><th>${__("Patient")}</th><th>${__("Sex / DOB")}</th><th>${__("Identifier")}</th><th></th></tr></thead>
			<tbody>${rows}</tbody>
		</table>`);
	dialog.$wrapper.off("click.krcs").on("click.krcs", ".krcs-open-found", function () {
		const name = $(this).attr("data-name");
		const unsaved = frm.docname;
		dialog.hide();
		// Discard the unsaved new form, then open the existing record.
		frappe.set_route("Form", "Patient", name).then(() => {
			if (locals["Patient"] && locals["Patient"][unsaved] && locals["Patient"][unsaved].__islocal) {
				frappe.model.clear_doc("Patient", unsaved);
			}
		});
	});
};

krcs.registration.prefill_identifier = function (frm, identifier_type, identifier_value) {
	frm.clear_table("krcs_identifiers");
	frm.add_child("krcs_identifiers", { identifier_type, identifier_value });
	frm.refresh_field("krcs_identifiers");
	frappe.show_alert({
		message: __("No patient holds {0} {1}. Register the patient below.", [
			frappe.utils.escape_html(identifier_type),
			frappe.utils.escape_html(identifier_value),
		]),
		indicator: "blue",
	});
	krcs.registration.focus_first_name(frm);
};

krcs.registration.focus_first_name = function (frm) {
	frm.scroll_to_field("first_name");
	const field = frm.get_field("first_name");
	if (field && field.$input) setTimeout(() => field.$input.trigger("focus"), 100);
};
