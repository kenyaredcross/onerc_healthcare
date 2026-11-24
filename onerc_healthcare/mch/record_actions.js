// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// MCH record forms (DEFECT-MCH-6): Add Addendum on a submitted record, Archive for the Medical In-Charge, and
// Close Pregnancy on an open ANC Pregnancy. The server (mch.common.add_addendum / archive, mch.anc.close_pregnancy)
// checks the roles, the permission and the record state; these buttons only appear where they can work.

frappe.provide("krcs.mch");

krcs.mch.MCH_ROLES = ["Clinical Officer", "Midwife", "Medical In-Charge"];
krcs.mch.CHILD_ROLES = krcs.mch.MCH_ROLES.concat(["Triage Nurse"]);
krcs.mch.ADDENDUM_ROLES = {
	"ANC Contact": krcs.mch.MCH_ROLES,
	"PNC Visit": krcs.mch.MCH_ROLES,
	Immunisation: krcs.mch.CHILD_ROLES,
	"Growth Monitoring": krcs.mch.CHILD_ROLES,
};

krcs.mch.post = (method, args, frm, message) =>
	frappe.xcall(method, args).then(() => {
		frappe.show_alert({ message, indicator: "green" });
		frm.reload_doc();
	});

krcs.mch.record_buttons = function (frm) {
	if (frm.is_new()) return;
	const archived = cint(frm.doc.krcs_archived);
	const can_write = frm.perm && frm.perm[0] && frm.perm[0].write;
	const addendum_roles = krcs.mch.ADDENDUM_ROLES[frm.doctype];

	if (addendum_roles && frm.doc.docstatus === 1 && !archived && can_write && frappe.user.has_role(addendum_roles)) {
		frm.add_custom_button(__("Add Addendum"), () =>
			frappe.prompt(
				[{ fieldname: "text", fieldtype: "Small Text", label: __("Correction"), reqd: 1 }],
				(v) =>
					krcs.mch.post(
						"onerc_healthcare.mch.common.add_addendum",
						{ doctype: frm.doctype, name: frm.doc.name, text: v.text },
						frm,
						__("Addendum added"),
					),
				__("Add Addendum"),
				__("Add"),
			),
		);
	}

	if (frm.doctype === "ANC Pregnancy" && frm.doc.status === "Open" && !archived && can_write) {
		if (frappe.user.has_role(krcs.mch.MCH_ROLES)) {
			frm.add_custom_button(__("Close Pregnancy"), () =>
				frappe.prompt(
					[
						{
							fieldname: "reason",
							fieldtype: "Select",
							label: __("Reason"),
							options: ["Pregnancy ended", "Transferred out"],
							reqd: 1,
							description: __("A delivery closes the pregnancy by itself (Delivery Record)."),
						},
						{ fieldname: "note", fieldtype: "Small Text", label: __("Note") },
					],
					(v) =>
						krcs.mch.post(
							"onerc_healthcare.mch.anc.close_pregnancy",
							{ pregnancy: frm.doc.name, reason: v.reason, note: v.note || null },
							frm,
							__("Pregnancy closed"),
						),
					__("Close Pregnancy"),
					__("Close"),
				),
			);
		}
	}

	if (!archived && frm.doc.docstatus < 2 && frappe.user.has_role("Medical In-Charge")) {
		frm.add_custom_button(
			__("Archive"),
			() =>
				frappe.prompt(
					[
						{
							fieldname: "reason",
							fieldtype: "Small Text",
							label: __("Reason"),
							reqd: 1,
							description: __("For a record made in error (wrong patient or visit). Never deleted."),
						},
					],
					(v) =>
						krcs.mch.post(
							"onerc_healthcare.mch.common.archive",
							{ doctype: frm.doctype, name: frm.doc.name, reason: v.reason },
							frm,
							__("Archived"),
						),
					__("Archive {0}", [__(frm.doctype)]),
					__("Archive"),
				),
			__("Actions"),
		);
	}
};

["ANC Pregnancy", "ANC Contact", "PNC Visit", "Immunisation", "Growth Monitoring"].forEach((doctype) =>
	frappe.ui.form.on(doctype, { refresh: krcs.mch.record_buttons }),
);
