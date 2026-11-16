// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Phase 5M (M1): "Admit in Labour" on the Patient form (fast path: the visit is opened and closed as Admitted
// behind the scenes). Needs maternity/admit_dialog.js (loaded before this file through hooks.py doctype_js).

frappe.ui.form.on("Patient", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.sex === "Male" || frm.doc.status === "Disabled") return;
		if (!krcs.maternity.can_admit() && !frappe.model.can_read("Maternity Admission")) return;
		frappe
			.xcall(krcs.maternity.API + "get_patient_state", { patient: frm.doc.name }, "GET")
			.then((state) => {
				if (state.admission) {
					frm.add_custom_button(
						__("Maternity Admission"),
						() => krcs.maternity.open_admission(state.admission),
						__("View"),
					);
					return;
				}
				if (!state.can_admit) return;
				frm.add_custom_button(__("Admit in Labour"), () =>
					krcs.maternity.admit_dialog({
						patient: frm.doc.name,
						patient_name: frm.doc.patient_name,
						fast_path: true,
						title: __("Admit in Labour: {0}", [frappe.utils.escape_html(frm.doc.patient_name || frm.doc.name)]),
					}),
				);
			});
	},
});
