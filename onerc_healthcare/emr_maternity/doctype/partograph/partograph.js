// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Partograph form (M2, INP-15): the bedside "Add hourly observation" dialog and the WHO modified partograph
// drawn inline as SVG (no external library; colours from Frappe CSS variables so dark mode works).
// Rows are appended only through onerc_healthcare.maternity.partograph.add_observation; the server checks
// the role, the admission, the ranges and computes the alert/action lines and the warnings (get_chart).

const KRCS_PTG_CHART = "onerc_healthcare.maternity.partograph.get_chart";
const KRCS_PTG_ADD = "onerc_healthcare.maternity.partograph.add_observation";
const KRCS_PTG_OPTIONS = {
	liquor: ["I", "C", "M", "B", "A"],
	moulding: ["0", "+", "++", "+++"],
	contraction_duration: ["<20 s", "20-40 s", ">40 s"],
	urine: ["Nil", "Trace", "+", "++", "+++"],
};

// Int/Float columns store 0 for "not recorded" (their ranges exclude 0): show those as blank in the table.
// The numbers whose range includes 0 (dilatation, descent, contractions, oxytocin, urine) are string columns,
// where a blank stays blank and 0 is shown as 0.
const KRCS_PTG_ZERO_IS_BLANK = [
	"foetal_heart_rate",
	"pulse",
	"bp_systolic",
	"bp_diastolic",
	"temperature",
	"respiratory_rate",
];

frappe.ui.form.on("Partograph", {
	setup() {
		KRCS_PTG_ZERO_IS_BLANK.forEach((fieldname) => {
			const df = frappe.meta.get_docfield("Partograph Observation", fieldname);
			if (!df) return;
			const standard = frappe.form.get_formatter(df.fieldtype);
			df.formatter = (value, field, options, doc) =>
				value === null || value === undefined || value === "" || Number(value) === 0
					? ""
					: standard(value, field, options, doc);
		});
	},
	refresh(frm) {
		const $chart = krcs_ptg_wrapper(frm);
		if (frm.is_new()) {
			$chart.hide();
			return;
		}
		frm.disable_save();
		$chart.show().html(`<div class="text-muted small">${__("Loading the partograph...")}</div>`);
		frappe
			.call({ method: KRCS_PTG_CHART, type: "GET", args: { partograph: frm.doc.name } })
			.then((r) => {
				const chart = r.message || {};
				frm.krcs_chart = chart;
				krcs_ptg_render(frm, $chart, chart);
				if (chart.can_add) {
					frm.add_custom_button(__("Add hourly observation"), () =>
						krcs_ptg_dialog(frm),
					).addClass("btn-primary");
				}
				krcs_ptg_indicator(frm, chart);
			});
	},
});

function krcs_ptg_indicator(frm, chart) {
	const levels = (chart.warning_details || []).map((w) => w.level);
	if (levels.includes("red")) frm.page.set_indicator(__("Warning"), "red");
	else if (levels.length) frm.page.set_indicator(__("Watch"), "orange");
	else if (chart.started_at) frm.page.set_indicator(__("Active labour"), "blue");
}

// ------------------------------------------------------------------------------------------------ layout

function krcs_ptg_wrapper(frm) {
	const html_field = frm.get_field("chart_html");
	if (html_field) return html_field.$wrapper;
	const $layout = $(frm.layout.wrapper);
	let $w = $layout.find(".krcs-ptg-chart");
	if (!$w.length) {
		$w = $('<div class="krcs-ptg-chart" style="padding: var(--padding-md, 15px) 0;"></div>');
		const $section = $(frm.fields_dict.observations.wrapper).closest(".form-section");
		if ($section.length) $section.before($w);
		else $layout.prepend($w);
	}
	return $w;
}

