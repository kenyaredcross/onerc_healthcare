// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Medication Request form (B3): dispensing status and the Pharmacist's "Dispense" action (FR-20, FR-21).
// Medicines are non-stock (C-5, SDD §5.5), so stock / billing UI is hidden.

const KRCS_DISPENSE_COLORS = {
	Pending: "orange",
	"Partly Dispensed": "yellow",
	Dispensed: "green",
	"Not Dispensed": "red",
	Closed: "blue",
};
const KRCS_HIDDEN_MR_FIELDS = ["billing_section", "qty_invoiced", "billing_status", "item_code"];

frappe.ui.form.on("Medication Request", {
	refresh(frm) {
		KRCS_HIDDEN_MR_FIELDS.forEach((f) => {
			if (frm.fields_dict[f]) frm.set_df_property(f, "hidden", 1);
		});

		const can_write = frm.perm && frm.perm[0] && frm.perm[0].write;
		if (!can_write) {
			// Marley's Status buttons need write on the request; only clinicians hold it.
			frm.remove_custom_button(__("On Hold"), __("Status"));
			frm.remove_custom_button(__("Active"), __("Status"));
			frm.remove_custom_button(__("Revoked"), __("Status"));
			frm.remove_custom_button(__("Entered in Error"), __("Status"));
			frm.remove_custom_button(__("Unknown"), __("Status"));
		}

		if (frm.doc.docstatus !== 1) return;
		const status = frm.doc.krcs_dispense_status || "Pending";
		frm.page.set_indicator(__(status), KRCS_DISPENSE_COLORS[status] || "gray");
		frm.dashboard.add_comment(
			__("Dispense status: {0}. Quantity dispensed: {1} of {2}.", [
				__(status),
				frm.doc.krcs_qty_dispensed || 0,
				frm.doc.total_dispensable_quantity || frm.doc.quantity || 0,
			]),
			KRCS_DISPENSE_COLORS[status] || "blue",
			true
		);

		if (frappe.model.can_read("Dispensing Record")) {
			frm.add_custom_button(__("Dispensing Records"), () => {
				frappe.set_route("List", "Dispensing Record", {
					medication_request: frm.doc.name,
				});
			});
		}
		const open = ["Pending", "Partly Dispensed"].includes(status);
		if (open && frappe.model.can_create("Dispensing Record")) {
			frm.add_custom_button(__("Dispense"), () => {
				frappe.new_doc("Dispensing Record", { medication_request: frm.doc.name });
			}).addClass("btn-primary");
		}
	},
});
