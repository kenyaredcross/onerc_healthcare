// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Phase 5M (M1): the "Admit to Maternity" / "Admit in Labour" dialog shared by the Patient and Patient Visit forms.
// The server (onerc_healthcare.maternity.admission.admit) checks the role, the visit, the open admission, the ward
// and the bed; the rest of the admission (history, examination, diagnoses) is completed on the admission form.

frappe.provide("krcs.maternity");

krcs.maternity.API = "onerc_healthcare.maternity.admission.";
krcs.maternity.ROLES = ["Midwife", "Clinical Officer", "Medical In-Charge"];

krcs.maternity.can_admit = () => frappe.user.has_role(krcs.maternity.ROLES);

krcs.maternity.open_admission = (name) => frappe.set_route("Form", "Maternity Admission", name);

krcs.maternity.bed_query = (ward) => ({
	filters: {
		parent_healthcare_service_unit: ward || "__none__",
		is_group: 0,
		service_unit_type: "Bed",
		occupancy_status: "Vacant",
	},
});

// opts: {patient, patient_name, visit, title, fast_path}
krcs.maternity.admit_dialog = function (opts) {
	frappe
		.xcall(krcs.maternity.API + "get_admit_defaults", { patient: opts.patient, visit: opts.visit || null }, "GET")
		.then((defaults) => {
			const fields = [];
			if (opts.fast_path) {
				fields.push({
					fieldname: "facility",
					fieldtype: "Link",
					options: "Healthcare Service Unit",
					label: __("Facility"),
					reqd: 1,
					default: defaults.facility,
					read_only: defaults.open_visit ? 1 : 0,
					description: defaults.message
						? frappe.utils.escape_html(defaults.message)
						: __("A visit is opened here and closed as Admitted."),
					get_query: () => ({ filters: { is_group: 1, parent_healthcare_service_unit: ["is", "set"] } }),
					onchange() {
						const facility = dialog.get_value("facility");
						if (!facility) return;
						frappe
							.xcall(krcs.maternity.API + "get_admit_defaults", { facility }, "GET")
							.then((d) => dialog.set_value("ward", d.ward || ""));
					},
				});
			}
			fields.push(
				{
					fieldname: "ward",
					fieldtype: "Link",
					options: "Healthcare Service Unit",
					label: __("Ward"),
					default: defaults.ward,
					get_query: () => ({ filters: { is_group: 1 } }),
					onchange() {
						dialog.set_value("bed", "");
					},
				},
				{
					fieldname: "bed",
					fieldtype: "Link",
					options: "Healthcare Service Unit",
					label: __("Bed"),
					description: __("Optional. Vacant beds of the ward."),
					get_query: () => krcs.maternity.bed_query(dialog.get_value("ward")),
				},
				{ fieldtype: "Column Break" },
				{ fieldname: "lmp", fieldtype: "Date", label: __("LMP") },
				{
					fieldname: "gravida",
					fieldtype: "Int",
					label: __("Gravida"),
					reqd: 1,
					description: __("Every pregnancy including this one (1 for a first pregnancy)."),
				},
				{
					fieldname: "para",
					fieldtype: "Data",
					label: __("Para"),
					description: __("Earlier births (+ abortions), e.g. 2 or 2+1; at most gravida - 1."),
				},
				{ fieldtype: "Section Break" },
				{ fieldname: "remarks", fieldtype: "Small Text", label: __("Remarks") },
			);
			const dialog = new frappe.ui.Dialog({
				title: opts.title || __("Admit to Maternity"),
				fields,
				size: "large",
				primary_action_label: __("Admit"),
				primary_action(values) {
					const clinical = {};
					["lmp", "gravida", "para", "remarks"].forEach((k) => {
						if (values[k]) clinical[k] = values[k];
					});
					if (opts.fast_path && values.facility) clinical.facility = values.facility;
					dialog.disable_primary_action();
					frappe
						.call({
							method: krcs.maternity.API + "admit",
							type: "POST",
							args: {
								patient: opts.patient,
								visit: opts.visit || null,
								ward: values.ward || null,
								bed: values.bed || null,
								values: clinical,
							},
							freeze: true,
							freeze_message: __("Admitting..."),
						})
						.then((r) => {
							dialog.hide();
							frappe.show_alert({
								message: __("{0} admitted ({1})", [
									frappe.utils.escape_html(opts.patient_name || opts.patient),
									r.message.name,
								]),
								indicator: "green",
							});
							krcs.maternity.open_admission(r.message.name);
						})
						.always(() => dialog.enable_primary_action());
				},
			});
			dialog.show();
		});
};