function krcs_ptg_render(frm, $w, chart) {
	const esc = frappe.utils.escape_html;
	const points = chart.points || [];
	const warnings = chart.warning_details || [];
	let banner = "";
	if (warnings.length) {
		const red = warnings.some((w) => w.level === "red");
		const bg = red ? "var(--bg-red, var(--red-100))" : "var(--bg-orange, var(--orange-100))";
		const fg = red ? "var(--text-on-red, var(--red-600))" : "var(--text-on-orange, var(--orange-600))";
		banner = `<div role="alert" style="background:${bg};color:${fg};border-radius:var(--border-radius-md, 8px);padding:12px 16px;margin-bottom:12px;font-size:1.05rem;">
			${warnings
				.map(
					(w) =>
						`<div style="display:flex;gap:8px;align-items:baseline;"><span style="display:inline-block;width:10px;height:10px;border-radius:50%;flex:none;background:var(${
							w.level === "red" ? "--red-500" : "--orange-500"
						});"></span><span>${esc(w.message)}</span></div>`,
				)
				.join("")}
		</div>`;
	}
	const started = chart.started_at
		? __("Active labour from {0}", [frappe.datetime.str_to_user(chart.started_at)])
		: __("Active labour not reached yet (cervix below 4 cm)");
	const add_btn = chart.can_add
		? `<button class="btn btn-primary krcs-ptg-add" style="min-height:48px;font-size:1.05rem;padding:8px 20px;">+ ${__(
				"Add hourly observation",
			)}</button>`
		: "";
	const closed =
		!chart.can_add && chart.admission_status
			? `<div class="text-muted small">${__("Admission is {0}: no more rows can be added.", [
					esc(__(chart.admission_status)),
				])}</div>`
			: "";
	const body = points.length
		? `<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;">${krcs_ptg_svg(chart)}${krcs_ptg_fhr_svg(chart)}</div>`
		: `<div class="text-muted" style="padding:24px 0;">${__("No observations yet.")}</div>`;
	$w.html(`
		${banner}
		<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:8px;">
			<div><div style="font-weight:600;font-size:1.05rem;">${__("Partograph")}</div>
			<div class="text-muted small">${esc(started)}</div>${closed}</div>
			${add_btn}
		</div>
		${body}
		${points.length ? krcs_ptg_legend() : ""}
	`);
	$w.find(".krcs-ptg-add").on("click", () => krcs_ptg_dialog(frm));
}

function krcs_ptg_legend() {
	const item = (swatch, label) =>
		`<span style="display:inline-flex;align-items:center;gap:6px;margin-right:16px;">${swatch}<span>${label}</span></span>`;
	const svg = (inner) => `<svg width="22" height="14" viewBox="0 0 22 14" aria-hidden="true">${inner}</svg>`;
	return `<div class="small text-muted" style="margin-top:6px;line-height:2;">
		${item(svg('<path d="M6 2 L16 12 M16 2 L6 12" style="stroke:var(--blue-500);stroke-width:2"/>'), __("Cervical dilatation (cm)"))}
		${item(svg('<circle cx="11" cy="7" r="5" style="fill:none;stroke:var(--green-500);stroke-width:2"/>'), __("Descent (fifths palpable)"))}
		${item(svg('<line x1="1" y1="7" x2="21" y2="7" style="stroke:var(--orange-500);stroke-width:2.5"/>'), __("Alert line"))}
		${item(svg('<line x1="1" y1="7" x2="21" y2="7" style="stroke:var(--red-500);stroke-width:2.5"/>'), __("Action line"))}
	</div>`;
}

// ------------------------------------------------------------------------------------------------- charts

const KRCS_PTG_W = 760;
const KRCS_PTG_M = { left: 48, right: 52, top: 28, bottom: 40 };

function krcs_ptg_xmax(chart) {
	const hours = (chart.points || []).map((p) => p.hours);
	const lines = [...(chart.alert_line || []), ...(chart.action_line || [])].map((p) => p[0]);
	return Math.max(12, Math.ceil(Math.max(0, ...hours, ...lines)) + 1);
}

function krcs_ptg_x(chart) {
	const xmax = krcs_ptg_xmax(chart);
	const span = KRCS_PTG_W - KRCS_PTG_M.left - KRCS_PTG_M.right;
	return { xmax, x: (h) => KRCS_PTG_M.left + (h / xmax) * span };
}

function krcs_ptg_clock(chart, h) {
	if (!chart.origin) return "";
	const t = moment(chart.origin).add(h, "hours");
	return t.format("HH:mm");
}

