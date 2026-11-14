// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Medication Request form (B3): dispensing status and the Pharmacist's "Dispense" action (FR-20, FR-21).
// Medicines are non-stock (C-5, SDD §5.5), so stock / billing UI is hidden.
// Prescriber Hold / Stop / Resume (fix round L3) go through onerc_healthcare.pharmacy.api, which checks the
// role and, for a Clinical Officer / Mental Health Officer, that the line is their own.

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

		// Marley's Status buttons would bypass the prescriber hold and the dispensing line state.
		["On Hold", "Active", "Revoked", "Entered in Error", "Unknown"].forEach((label) =>
			frm.remove_custom_button(__(label), __("Status"))
		);

		if (frm.doc.docstatus !== 1) return;
		const status = frm.doc.krcs_dispense_status || "Pending";
		const hold = frm.doc.krcs_hold_status || "";
		if (hold) {
			frm.page.set_indicator(__(hold), hold === "Held" ? "orange" : "red");
		} else {
			frm.page.set_indicator(__(status), KRCS_DISPENSE_COLORS[status] || "gray");
		}
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
		if (open && !hold && frappe.model.can_create("Dispensing Record")) {
			frm.add_custom_button(__("Dispense"), () => {
				frappe.new_doc("Dispensing Record", { medication_request: frm.doc.name });
			}).addClass("btn-primary");
		}
		if (
			open &&
			frappe.user.has_role([
				"Clinical Officer",
				"Mental Health Officer",
				"Medical In-Charge",
			])
		) {
			const actions = [];
			if (!hold) actions.push(["hold_line", __("Hold")]);
			if (hold !== "Stopped") actions.push(["stop_line", __("Stop")]);
			if (hold === "Held") actions.push(["resume_line", __("Resume")]);
			actions.forEach(([method, label]) => {
				frm.add_custom_button(
					label,
					() => krcs_line_action(frm, method, label),
					__("Prescription Line")
				);
			});
		}
	},
});

// Literal method paths (POST endpoints in onerc_healthcare.pharmacy.api).
const KRCS_LINE_API = {
	hold_line: (args) => frappe.xcall("onerc_healthcare.pharmacy.api.hold_line", args, "POST"),
	stop_line: (args) => frappe.xcall("onerc_healthcare.pharmacy.api.stop_line", args, "POST"),
	resume_line: (args) => frappe.xcall("onerc_healthcare.pharmacy.api.resume_line", args, "POST"),
};

function krcs_line_action(frm, method, label) {
	frappe.prompt(
		[{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 }],
		(values) => {
			KRCS_LINE_API[method]({
				medication_request: frm.doc.name,
				reason: values.reason,
			}).then(() => frm.reload_doc());
		},
		label,
		label
	);
}
