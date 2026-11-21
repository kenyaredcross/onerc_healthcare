// Copyright (c) 2026, Lennox Mwita and contributors
// For license information, please see license.txt
// Weight-for-age growth chart (CON-17; docs/MCH_CONTRACT.md §8). Inline SVG, no external library, styled like
// the partograph chart (Frappe CSS variables, so it follows the light and dark themes).
//
//   onerc_mch.render_growth_chart(wrapper, data)
//
// `wrapper`: a DOM element or a jQuery object. `data`: the result of onerc_healthcare.mch.growth.chart_data
// {sex, dob, dob_estimated, points: [{age_days, weight_kg, measured_on, name}], curves: {"-3": [[day, kg]], ...}}.
// The WHO lines are -3 and +3 (red), -2 and +2 (orange) and the median (green); the band between -2 and +2
// is shaded. A point below -2 is orange, below -3 red.

window.onerc_mch = window.onerc_mch || {};

(function () {
	const DAYS_PER_MONTH = 30.4375;
	const W = 760;
	const H = 360;
	const M = { left: 52, right: 44, top: 20, bottom: 46 };
	const LINES = [
		{ key: "+3", colour: "--red-500", dash: "6 4" },
		{ key: "+2", colour: "--orange-500", dash: "" },
		{ key: "0", colour: "--green-500", dash: "" },
		{ key: "-2", colour: "--orange-500", dash: "" },
		{ key: "-3", colour: "--red-500", dash: "6 4" },
	];

	const esc = (s) => frappe.utils.escape_html(s === null || s === undefined ? "" : String(s));

	// Linear interpolation of a curve [[day, kg], ...] at `day`.
	function at(curve, day) {
		if (!curve || !curve.length) return null;
		if (day <= curve[0][0]) return curve[0][1];
		for (let i = 1; i < curve.length; i++) {
			const [x1, y1] = curve[i];
			if (day <= x1) {
				const [x0, y0] = curve[i - 1];
				return y0 + ((y1 - y0) * (day - x0)) / (x1 - x0 || 1);
			}
		}
		return curve[curve.length - 1][1];
	}

	function band(point, curves) {
		const m3 = at(curves["-3"], point.age_days);
		const m2 = at(curves["-2"], point.age_days);
		if (m3 !== null && point.weight_kg < m3) return { colour: "--red-500", label: __("below -3 SD") };
		if (m2 !== null && point.weight_kg < m2) return { colour: "--orange-500", label: __("below -2 SD") };
		return { colour: "--blue-500", label: "" };
	}

	function svg(data) {
		const curves = data.curves || {};
		const points = (data.points || []).filter((p) => p.weight_kg > 0 && p.age_days >= 0);
		const lastDay = Math.max(0, ...points.map((p) => p.age_days));
		// x: whole months, at least 12, 6 months beyond the last point, at most 60.
		const maxMonths = Math.min(60, Math.max(12, Math.ceil((lastDay / DAYS_PER_MONTH + 6) / 6) * 6));
		const maxDay = Math.min(1856, Math.round(maxMonths * DAYS_PER_MONTH));
		const inRange = (curve) => (curve || []).filter((c) => c[0] <= maxDay + 30);
		const upper = [...inRange(curves["+3"]).map((c) => c[1]), ...points.map((p) => p.weight_kg)];
		const lower = [...inRange(curves["-3"]).map((c) => c[1]), ...points.map((p) => p.weight_kg)];
		const yMax = Math.ceil(Math.max(5, ...upper) + 0.5);
		const yMin = Math.max(0, Math.floor(Math.min(...lower, yMax) - 0.5));
		const x = (day) => M.left + (Math.min(day, maxDay) / maxDay) * (W - M.left - M.right);
		const y = (kg) => M.top + ((yMax - kg) / (yMax - yMin || 1)) * (H - M.top - M.bottom);
		const grid = "stroke:var(--border-color);stroke-width:1";
		const text = "fill:var(--text-muted);font-size:12px";
		const parts = [];

		// grid: kg every 1 (labels every 2 when the range is wide), months every 3 (labels every 6 beyond 24)
		const kgStep = yMax - yMin > 14 ? 2 : 1;
		for (let kg = yMin; kg <= yMax; kg++) {
			parts.push(`<line x1="${x(0)}" y1="${y(kg)}" x2="${x(maxDay)}" y2="${y(kg)}" style="${grid};stroke-opacity:${kg % kgStep ? 0.4 : 1}"/>`);
			if (kg % kgStep === 0) {
				parts.push(`<text x="${x(0) - 8}" y="${y(kg) + 4}" text-anchor="end" style="${text}">${kg}</text>`);
			}
		}
		const monthLabel = maxMonths > 24 ? 6 : 3;
		for (let m = 0; m <= maxMonths; m += 3) {
			const day = Math.min(maxDay, m * DAYS_PER_MONTH);
			parts.push(`<line x1="${x(day)}" y1="${y(yMax)}" x2="${x(day)}" y2="${y(yMin)}" style="${grid}"/>`);
			if (m % monthLabel === 0) {
				parts.push(`<text x="${x(day)}" y="${y(yMin) + 16}" text-anchor="middle" style="${text}">${m}</text>`);
			}
		}
		parts.push(`<text x="${x(0) - 8}" y="${M.top - 6}" text-anchor="end" style="${text}">${__("kg")}</text>`);

		// -2 to +2 band
		const p2 = inRange(curves["+2"]);
		const m2 = inRange(curves["-2"]);
		if (p2.length && m2.length) {
			const top = p2.map((c) => `${x(c[0])},${y(c[1])}`);
			const bottom = m2
				.slice()
				.reverse()
				.map((c) => `${x(c[0])},${y(c[1])}`);
			parts.push(`<polygon points="${top.concat(bottom).join(" ")}" style="fill:var(--green-500);fill-opacity:0.08;stroke:none"/>`);
		}
		LINES.forEach((line) => {
			const curve = inRange(curves[line.key]);
			if (curve.length < 2) return;
			parts.push(
				`<polyline points="${curve.map((c) => `${x(c[0])},${y(c[1])}`).join(" ")}" style="fill:none;stroke:var(${line.colour});stroke-width:1.5;${
					line.dash ? `stroke-dasharray:${line.dash};` : ""
				}"/>`,
			);
			const end = at(curve, maxDay);
			parts.push(`<text x="${x(maxDay) + 6}" y="${y(end) + 4}" style="fill:var(${line.colour});font-size:11px;font-weight:600">${line.key}</text>`);
		});

		// the child's measurements
		if (points.length > 1) {
			parts.push(
				`<polyline points="${points.map((p) => `${x(p.age_days)},${y(p.weight_kg)}`).join(" ")}" style="fill:none;stroke:var(--text-color);stroke-width:1.5;stroke-opacity:0.7"/>`,
			);
		}
		points.forEach((p) => {
			const b = band(p, curves);
			const tip = `${p.weight_kg} kg, ${frappe.datetime.str_to_user(p.measured_on)}${b.label ? ", " + b.label : ""}`;
			parts.push(
				`<circle cx="${x(p.age_days)}" cy="${y(p.weight_kg)}" r="5" style="fill:var(${b.colour});stroke:var(--card-bg, var(--fg-color));stroke-width:1.5"><title>${esc(tip)}</title></circle>`,
			);
		});
		return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;min-width:520px;height:auto;display:block;" role="img" aria-label="${esc(
			__("Weight-for-age against age in months"),
		)}">${parts.join("")}</svg>
		<div class="text-muted small" style="text-align:center;min-width:520px;">${__("Age (completed months)")}</div>`;
	}

	function legend() {
		const item = (swatch, label) =>
			`<span style="display:inline-flex;align-items:center;gap:6px;margin-right:16px;">${swatch}<span>${label}</span></span>`;
		const line = (colour, dash) =>
			`<svg width="22" height="14" viewBox="0 0 22 14" aria-hidden="true"><line x1="1" y1="7" x2="21" y2="7" style="stroke:var(${colour});stroke-width:2;${
				dash ? "stroke-dasharray:4 3;" : ""
			}"/></svg>`;
		const dot = (colour) =>
			`<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="7" r="5" style="fill:var(${colour})"/></svg>`;
		return `<div class="small text-muted" style="margin-top:6px;line-height:2;">
			${item(line("--green-500"), __("Median"))}
			${item(line("--orange-500"), __("-2 / +2 SD"))}
			${item(line("--red-500", true), __("-3 / +3 SD"))}
			${item(dot("--blue-500"), __("Weight"))}
			${item(dot("--orange-500"), __("Below -2 SD"))}
			${item(dot("--red-500"), __("Below -3 SD"))}
		</div>`;
	}

	onerc_mch.render_growth_chart = function (wrapper, data) {
		const el = wrapper && wrapper.jquery ? wrapper.get(0) : wrapper;
		if (!el) return;
		data = data || {};
		const sex = { Male: __("boys"), Female: __("girls") }[data.sex];
		const title = sex
			? __("Weight-for-age ({0}), WHO Child Growth Standards", [sex])
			: __("Weight-for-age, WHO Child Growth Standards");
		const notes = [];
		if (data.dob_estimated) {
			notes.push(__("Date of birth is estimated: the ages on this chart are approximate."));
		}
		let body;
		if (!data.curves || !Object.keys(data.curves).length) {
			body = `<div class="text-muted" style="padding:16px 0;">${__("The WHO chart needs the child's sex (Male or Female) on the Patient.")}</div>`;
		} else if (!(data.points || []).length) {
			body = `<div class="text-muted" style="padding:16px 0;">${__("No submitted weight measurements yet.")}</div>`;
		} else {
			body = `<div style="overflow-x:auto;-webkit-overflow-scrolling:touch;">${svg(data)}</div>${legend()}`;
		}
		el.innerHTML = `<div class="krcs-growth-chart" style="margin-top:12px;">
			<div style="font-weight:600;font-size:1.05rem;">${esc(title)}</div>
			${notes.map((n) => `<div class="text-warning small">${esc(n)}</div>`).join("")}
			${body}
		</div>`;
	};
})();
