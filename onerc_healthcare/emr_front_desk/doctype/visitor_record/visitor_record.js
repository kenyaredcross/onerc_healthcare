// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Visitor Record (FR-41): Inside -> Exited through "Record Exit"; "Record Screening" opens a screening.

frappe.ui.form.on("Visitor Record", {
	setup(frm) {
		frm.set_query("facility", () => ({ filters: { is_group: 1 } }));
		frm.set_query("visit", () => ({
			filters: frm.doc.patient ? { patient: frm.doc.patient } : {},
		}));
	},

	refresh(frm) {
		if (frm.is_new()) return;
		frm.page.set_indicator(__(frm.doc.status), frm.doc.status === "Inside" ? "green" : "gray");
		if (frm.doc.status === "Inside" && frappe.user.has_role("Nurse Receptionist")) {
			frm.add_custom_button(__("Record Exit"), () => {
				frappe.prompt(
					[
						{
							fieldname: "exit_time",
							fieldtype: "Datetime",
							label: __("Exit Time"),
							default: frappe.datetime.now_datetime(),
							reqd: 1,
						},
						{ fieldname: "comment", fieldtype: "Small Text", label: __("Comment") },
					],
					(values) => {
						frappe
							.xcall("onerc_healthcare.frontdesk.visitor.record_exit", {
								visitor_record: frm.doc.name,
								exit_time: values.exit_time,
								comment: values.comment,
							})
							.then(() => frm.reload_doc());
					},
					__("Record Exit"),
					__("Record Exit")
				);
			}).addClass("btn-primary");
		}
		if (frappe.model.can_create("Screening Record")) {
			frm.add_custom_button(__("Record Screening"), () => {
				frappe.new_doc("Screening Record", {
					person_type: "Visitor",
					visitor_record: frm.doc.name,
					person_name: frm.doc.visitor_name,
					facility: frm.doc.facility,
				});
			});
		}
	},

	patient(frm) {
		if (!frm.doc.patient) frm.set_value("visit", "");
	},
});
