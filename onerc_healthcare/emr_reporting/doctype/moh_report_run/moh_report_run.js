// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// MOH Report Run (FR-30): action buttons, period helper and preview. Status changes only through
// onerc_healthcare.reporting.run.apply_action (D8).

frappe.provide("krcs.reporting");

krcs.reporting.RUN_API = "onerc_healthcare.reporting.run";

frappe.ui.form.on("MOH Report Run", {
	setup(frm) {
		frm.set_query("facility", () => ({ filters: { is_group: 1 } }));
	},

	onload(frm) {
		if (frm.is_new() && !frm.doc.from_date) krcs.reporting.set_default_period(frm);
		if (!krcs.reporting.realtime_bound) {
			krcs.reporting.realtime_bound = true;
			frappe.realtime.on("krcs_moh_run_generated", (data) => {
				if (cur_frm && cur_frm.doctype === "MOH Report Run" && cur_frm.doc.name === data.run) {
					cur_frm.reload_doc();
				}
			});
		}
	},

	refresh(frm) {
		const onload = frm.doc.__onload || {};
		frm.get_field("report_preview").$wrapper.html(onload.preview || "");
		if (onload.is_queued) {
			frm.dashboard.set_headline_alert(
				__("Being generated in the background (queued {0}). The form reloads when it is ready.", [
					frappe.datetime.str_to_user(frm.doc.queued_at),
				]),
				"orange"
			);
		} else if (frm.doc.generation_status === "Failed") {
			frm.dashboard.set_headline_alert(__("Background generation failed. See Generation Error."), "red");
		} else if (frm.doc.report_status && frm.doc.report_status !== "Confirmed" && !frm.is_new()) {
			frm.dashboard.set_headline_alert(__("PROVISIONAL — HIS Officer to confirm (OI-5)"), "yellow");
		}
		if (frm.is_new()) return;
		(onload.available_actions || []).forEach((a) => {
			const button = frm.add_custom_button(__(a.label), () => krcs.reporting.run_action(frm, a), __("Actions"));
			if (a.action === "generate") button.addClass("btn-primary");
		});
	},

	period_type(frm) {
		krcs.reporting.set_default_period(frm);
	},

	from_date(frm) {
		krcs.reporting.snap_period(frm);
	},
});

krcs.reporting.set_default_period = function (frm) {
	if (!frm.is_new() || frm.doc.period_type === "Custom") return;
	frappe
		.xcall("onerc_healthcare.reporting.run.default_period", { period_type: frm.doc.period_type || "Monthly" }, "GET")
		.then((p) => frm.set_value({ from_date: p.from_date, to_date: p.to_date }));
};

// Monthly and quarterly periods are whole months or calendar quarters: snap the end date to the start.
krcs.reporting.snap_period = function (frm) {
	if (!frm.is_new() || !frm.doc.from_date) return;
	const start = moment(frm.doc.from_date);
	if (frm.doc.period_type === "Monthly") {
		frm.set_value({
			from_date: start.clone().startOf("month").format("YYYY-MM-DD"),
			to_date: start.clone().endOf("month").format("YYYY-MM-DD"),
		});
	} else if (frm.doc.period_type === "Quarterly") {
		frm.set_value({
			from_date: start.clone().startOf("quarter").format("YYYY-MM-DD"),
			to_date: start.clone().endOf("quarter").format("YYYY-MM-DD"),
		});
	}
};

krcs.reporting.run_action = function (frm, a) {
	const run = (values) =>
		frappe
			.xcall("onerc_healthcare.reporting.run.apply_action", Object.assign({ run: frm.doc.name, action: a.action }, values || {}))
			.then((r) => {
				if (r && r.queued) {
					frappe.show_alert({ message: __("Queued: the report is being generated in the background."), indicator: "orange" });
				}
				if (r && r.new_run) {
					frappe.set_route("Form", "MOH Report Run", r.new_run);
				} else {
					frm.reload_doc();
				}
			});
	if (a.requires_reason) {
		frappe.prompt(
			[{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 }],
			(values) => run(values),
			__(a.label),
			__(a.label)
		);
		return;
	}
	if (a.action === "submit_run") {
		frappe.confirm(
			__("Mark this run as submitted to the Ministry of Health? It cannot be changed afterwards; a correction needs a revision."),
			() => run()
		);
		return;
	}
	if (a.action === "generate") {
		frappe.dom.freeze(__("Generating…"));
		run().finally(() => frappe.dom.unfreeze());
		return;
	}
	run();
};
