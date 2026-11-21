// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// MCH entry points (integrator): an "MCH" button group on an open Patient Visit and on a draft MCH consultation,
// and an MCH panel on the Patient form. The server (onerc_healthcare.mch.forms) decides which records fit the
// patient and the user; the record controllers do every check. New records are prefilled with the patient and
// the visit only: the server sets the facility and the consultation link (a Triage Nurse cannot read encounters).

frappe.provide("krcs.mch");

krcs.mch.API = "onerc_healthcare.mch.forms.";

krcs.mch.add_buttons = function (frm, actions) {
	(actions || []).forEach((a) => {
		const label =
			a.action === "open"
				? __("Open {0}", [a.label])
				: a.action === "book"
					? __("Book Pregnancy (ANC)")
					: __("New {0}", [a.label]);
		frm.add_custom_button(
			label,
			() => {
				if (a.action === "open") frappe.set_route("Form", a.doctype, a.existing);
				else frappe.new_doc(a.action === "book" ? "ANC Pregnancy" : a.doctype, a.defaults || {});
			},
			__("MCH"),
		);
	});
};

frappe.ui.form.on("Patient Visit", {
	refresh(frm) {
		if (frm.is_new()) return;
		frappe
			.xcall(krcs.mch.API + "visit_actions", { visit: frm.doc.name }, "GET")
			.then((r) => krcs.mch.add_buttons(frm, r.actions));
	},
});

frappe.ui.form.on("Patient Encounter", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.docstatus !== 0 || !frm.doc.krcs_visit) return;
		frappe
			.xcall(krcs.mch.API + "encounter_actions", { encounter: frm.doc.name }, "GET")
			.then((r) => krcs.mch.add_buttons(frm, r.actions));
	},
});

krcs.mch.render_panel = function (frm, s) {
	const parts = [];
	const link = (dt, name) =>
		`<a href="/app/${frappe.router.slug(dt)}/${encodeURIComponent(name)}">${frappe.utils.escape_html(name)}</a>`;
	if (s.pregnancy) {
		parts.push(
			`<div><b>${__("Open pregnancy")}</b>: ${link("ANC Pregnancy", s.pregnancy.name)}` +
				(s.pregnancy.edd ? ` &middot; ${__("EDD")} ${frappe.datetime.str_to_user(s.pregnancy.edd)}` : "") +
				`</div>`,
		);
	}
	const counts = Object.entries(s.counts || {}).map(
		([dt, n]) =>
			`<a href="/app/${frappe.router.slug(dt)}?patient=${encodeURIComponent(frm.doc.name)}">${__(dt)}: ${n}</a>`,
	);
	if (counts.length) parts.push(`<div class="text-muted">${counts.join(" &middot; ")}</div>`);
	if (s.is_child && s.dob_estimated) {
		parts.push(`<div class="text-warning">${__("Date of birth is estimated: schedule and z-scores are approximate.")}</div>`);
	}
	parts.push(`<div class="krcs-mch-schedule"></div><div class="krcs-mch-growth"></div>`);
	frm.dashboard.add_section(parts.join(""), __("Maternal and Child Health"));
	const $section = frm.dashboard.wrapper.find(".krcs-mch-schedule").last();
	if (s.is_child && frappe.model.can_read("Immunisation")) krcs.mch.render_schedule(frm, $section);
	const $growth = frm.dashboard.wrapper.find(".krcs-mch-growth").last();
	if (s.is_child && frappe.model.can_read("Growth Monitoring") && window.onerc_mch && onerc_mch.render_growth_chart) {
		frappe
			.xcall("onerc_healthcare.mch.growth.chart_data", { patient: frm.doc.name }, "GET")
			.then((data) => data && data.points && data.points.length && onerc_mch.render_growth_chart($growth.get(0), data));
	}
};

krcs.mch.STATUS_COLOUR = { Given: "green", Due: "blue", Overdue: "orange", Missed: "red", Upcoming: "gray" };

krcs.mch.render_schedule = function (frm, $wrapper) {
	frappe.xcall("onerc_healthcare.mch.immunisation.schedule", { patient: frm.doc.name }, "GET").then((rows) => {
		if (!rows || !rows.length) return;
		const cells = rows
			.map(
				(r) =>
					`<span class="indicator-pill ${krcs.mch.STATUS_COLOUR[r.status] || "gray"}" style="margin:2px" title="${frappe.utils.escape_html(
						__(r.status) + (r.due_date ? " · " + frappe.datetime.str_to_user(r.due_date) : ""),
					)}">${frappe.utils.escape_html(r.vaccine)}</span>`,
			)
			.join("");
		$wrapper.html(`<div style="margin-top:8px"><b>${__("Immunisations")}</b><div>${cells}</div></div>`);
	});
};

frappe.ui.form.on("Patient", {
	refresh(frm) {
		if (frm.is_new()) return;
		const readable = ["ANC Pregnancy", "ANC Contact", "PNC Visit", "Immunisation", "Growth Monitoring"].some((dt) =>
			frappe.model.can_read(dt),
		);
		if (!readable) return;
		frappe
			.xcall(krcs.mch.API + "patient_summary", { patient: frm.doc.name }, "GET")
			.then((s) => {
				if (s.pregnancy || Object.keys(s.counts || {}).length || s.is_child) krcs.mch.render_panel(frm, s);
			});
	},
});
