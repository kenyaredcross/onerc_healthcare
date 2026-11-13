// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Data Access Request (FR-38): action buttons. Status changes only through privacy.dar.apply_action.

frappe.provide("krcs.privacy");

krcs.privacy.DAR_API = "onerc_healthcare.privacy.dar";

frappe.ui.form.on("Data Access Request", {
	refresh(frm) {
		const onload = frm.doc.__onload || {};
		if (onload.is_overdue) {
			frm.dashboard.set_headline_alert(
				__("Overdue: the response was due on {0}.", [frappe.datetime.str_to_user(frm.doc.due_date)]),
				"red"
			);
		}
		if (frm.is_new()) return;
		(onload.available_actions || []).forEach((a) => {
			frm.add_custom_button(__(a.label), () => krcs.privacy.dar_action(frm, a), __("Actions"));
		});
	},
});

krcs.privacy.dar_fields = function (frm, action) {
	const onload = frm.doc.__onload || {};
	const opts = (list) => [""].concat(list || []).join("\n");
	switch (action) {
		case "verify_identity":
			return [
				{ fieldname: "identity_method", fieldtype: "Select", label: __("Identity Method"), reqd: 1, options: opts(onload.identity_methods) },
				{
					fieldname: "identity_evidence",
					fieldtype: "Small Text",
					label: __("Identity Evidence"),
					reqd: frm.doc.requester_type === "Authorised Representative" ? 1 : 0,
					description: __("Document number or how identity was confirmed."),
				},
			];
		case "approve":
			return [{ fieldname: "decision_reason", fieldtype: "Small Text", label: __("Comment") }];
		case "reject":
			return [{ fieldname: "decision_reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 }];
		case "record_correction":
			return [
				{ fieldname: "corrected_doctype", fieldtype: "Link", label: __("Corrected Record Type"), options: "DocType", reqd: 1 },
				{ fieldname: "corrected_record", fieldtype: "Dynamic Link", label: __("Corrected Record"), options: "corrected_doctype", reqd: 1 },
				{
					fieldname: "correction_version",
					fieldtype: "Select",
					label: __("Version Entry"),
					reqd: 1,
					description: __("The change made by the record's owner after this request was received."),
				},
			];
		case "respond":
			return [
				{ fieldname: "response_date", fieldtype: "Date", label: __("Response Date"), reqd: 1, default: frappe.datetime.get_today() },
				{ fieldname: "response_method", fieldtype: "Select", label: __("Response Method"), reqd: 1, options: opts(onload.response_methods) },
				{ fieldname: "comment", fieldtype: "Small Text", label: __("Comment") },
			];
		default:
			return [];
	}
};

krcs.privacy.dar_action = function (frm, a) {
	const run = (values) =>
		frappe
			.xcall(`${krcs.privacy.DAR_API}.apply_action`, Object.assign({ request: frm.doc.name, action: a.action }, values || {}))
			.then(() => frm.reload_doc());
	const fields = krcs.privacy.dar_fields(frm, a.action);
	if (!fields.length) {
		frappe.confirm(__("{0}: continue?", [__(a.label)]), () => run());
		return;
	}
	const dialog = new frappe.ui.Dialog({
		title: __(a.label),
		fields,
		primary_action_label: __(a.label),
		primary_action(values) {
			dialog.hide();
			run(values);
		},
	});
	if (a.action === "record_correction") {
		const refresh_versions = () => {
			const v = dialog.get_values(true);
			if (!v.corrected_doctype || !v.corrected_record) return;
			frappe
				.xcall(`${krcs.privacy.DAR_API}.get_correction_versions`, {
					request: frm.doc.name,
					corrected_doctype: v.corrected_doctype,
					corrected_record: v.corrected_record,
				})
				.then((rows) => {
					const field = dialog.get_field("correction_version");
					field.df.options = [""].concat((rows || []).map((r) => r.name)).join("\n");
					field.df.description = (rows || [])
						.map((r) => `${r.name}: ${r.owner}, ${frappe.datetime.str_to_user(r.creation)}`)
						.join("<br>") || __("No change of this record since the request was received.");
					field.refresh();
					if (rows && rows.length) dialog.set_value("correction_version", rows[0].name);
				});
		};
		dialog.fields_dict.corrected_record.df.onchange = refresh_versions;
	}
	dialog.show();
};