function krcs_ptg_svg(chart) {
	const H = 330;
	const M = KRCS_PTG_M;
	const { xmax, x } = krcs_ptg_x(chart);
	const y = (cm) => M.top + ((10 - cm) / 10) * (H - M.top - M.bottom);
	const grid = "stroke:var(--border-color);stroke-width:1";
	const text = "fill:var(--text-muted);font-size:12px";
	const parts = [];
	for (let cm = 0; cm <= 10; cm++) {
		parts.push(`<line x1="${x(0)}" y1="${y(cm)}" x2="${x(xmax)}" y2="${y(cm)}" style="${grid}"/>`);
		parts.push(`<text x="${x(0) - 8}" y="${y(cm) + 4}" text-anchor="end" style="${text}">${cm}</text>`);
		if (cm % 2 === 0) {
			parts.push(
				`<text x="${x(xmax) + 8}" y="${y(cm) + 4}" style="${text}">${cm / 2}</text>`,
			);
		}
	}
	for (let h = 0; h <= xmax; h++) {
		parts.push(`<line x1="${x(h)}" y1="${y(10)}" x2="${x(h)}" y2="${y(0)}" style="${grid}"/>`);
		parts.push(`<text x="${x(h)}" y="${y(0) + 16}" text-anchor="middle" style="${text}">${h}</text>`);
		if (h % 2 === 0 && chart.origin) {
			parts.push(
				`<text x="${x(h)}" y="${y(0) + 31}" text-anchor="middle" style="fill:var(--text-light, var(--text-muted));font-size:10px">${krcs_ptg_clock(chart, h)}</text>`,
			);
		}
	}
	parts.push(
		`<text x="${x(0) - 8}" y="12" text-anchor="end" style="${text}">${__("cm")}</text>`,
		`<text x="${x(xmax) + 8}" y="12" style="${text}">${__("fifths")}</text>`,
	);
	const line = (pts, color, label) => {
		if (!pts || pts.length < 2) return "";
		const [a, b] = pts;
		return `<line x1="${x(a[0])}" y1="${y(a[1])}" x2="${x(b[0])}" y2="${y(b[1])}" style="stroke:var(${color});stroke-width:2.5"/>
			<text x="${x(b[0]) + 4}" y="${y(b[1]) + 14}" style="fill:var(${color});font-size:12px;font-weight:600">${label}</text>`;
	};
	parts.push(line(chart.alert_line, "--orange-500", __("Alert")));
	parts.push(line(chart.action_line, "--red-500", __("Action")));

	const dil = (chart.points || []).filter((p) => p.dilatation !== null && p.dilatation !== undefined);
	if (dil.length > 1) {
		parts.push(
			`<polyline points="${dil.map((p) => `${x(p.hours)},${y(p.dilatation)}`).join(" ")}" style="fill:none;stroke:var(--blue-500);stroke-width:1.5;stroke-opacity:0.6"/>`,
		);
	}
	dil.forEach((p) => {
		const cx = x(p.hours);
		const cy = y(p.dilatation);
		const colour = p.action_crossed ? "--red-500" : p.alert_crossed ? "--orange-500" : "--blue-500";
		parts.push(
			`<path d="M${cx - 6} ${cy - 6} L${cx + 6} ${cy + 6} M${cx + 6} ${cy - 6} L${cx - 6} ${cy + 6}" style="stroke:var(${colour});stroke-width:2.5"><title>${frappe.utils.escape_html(
				`${p.dilatation} cm, ${frappe.datetime.str_to_user(p.observed_at)}`,
			)}</title></path>`,
		);
	});
	const desc = (chart.points || []).filter((p) => p.descent !== null && p.descent !== undefined);
	if (desc.length > 1) {
		parts.push(
			`<polyline points="${desc.map((p) => `${x(p.hours)},${y(p.descent * 2)}`).join(" ")}" style="fill:none;stroke:var(--green-500);stroke-width:1.5;stroke-opacity:0.6"/>`,
		);
	}
	desc.forEach((p) => {
		parts.push(
			`<circle cx="${x(p.hours)}" cy="${y(p.descent * 2)}" r="6" style="fill:var(--card-bg, var(--fg-color));stroke:var(--green-500);stroke-width:2.5"><title>${frappe.utils.escape_html(
				`${p.descent}/5, ${frappe.datetime.str_to_user(p.observed_at)}`,
			)}</title></circle>`,
		);
	});
	return `<svg viewBox="0 0 ${KRCS_PTG_W} ${H}" style="width:100%;min-width:520px;height:auto;display:block;" role="img" aria-label="${__(
		"Cervical dilatation and descent against hours",
	)}">${parts.join("")}</svg>
	<div class="text-muted small" style="text-align:center;min-width:520px;">${__("Hours since the first observation")}</div>`;
}

