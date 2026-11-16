// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Phase 5M (M1): "Admit to Maternity" on the Patient Visit form, for a visit waiting in Registered / In Triage /
// Routed to Department (the engine's admit closes it: Discharged, outcome Admitted) or already closed by the
// clinician with outcome Admitted. Needs maternity/admit_dialog.js (hooks.py doctype_js, loaded first).

frappe.ui.form.on("Patient Visit", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.patient) return;
		if (!krcs.maternity.can_admit() && !frappe.model.can_read("Maternity Admission")) return;
		frappe.xcall(krcs.maternity.API + "get_visit_state", { visit: frm.doc.name }, "GET").then((state) => {
			if (state.admission) {
				frm.add_custom_button(
					__("Maternity Admission"),
					() => krcs.maternity.open_admission(state.admission),
					__("View"),
				);
				return;
			}
			if (state.open_admission) {
				frm.add_custom_button(
					__("Open Maternity Admission"),
					() => krcs.maternity.open_admission(state.open_admission),
					__("View"),
				);
				return;
			}
			if (!state.can_admit) return;
			const btn = frm.add_custom_button(__("Admit to Maternity"), () =>
				krcs.maternity.admit_dialog({
					patient: frm.doc.patient,
					patient_name: frm.doc.patient_name,
					visit: frm.doc.name,
					title: __("Admit to Maternity: {0}", [
						frappe.utils.escape_html(frm.doc.patient_name || frm.doc.patient),
					]),
				}),
			);
			if (state.maternity && btn) btn.removeClass("btn-default").addClass("btn-primary");
		});
	},
});
