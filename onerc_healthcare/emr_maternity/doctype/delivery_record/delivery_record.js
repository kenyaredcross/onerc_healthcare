// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Delivery Record (INP-16, M3): one row per baby; on submit each live-born baby is registered as a Patient
// linked to the mother. Never cancelled: corrections go in the addendum.

const KRCS_DELIVERY_API = "onerc_healthcare.maternity.delivery";
const KRCS_LIVE_BIRTH = "Live birth";
const KRCS_DELIVERY_ADMISSION_STATUSES = ["Admitted", "In Labour"];
const KRCS_ADDENDUM_ROLES = ["Midwife", "Clinical Officer", "Medical In-Charge"];

frappe.ui.form.on("Delivery Record", {
	setup(frm) {
		frm.set_query("admission", () => ({
			filters: { status: ["in", KRCS_DELIVERY_ADMISSION_STATUSES], krcs_archived: 0 },
		}));
	},

	onload(frm) {
		if (frm.is_new()) {
			if (!frm.doc.delivered_by) frm.set_value("delivered_by", frappe.session.user);
			if (!(frm.doc.babies || []).length) krcs_delivery_add_baby(frm);
		}
	},

	refresh(frm) {
		frm.trigger("show_newborns");
		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(__("Add Baby (twin)"), () => krcs_delivery_add_baby(frm));
		}
		if (frm.doc.docstatus === 1 && KRCS_ADDENDUM_ROLES.some((r) => frappe.user.has_role(r))) {
			frm.add_custom_button(__("Add Addendum"), () => krcs_delivery_add_addendum(frm));
		}
		// Correction of a submitted record (Medical In-Charge): archived and replaced by a new draft.
		if (frm.doc.docstatus === 1 && !frm.doc.krcs_archived && frappe.user.has_role("Medical In-Charge")) {
			frm.add_custom_button(__("Supersede Delivery"), () => krcs_delivery_supersede(frm));
		}
		if (frm.doc.krcs_archived) {
			frm.dashboard.set_headline_alert(
				__("This Delivery Record is archived (superseded); it no longer counts in reports."),
				"orange",
			);
		}
	},

	born_before_arrival(frm) {
		if (frm.doc.born_before_arrival) {
			frappe.show_alert({
				message: __(
					"Born before arrival: the delivery time may be before the admission. Still choose the mode of delivery."
				),
				indicator: "blue",
			});
		}
	},

	on_submit(frm) {
		// The server registered the live-born babies during the submit; say so once.
		const babies = frm.doc.babies || [];
		const live = babies.filter((b) => b.outcome === KRCS_LIVE_BIRTH);
		const lines = live.map((b) =>
			b.newborn_patient
				? __("Baby {0}: {1}", [b.idx, krcs_delivery_patient_link(b.newborn_patient)])
				: __("Baby {0}: not registered", [b.idx])
		);
		const stillbirths = babies.length - live.length;
		if (stillbirths) {
			lines.push(__("{0} stillbirth(s): no patient record", [stillbirths]));
		}
		frappe.msgprint({
			title: __("Register newborns"),
			message: live.length
				? (frm.doc.supersedes
						? __("{0} newborn patient(s), linked to the mother (kept from {1} or registered now):", [
								live.length,
								frm.doc.supersedes,
							])
						: __("Registered {0} newborn patient(s), linked to the mother:", [live.length])) +
				  "<br>" +
				  lines.join("<br>")
				: lines.join("<br>") || __("No live births: no newborn patient was registered."),
			indicator: "green",
		});
	},

	show_newborns(frm) {
		if (frm.doc.docstatus !== 1) return;
		const links = (frm.doc.babies || [])
			.filter((b) => b.newborn_patient)
			.map((b) =>
				__("Baby {0}: {1}", [b.idx, krcs_delivery_patient_link(b.newborn_patient)])
			);
		if (links.length) {
			frm.dashboard.set_headline(__("Newborn patients") + ": " + links.join(" · "));
		}
	},
});

frappe.ui.form.on("Delivery Baby", {
	babies_add(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.outcome) frappe.model.set_value(cdt, cdn, "outcome", KRCS_LIVE_BIRTH);
	},
});

function krcs_delivery_add_baby(frm) {
	frm.add_child("babies", { outcome: KRCS_LIVE_BIRTH });
	frm.refresh_field("babies");
}

function krcs_delivery_patient_link(name) {
	const href = frappe.utils.get_form_link("Patient", name);
	return `<a href="${href}">${frappe.utils.escape_html(name)}</a>`;
}

function krcs_delivery_add_addendum(frm) {
	frappe.prompt(
		[{ fieldname: "text", fieldtype: "Small Text", label: __("Correction"), reqd: 1 }],
		(values) => {
			frappe
				.xcall(`${KRCS_DELIVERY_API}.add_addendum`, {
					delivery: frm.doc.name,
					text: values.text,
				})
				.then(() => frm.reload_doc());
		},
		__("Add Addendum"),
		__("Add")
	);
}

function krcs_delivery_supersede(frm) {
	frappe.prompt(
		[
			{
				fieldname: "reason",
				fieldtype: "Small Text",
				label: __("Reason for the correction"),
				reqd: 1,
				description: __(
					"This record is archived and a new draft copying it is opened: correct it there, then a Midwife or Clinical Officer submits it."
				),
			},
		],
		(values) => {
			frappe
				.xcall(`${KRCS_DELIVERY_API}.supersede`, { delivery: frm.doc.name, reason: values.reason })
				.then((r) => frappe.set_route("Form", "Delivery Record", r.name));
		},
		__("Supersede Delivery"),
		__("Supersede"),
	);
}