function krcs_ptg_fhr_svg(chart) {
	const pts = (chart.points || []).filter((p) => p.fhr !== null && p.fhr !== undefined);
	if (!pts.length) return "";
	const H = 130;
	const M = { top: 10, bottom: 10 };
	const lo = 80;
	const hi = 200;
	const { xmax, x } = krcs_ptg_x(chart);
	const y = (v) => M.top + ((hi - Math.max(lo, Math.min(hi, v))) / (hi - lo)) * (H - M.top - M.bottom);
	const text = "fill:var(--text-muted);font-size:11px";
	const parts = [
		`<rect x="${x(0)}" y="${y(160)}" width="${x(xmax) - x(0)}" height="${y(110) - y(160)}" style="fill:var(--green-500);fill-opacity:0.12"/>`,
		`<rect x="${x(0)}" y="${y(hi)}" width="${x(xmax) - x(0)}" height="${y(lo) - y(hi)}" style="fill:none;stroke:var(--border-color)"/>`,
	];
	[110, 160].forEach((v) => {
		parts.push(
			`<line x1="${x(0)}" y1="${y(v)}" x2="${x(xmax)}" y2="${y(v)}" style="stroke:var(--text-muted);stroke-dasharray:4 4;stroke-width:1"/>`,
			`<text x="${x(0) - 8}" y="${y(v) + 4}" text-anchor="end" style="${text}">${v}</text>`,
		);
	});
	parts.push(
		`<text x="${x(xmax) + 6}" y="${y(135) + 4}" style="${text}">${__("FHR")}</text>`,
	);
	if (pts.length > 1) {
		parts.push(
			`<polyline points="${pts.map((p) => `${x(p.hours)},${y(p.fhr)}`).join(" ")}" style="fill:none;stroke:var(--text-color);stroke-width:1.5"/>`,
		);
	}
	pts.forEach((p) => {
		const bad = p.fhr < 110 || p.fhr > 160;
		parts.push(
			`<circle cx="${x(p.hours)}" cy="${y(p.fhr)}" r="4.5" style="fill:var(${bad ? "--red-500" : "--text-color"})"><title>${frappe.utils.escape_html(
				`${p.fhr}/min, ${frappe.datetime.str_to_user(p.observed_at)}`,
			)}</title></circle>`,
		);
	});
	return `<div class="text-muted small" style="margin-top:12px;min-width:520px;">${__("Foetal heart rate (/min); shaded band 110-160")}</div>
	<svg viewBox="0 0 ${KRCS_PTG_W} ${H}" style="width:100%;min-width:520px;height:auto;display:block;" role="img" aria-label="${__(
		"Foetal heart rate against hours",
	)}">${parts.join("")}</svg>`;
}

// ------------------------------------------------------------------------------------------------- dialog

function krcs_ptg_last(frm, fieldname, unit) {
	const last = ((frm.krcs_chart || {}).last_values || {})[fieldname];
	if (!last) return "";
	return __("Last: {0}{1} at {2}", [
		last.value,
		unit || "",
		frappe.datetime.str_to_user(last.observed_at),
	]);
}

