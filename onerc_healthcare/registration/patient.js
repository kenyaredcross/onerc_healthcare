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
		if (frm.is_new() && !frm.doc.krcs_registration_facility) {
			frappe
				.xcall(`${krcs.registration.API}.get_registration_defaults`)
				.then((r) => {
					if (r && r.registration_facility && !frm.doc.krcs_registration_facility) {
						frm.set_value("krcs_registration_facility", r.registration_facility);
					}
				})
				.catch(() => {});
		}
	},

	refresh(frm) {
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
			})
			.then((matches) => {
				if (matches && matches.length) {
					frappe.validated = false;
					krcs.registration.show_matches(frm, matches);
				}
			});
	},
});

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
			<td>${esc(m.sex || "")}<br>${m.dob ? frappe.datetime.str_to_user(m.dob) : ""}</td>
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
