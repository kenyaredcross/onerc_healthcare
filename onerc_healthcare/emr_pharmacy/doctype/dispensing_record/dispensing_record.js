// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Dispensing Record (FR-21, FR-22): one dispensing event against one prescription line.

const KRCS_OPEN_LINES = ["Pending", "Partly Dispensed"];

frappe.ui.form.on("Dispensing Record", {
	setup(frm) {
		frm.set_query("medication_request", () => ({
			filters: { docstatus: 1, krcs_dispense_status: ["in", KRCS_OPEN_LINES] },
		}));
		frm.set_query("dispensed_medication", () => ({
			filters: { disabled: 0, is_orderable: 1 },
		}));
		frm.set_query("dispensed_item", () => ({
			query: "onerc_healthcare.pharmacy.catalogue.dispensable_item_query",
			filters: frm.doc.dispensed_medication
				? { medication: frm.doc.dispensed_medication }
				: {},
		}));
	},

	refresh(frm) {
		frm.trigger("show_line_summary");
		if (frm.doc.docstatus === 1 && frm.perm[0] && frm.perm[0].cancel) {
			frm.add_custom_button(__("Cancel with Reason"), () => {
				frappe.prompt(
					[
						{
							fieldname: "reason",
							fieldtype: "Small Text",
							label: __("Reason"),
							reqd: 1,
						},
					],
					(values) => {
						frappe
							.xcall("onerc_healthcare.pharmacy.dispensing.cancel_dispensing", {
								name: frm.doc.name,
								reason: values.reason,
							})
							.then(() => frm.reload_doc());
					},
					__("Cancel Dispensing Record"),
					__("Cancel Record")
				);
			});
		}
	},

	medication_request(frm) {
		frm.set_value({ dispensed_medication: "", dispensed_item: "", substitution_reason: "" });
		frm.trigger("show_line_summary");
	},

	show_line_summary(frm) {
		if (!frm.doc.medication_request) return;
		frappe
			.xcall("onerc_healthcare.pharmacy.dispensing.get_line_summary", {
				medication_request: frm.doc.medication_request,
			})
			.then((s) => {
				frm.dashboard.set_headline(
					__("Line {0}: prescribed {1}, dispensed {2}, remaining {3}.", [
						__(s.state),
						s.prescribed,
						s.dispensed,
						s.remaining,
					])
				);
				if (frm.is_new()) {
					const prefill = {};
					[
						"patient",
						"patient_name",
						"encounter",
						"prescribed_medication",
						"prescribed_item",
						"prescribed_dosage",
						"prescribed_period",
					].forEach((f) => {
						if (s[f] && !frm.doc[f]) prefill[f] = s[f];
					});
					prefill.prescribed_quantity = s.prescribed;
					prefill.previously_dispensed = s.dispensed;
					frm.set_value(prefill);
				}
				if (
					frm.doc.docstatus === 0 &&
					frm.doc.outcome === "Dispensed" &&
					!frm.doc.quantity
				) {
					frm.set_value("quantity", s.remaining);
				}
			});
	},

	dispensed_medication(frm) {
		if (frm.doc.dispensed_medication === frm.doc.prescribed_medication) {
			frm.set_value("dispensed_item", frm.doc.prescribed_item);
		} else {
			frm.set_value("dispensed_item", "");
		}
		frm.trigger("set_substitution");
	},

	dispensed_item(frm) {
		frm.trigger("set_substitution");
	},

	set_substitution(frm) {
		const item = frm.doc.dispensed_item;
		frm.set_value("is_substitution", item && item !== frm.doc.prescribed_item ? 1 : 0);
	},

	outcome(frm) {
		if (frm.doc.outcome === "Not Dispensed") {
			frm.set_value({ final: 1, quantity: 0 });
		}
	},
});