function krcs_ptg_dialog(frm) {
	krcs_ptg_style();
	const opts = (list) => [""].concat(list);
	const last = (f, unit) => krcs_ptg_last(frm, f, unit);
	const d = new frappe.ui.Dialog({
		title: __("Add hourly observation"),
		size: "large",
		fields: [
			{
				fieldname: "observed_at",
				fieldtype: "Datetime",
				label: __("Time"),
				reqd: 1,
				default: frappe.datetime.now_datetime(),
			},
			{ fieldtype: "Section Break", label: __("Baby") },
			{
				fieldname: "foetal_heart_rate",
				fieldtype: "Int",
				label: __("Foetal heart rate (/min)"),
				description: last("foetal_heart_rate"),
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "liquor",
				fieldtype: "Select",
				label: __("Liquor"),
				options: opts(KRCS_PTG_OPTIONS.liquor),
				description: __("I intact, C clear, M meconium, B blood, A absent"),
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "moulding",
				fieldtype: "Select",
				label: __("Moulding"),
				options: opts(KRCS_PTG_OPTIONS.moulding),
			},
			{ fieldtype: "Section Break", label: __("Labour") },
			{
				fieldname: "cervical_dilatation_cm",
				fieldtype: "Float",
				label: __("Cervical dilatation (cm)"),
				description: last("cervical_dilatation_cm", " cm"),
			},
			{
				fieldname: "descent_fifths",
				fieldtype: "Int",
				label: __("Descent (fifths palpable, 0-5)"),
				description: last("descent_fifths", "/5"),
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "contractions_per_10_min",
				fieldtype: "Int",
				label: __("Contractions per 10 min"),
				description: last("contractions_per_10_min"),
			},
			{
				fieldname: "contraction_duration",
				fieldtype: "Select",
				label: __("Contraction duration"),
				options: opts(KRCS_PTG_OPTIONS.contraction_duration),
			},
			{ fieldtype: "Section Break", label: __("Mother") },
			{ fieldname: "pulse", fieldtype: "Int", label: __("Pulse (/min)"), description: last("pulse") },
			{
				fieldname: "temperature",
				fieldtype: "Float",
				label: __("Temperature (°C)"),
				description: last("temperature", " °C"),
			},
			{ fieldtype: "Column Break" },
			{ fieldname: "bp_systolic", fieldtype: "Int", label: __("BP systolic (mmHg)") },
			{ fieldname: "bp_diastolic", fieldtype: "Int", label: __("BP diastolic (mmHg)") },
			{ fieldtype: "Column Break" },
			{ fieldname: "respiratory_rate", fieldtype: "Int", label: __("Respiration (/min)") },
			{
				fieldtype: "Section Break",
				label: __("Oxytocin, drugs and IV fluids"),
				collapsible: 1,
			},
			{
				fieldname: "oxytocin_u_per_l",
				fieldtype: "Float",
				label: __("Oxytocin (U/L)"),
				description: last("oxytocin_u_per_l"),
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "oxytocin_drops_per_min",
				fieldtype: "Int",
				label: __("Oxytocin (drops/min)"),
				description: last("oxytocin_drops_per_min"),
			},
			{ fieldtype: "Section Break" },
			{ fieldname: "drugs_iv_fluids", fieldtype: "Small Text", label: __("Drugs and IV fluids") },
			{ fieldtype: "Section Break", label: __("Urine"), collapsible: 1 },
			{
				fieldname: "urine_protein",
				fieldtype: "Select",
				label: __("Protein"),
				options: opts(KRCS_PTG_OPTIONS.urine),
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "urine_acetone",
				fieldtype: "Select",
				label: __("Acetone"),
				options: opts(KRCS_PTG_OPTIONS.urine),
			},
			{ fieldtype: "Column Break" },
			{ fieldname: "urine_volume_ml", fieldtype: "Int", label: __("Volume (mL)") },
			{ fieldtype: "Section Break" },
			{
				fieldname: "note",
				fieldtype: "Small Text",
				label: __("Note"),
				description: __(
					"Needed when this row corrects an earlier one (for example a lower dilatation).",
				),
			},
		],
		primary_action_label: __("Save observation"),
		primary_action(values) {
			const clean = {};
			Object.keys(values || {}).forEach((k) => {
				const v = values[k];
				if (v !== null && v !== undefined && v !== "") clean[k] = v;
			});
			d.get_primary_btn().prop("disabled", true);
			frappe
				.call({
					method: KRCS_PTG_ADD,
					args: { partograph: frm.doc.name, values: clean },
					freeze: true,
					freeze_message: __("Saving..."),
				})
				.then((r) => {
					d.hide();
					const res = r.message || {};
					frappe.show_alert({
						message: (res.warnings || []).length
							? __("Observation saved. Check the warnings.")
							: __("Observation saved."),
						indicator: (res.warnings || []).length ? "orange" : "green",
					});
					frm.reload_doc();
				})
				.always(() => d.get_primary_btn().prop("disabled", false));
		},
	});
	d.$wrapper.addClass("krcs-ptg-dialog");
	d.fields_dict.cervical_dilatation_cm.$input.attr("inputmode", "decimal");
	d.fields_dict.temperature.$input.attr("inputmode", "decimal");
	d.fields_dict.oxytocin_u_per_l.$input.attr("inputmode", "decimal");
	d.show();
}

function krcs_ptg_style() {
	if (document.getElementById("krcs-ptg-style")) return;
	const style = document.createElement("style");
	style.id = "krcs-ptg-style";
	style.textContent = `
		.krcs-ptg-dialog .form-control, .krcs-ptg-dialog select { min-height: 48px; font-size: 1.15rem; }
		.krcs-ptg-dialog textarea.form-control { min-height: 72px; }
		.krcs-ptg-dialog .control-label { font-size: 0.95rem; }
		.krcs-ptg-dialog .modal-footer .btn { min-height: 48px; font-size: 1.05rem; padding: 8px 20px; }
		.krcs-ptg-dialog .section-head { font-size: 1rem; }
	`;
	document.head.appendChild(style);
}
