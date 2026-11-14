// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Sync Conflict form (O2, FR-26): the facility and outreach versions side by side, a per-field choice for a
// merge, and one button per decision valid for the conflict type. Decisions go only through
// onerc_healthcare.outreach.conflicts.resolve / retry (the server checks the role and the decision).

const KRCS_RESOLVE = "onerc_healthcare.outreach.conflicts.resolve";
const KRCS_RETRY = "onerc_healthcare.outreach.conflicts.retry";
const KRCS_MERGE = "Merge field by field";
const KRCS_PRIMARY = ["Keep facility version", "Reviewed - keep both"];

frappe.ui.form.on("Sync Conflict", {
	refresh(frm) {
		const ctx = (frm.doc.__onload && frm.doc.__onload.krcs_conflict) || {};
		frm.krcs_ctx = ctx;
		frm.disable_save();
		set_indicator(frm);
		render_comparison(frm, ctx);
		(ctx.decisions || []).forEach((decision) => {
			const btn = frm.add_custom_button(__(decision), () => decide(frm, decision), __("Resolve"));
			if (KRCS_PRIMARY.includes(decision)) btn.addClass("btn-primary");
		});
		if (ctx.can_retry) {
			frm.add_custom_button(__("Retry"), () => retry(frm));
		}
		if (frm.doc.sync_record) {
			frm.add_custom_button(__("Sync Record"), () =>
				frappe.set_route("Form", "Outreach Sync Record", frm.doc.sync_record),
			);
		}
	},
});

function set_indicator(frm) {
	if (frm.doc.status === "Resolved") {
		frm.page.set_indicator(__("Resolved"), "green");
	} else if (frm.doc.review_only) {
		frm.page.set_indicator(__("Review"), "blue");
	} else {
		frm.page.set_indicator(__("Open"), "orange");
	}
}

function render_comparison(frm, ctx) {
	const field = frm.get_field("comparison_html");
	if (!field) return;
	const rows = ctx.rows || [];
	if (!rows.length) {
		field.$wrapper.html("");
		return;
	}
	const esc = frappe.utils.escape_html;
	const merge = (ctx.decisions || []).includes(KRCS_MERGE);
	const mergeable = new Set(ctx.merge_fields || []);
	const head = `<tr><th>${__("Field")}</th><th>${__("Facility version")}</th><th>${__("Outreach version")}</th>${
		merge ? `<th>${__("Keep for merge")}</th>` : ""
	}</tr>`;
	const body = rows
		.map((row) => {
			const style = row.differs ? ' style="background: var(--bg-yellow);"' : "";
			let choice = "";
			if (merge) {
				choice = "<td></td>";
				if (mergeable.has(row.field) && row.differs) {
					const name = `krcs-merge-${esc(row.field)}`;
					choice = `<td>
						<label class="mr-3"><input type="radio" name="${name}" value="facility" checked> ${__("Facility")}</label>
						<label><input type="radio" name="${name}" value="outreach"> ${__("Outreach")}</label>
					</td>`;
				}
			}
			return `<tr${style}><td>${esc(__(row.label))}</td><td>${esc(row.facility)}</td><td>${esc(
				row.outreach,
			)}</td>${choice}</tr>`;
		})
		.join("");
	let candidates = "";
	if ((ctx.candidates || []).length) {
		candidates = `<p class="mt-3"><b>${__("Possible matches")}</b></p><ul>${ctx.candidates
			.map(
				(c) =>
					`<li>${esc(c.patient)} ${esc(c.patient_name || "")}: ${esc((c.matched_by || []).join(", "))}${
						c.score != null ? ` (${__("score")} ${esc(String(c.score))})` : ""
					}${(c.reasons || []).length ? ` - ${esc(c.reasons.join(", "))}` : ""}</li>`,
			)
			.join("")}</ul>`;
	}
	field.$wrapper.html(
		`<table class="table table-bordered table-sm krcs-conflict-table"><thead>${head}</thead><tbody>${body}</tbody></table>${candidates}`,
	);
}

function chosen_fields(frm) {
	const out = {};
	const wrapper = frm.get_field("comparison_html").$wrapper;
	wrapper.find("input[type=radio]:checked").each(function () {
		out[this.name.replace("krcs-merge-", "")] = this.value;
	});
	return out;
}

function decide(frm, decision) {
	const ctx = frm.krcs_ctx || {};
	const fields = [];
	if ((ctx.needs_patient || []).includes(decision) && (ctx.candidates || []).length) {
		fields.push({
			fieldname: "patient",
			fieldtype: "Select",
			label: __("Existing patient"),
			reqd: 1,
			options: ctx.candidates.map((c) => c.patient),
			default: frm.doc.patient || ctx.candidates[0].patient,
		});
	}
	fields.push({ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 });
	const args = { conflict: frm.doc.name, decision };
	if (decision === KRCS_MERGE) {
		args.chosen_fields = chosen_fields(frm);
		if (!Object.values(args.chosen_fields).includes("outreach")) {
			frappe.msgprint(__("Choose at least one field from the outreach version in the table."));
			return;
		}
	}
	frappe.prompt(
		fields,
		(values) => {
			frappe
				.call({
					method: KRCS_RESOLVE,
					type: "POST",
					args: Object.assign(args, values),
					freeze: true,
				})
				.then(() => frm.reload_doc());
		},
		__(decision),
		__("Confirm"),
	);
}

function retry(frm) {
	frappe.prompt(
		[{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason (optional)") }],
		(values) => {
			frappe
				.call({
					method: KRCS_RETRY,
					type: "POST",
					args: { conflict: frm.doc.name, reason: values.reason },
					freeze: true,
				})
				.then((r) => {
					const out = (r && r.message) || {};
					if (!out.resolved && out.error) {
						frappe.msgprint({ title: __("Still in conflict"), message: frappe.utils.escape_html(out.error), indicator: "orange" });
					}
					frm.reload_doc();
				});
		},
		__("Retry"),
		__("Retry"),
	);
}
