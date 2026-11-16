// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// KRCS EMR Patient form (B4): consent status, Record Consent, prompt after the first save (FR-37).
// Loaded via hooks.py doctype_js {"Patient": ["registration/patient.js", "privacy/patient.js"]}.
// No consent never blocks care (OI-14): the prompt can be dismissed and the patient is then listed on
// the Consent Exceptions report.

frappe.provide("krcs.privacy");

krcs.privacy.CONSENT_API = "onerc_healthcare.privacy.consent";
krcs.privacy.STATEMENT_API = "onerc_healthcare.privacy.statements";
krcs.privacy.OUTCOME_COLOURS = {
	Given: "green",
	Withheld: "orange",
	"Unable to Consent": "orange",
	Withdrawn: "red",
	none: "red",
};

krcs.privacy.can_read_consent = () => frappe.model.can_read("Patient Consent");
krcs.privacy.can_record_consent = () => frappe.model.can_create("Patient Consent");

krcs.privacy.show_status = function (frm, status) {
	if (!status) return;
	const label =
		status.outcome === "none"
			? __("Consent: not recorded")
			: __("Consent: {0} ({1}, {2})", [
					__(status.outcome),
					frappe.datetime.str_to_user(status.date),
					status.version || "-",
			  ]);
	frm.dashboard.add_indicator(label, krcs.privacy.OUTCOME_COLOURS[status.outcome] || "grey");
};

krcs.privacy.load_status = function (frm) {
	return frappe.xcall(`${krcs.privacy.CONSENT_API}.get_consent_status`, { patient: frm.doc.name }, "GET");
};

// `on_close` (optional) runs once when the dialog closes, recorded or not: registration continues with it.
krcs.privacy.record_consent_dialog = function (frm, intro, on_close) {
	const done = () => {
		const next = on_close;
		on_close = null;
		if (next) next();
	};
	frappe.xcall(`${krcs.privacy.STATEMENT_API}.get_active_statements`, {}, "GET").then((statements) => {
		statements = statements || [];
		if (!statements.length) {
			frappe.msgprint(__("No Active Consent Statement exists. Ask the Medical In-Charge to activate one."));
			done();
			return;
		}
		const lang = frappe.boot.lang || "en";
		const preferred = statements.find((s) => s.language === lang) || statements[0];
		const by_name = Object.fromEntries(statements.map((s) => [s.name, s]));
		const dialog = new frappe.ui.Dialog({
			title: __("Record Consent"),
			fields: [
				{ fieldtype: "HTML", fieldname: "intro" },
				{
					fieldname: "statement",
					fieldtype: "Select",
					label: __("Consent Statement"),
					options: statements.map((s) => s.name).join("\n"),
					default: preferred.name,
					reqd: 1,
					onchange: () => render_text(),
				},
				{ fieldtype: "HTML", fieldname: "statement_text" },
				{
					fieldname: "outcome",
					fieldtype: "Select",
					label: __("Outcome"),
					options: ["", "Given", "Withheld", "Unable to Consent", "Withdrawn"].join("\n"),
					reqd: 1,
				},
				{
					fieldname: "given_by",
					fieldtype: "Select",
					label: __("Given By"),
					options: "Patient\nRepresentative",
					default: "Patient",
					reqd: 1,
				},
				{
					fieldname: "representative_name",
					fieldtype: "Data",
					label: __("Representative Name"),
					depends_on: "eval:doc.given_by=='Representative'",
					mandatory_depends_on: "eval:doc.given_by=='Representative'",
				},
				{
					fieldname: "relationship",
					fieldtype: "Data",
					label: __("Relationship to Patient"),
					depends_on: "eval:doc.given_by=='Representative'",
					mandatory_depends_on: "eval:doc.given_by=='Representative'",
				},
				{
					fieldname: "reason",
					fieldtype: "Small Text",
					label: __("Reason"),
					depends_on: "eval:doc.outcome && doc.outcome!='Given'",
					mandatory_depends_on: "eval:doc.outcome && doc.outcome!='Given'",
					description: __("Care continues; the reason is recorded."),
				},
			],
			primary_action_label: __("Record Consent"),
			primary_action(values) {
				frappe
					.xcall(`${krcs.privacy.CONSENT_API}.record_consent`, Object.assign({ patient: frm.doc.name }, values))
					.then(() => {
						dialog.hide();
						frappe.show_alert({ message: __("Consent recorded"), indicator: "green" });
						frm.reload_doc();
					});
			},
		});
		const render_text = () => {
			const s = by_name[dialog.get_value("statement")];
			const note = s && s.is_provisional && s.provisional_note
				? `<p class="text-warning">${frappe.utils.escape_html(s.provisional_note)}</p>`
				: "";
			dialog.fields_dict.statement_text.$wrapper.html(
				s ? `${note}<div style="max-height: 260px; overflow-y: auto; border: 1px solid var(--border-color); padding: 8px; border-radius: 4px;">${s.text || ""}</div>` : ""
			);
		};
		if (intro) dialog.fields_dict.intro.$wrapper.html(`<p>${frappe.utils.escape_html(intro)}</p>`);
		dialog.onhide = done;
		dialog.show();
		render_text();
	});
};

frappe.ui.form.on("Patient", {
	before_save(frm) {
		frm.__krcs_consent_first_save = frm.is_new();
	},

	// Registering a new patient continues on this screen: consent first, then the visit (visit/patient.js
	// listens for `krcs_registration_next`).
	after_save(frm) {
		if (!frm.__krcs_consent_first_save) return;
		frm.__krcs_consent_first_save = false;
		const next = () => frm.trigger("krcs_registration_next");
		if (!krcs.privacy.can_record_consent()) return next();
		krcs.privacy.load_status(frm).then((status) => {
			if (status && status.outcome === "none") {
				krcs.privacy.record_consent_dialog(
					frm,
					__("No consent is recorded for this patient yet. Read the statement to the patient and record the outcome."),
					next
				);
			} else {
				next();
			}
		});
	},

	refresh(frm) {
		if (frm.is_new()) {
			// Registration is one screen: say what happens after Save (consent, then the visit).
			if (krcs.privacy.can_record_consent()) {
				frm.set_intro(
					__("After you save, you will record the patient's consent and then start their visit, without leaving this page."),
					"blue"
				);
			}
			return;
		}
		if (frm.doc.krcs_merged_into || !krcs.privacy.can_read_consent()) return;
		krcs.privacy.load_status(frm).then((status) => krcs.privacy.show_status(frm, status));
		if (krcs.privacy.can_record_consent()) {
			frm.add_custom_button(__("Record Consent"), () => krcs.privacy.record_consent_dialog(frm));
		}
	},
});
