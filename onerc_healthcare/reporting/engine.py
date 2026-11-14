# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MOH report engine: evaluate a definition for a facility and period, and lay the result out.

- `build_context(run)`: the `SourceContext` of a run (report, period, facility, company, age bands).
- `evaluate_report(report, ctx)`: groups the report's **Available** elements by source type and calls
  each source's evaluator once (`registry.get_evaluator`). Gap elements are never evaluated. A source
  whose evaluator module or function does not exist yet becomes a "Source not available" exception and
  its elements stay blank (never a crash, never a silent zero). Evaluator output is checked: a cell for
  another element, a cell missing a dimension the element is disaggregated by, or a negative or
  non-integer value is dropped with an "Engine" exception.
- Totals are added for disaggregated elements: the element total ``(code, "", "")``, and for "Age Band
  and Sex" also each band's total ``(code, band, "")`` and each sex's total ``(code, "", sex)``. A cell
  with a blank dimension is therefore always a total.
- `build_snapshot(...)`: everything the exports and the API need, frozen at generation (no patient data,
  no exceptions: the Head of Operations reads the snapshot).
- `build_layout(snapshot)`: sections, column blocks and rows shared by the XLSX, the PDF and the form
  preview.

Fingerprints: every dotted path in the ``krcs_report_fingerprints`` hook is called as
``fn(report: str, ctx: SourceContext) -> dict`` and merged into ``snapshot["fingerprints"]`` (R2 adds
its mapping fingerprint there). The engine adds ``definition``: the hash of the definition used.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

import frappe
from frappe import _
from frappe.utils import cint, cstr, getdate, now_datetime

from onerc_healthcare.constants import MOH_REPORT_DOCTYPE, MOH_REPORT_STATUSES
from onerc_healthcare.reporting import registry
from onerc_healthcare.reporting.ages import UNKNOWN_BAND, AgeBand
from onerc_healthcare.reporting.definitions import canonical, definition_hash, parse_json
from onerc_healthcare.reporting.period import period_label
from onerc_healthcare.reporting.registry import (
	AVAILABILITY_AVAILABLE,
	AVAILABILITY_GAP,
	DISAGGREGATION_AGE,
	DISAGGREGATION_AGE_SEX,
	DISAGGREGATION_NONE,
	DISAGGREGATION_SEX,
	CellKey,
	ElementSpec,
	SourceContext,
)

SNAPSHOT_SCHEMA = 1
REPORT_STATUS_PROVISIONAL, REPORT_STATUS_CONFIRMED, REPORT_STATUS_BLOCKED = MOH_REPORT_STATUSES
FINGERPRINT_HOOK = "krcs_report_fingerprints"
DEFAULT_SEXES = ("Male", "Female")
TOTAL_LABEL = "Total"
ALL_LABEL = "All"
VALUE_LABEL = "Value"

EXCEPTION_SOURCE_UNAVAILABLE = "Source not available"
EXCEPTION_ENGINE = "Engine"


@dataclass
class ReportResult:
	cells: dict[CellKey, int] = field(default_factory=dict)  # detail cells and totals
	unavailable: dict[str, str] = field(default_factory=dict)  # element code -> why it is blank


# --- definition helpers -------------------------------------------------------------------------------


def get_report(report) -> frappe.model.document.Document:
	return report if not isinstance(report, str) else frappe.get_doc(MOH_REPORT_DOCTYPE, report)


def age_bands_of(report) -> tuple[AgeBand, ...]:
	return tuple(
		AgeBand(cstr(b.label), cint(b.min_months), cint(b.max_months) or None)
		for b in get_report(report).get("age_bands") or []
	)


def sorted_elements(report) -> list:
	"""The report's element rows in display order (sort order, then table order)."""
	rows = list(get_report(report).get("elements") or [])
	return sorted(rows, key=lambda r: (cint(r.sort_order), cint(r.idx)))


def element_spec(row) -> ElementSpec:
	return ElementSpec(
		code=cstr(row.code).strip(),
		label=cstr(row.label),
		source_type=cstr(row.source_type),
		rule=parse_json(row.rule) or {},
		disaggregation=row.disaggregation or DISAGGREGATION_NONE,
	)


def element_specs(report) -> list[ElementSpec]:
	"""Specs of the Available elements (the ones evaluated)."""
	return [
		element_spec(r)
		for r in sorted_elements(report)
		if (r.availability or AVAILABILITY_AVAILABLE) == AVAILABILITY_AVAILABLE
	]


def build_context(run, report=None) -> SourceContext:
	report = get_report(report or run.report)
	return SourceContext(
		report=report.name,
		from_date=getdate(run.from_date),
		to_date=getdate(run.to_date),
		facility=run.facility or None,
		company=run.company or None,
		age_bands=age_bands_of(report),
	)


# --- evaluation ---------------------------------------------------------------------------------------


def _by_age(spec: ElementSpec) -> bool:
	return spec.disaggregation in (DISAGGREGATION_AGE, DISAGGREGATION_AGE_SEX)


def _by_sex(spec: ElementSpec) -> bool:
	return spec.disaggregation in (DISAGGREGATION_SEX, DISAGGREGATION_AGE_SEX)


def _resolve_evaluator(source_type: str):
	"""The evaluator, or (None, reason) when its module or function does not exist (yet)."""
	try:
		return registry.get_evaluator(source_type), None
	except (ImportError, AttributeError) as e:
		return None, f"{registry.SOURCE_TYPES.get(source_type)}: {e}"
	except frappe.ValidationError as e:
		return None, cstr(e) or _("Unknown MOH data source {0}.").format(source_type)


def _check_cell(spec: ElementSpec, key, value) -> str | None:
	"""Why an evaluator cell is refused, or None."""
	if not isinstance(key, tuple) or len(key) != 3:
		return f"cell key {key!r} is not (element, band, sex)"
	_code, band, sex = key
	if bool(band) != _by_age(spec) or bool(sex) != _by_sex(spec):
		return f"cell {key!r} does not match the element's disaggregation ({spec.disaggregation})"
	if isinstance(value, bool) or not isinstance(value, int) or value < 0:
		return f"cell {key!r} has value {value!r}; counts are whole numbers of 0 or more"
	return None


def evaluate_report(report, ctx: SourceContext) -> ReportResult:
	"""Evaluate every Available element of `report` for `ctx`, then add totals (module docstring)."""
	specs = element_specs(report)
	result = ReportResult()
	groups: dict[str, list[ElementSpec]] = defaultdict(list)
	for spec in specs:
		groups[spec.source_type].append(spec)

	detail: dict[CellKey, int] = {}
	for source_type, group in groups.items():
		evaluator, reason = _resolve_evaluator(source_type)
		if evaluator is None:
			for spec in group:
				result.unavailable[spec.code] = _("Source {0} is not available yet.").format(source_type)
			ctx.add_exception(
				EXCEPTION_SOURCE_UNAVAILABLE,
				_("{0} element(s) not computed: {1}").format(len(group), reason),
				element=", ".join(s.code for s in group)[:140],
			)
			continue
		by_code = {s.code: s for s in group}
		for key, value in (evaluator(ctx, group) or {}).items():
			code = key[0] if isinstance(key, tuple) and key else None
			spec = by_code.get(code)
			problem = (
				f"{source_type} returned a cell for element {code!r}, which it was not asked for"
				if spec is None
				else _check_cell(spec, key, value)
			)
			if problem:
				ctx.add_exception(EXCEPTION_ENGINE, problem, element=cstr(code)[:140] or None)
				continue
			if value:
				detail[key] = detail.get(key, 0) + value

	result.cells = add_totals(detail, specs)
	return result


def add_totals(detail: dict[CellKey, int], specs: list[ElementSpec]) -> dict[CellKey, int]:
	"""`detail` plus totals for disaggregated elements (module docstring)."""
	cells = dict(detail)
	for spec in specs:
		if spec.disaggregation == DISAGGREGATION_NONE:
			continue
		mine = {k: v for k, v in detail.items() if k[0] == spec.code}
		cells[(spec.code, "", "")] = sum(mine.values())
		if spec.disaggregation == DISAGGREGATION_AGE_SEX:
			for (_code, band, sex), value in mine.items():
				cells[(spec.code, band, "")] = cells.get((spec.code, band, ""), 0) + value
				cells[(spec.code, "", sex)] = cells.get((spec.code, "", sex), 0) + value
	return cells


# --- snapshot -----------------------------------------------------------------------------------------


def facility_info(facility: str | None) -> dict:
	if not facility:
		return {"name": None, "label": _("All facilities"), "code": None, "county": None, "sub_county": None}
	meta = frappe.get_meta("Healthcare Service Unit")
	fields = ["healthcare_service_unit_name"] + [
		f for f in ("krcs_facility_code", "krcs_county", "krcs_sub_county") if meta.has_field(f)
	]
	row = frappe.db.get_value("Healthcare Service Unit", facility, fields, as_dict=True) or {}
	return {
		"name": facility,
		"label": row.get("healthcare_service_unit_name") or facility,
		"code": row.get("krcs_facility_code"),
		"county": row.get("krcs_county"),
		"sub_county": row.get("krcs_sub_county"),
	}


def fingerprints(report, ctx: SourceContext) -> dict:
	out = {"definition": definition_hash(get_report(report))}
	for path in frappe.get_hooks(FINGERPRINT_HOOK):
		out.update(frappe.get_attr(path)(ctx.report, ctx) or {})
	return out


def build_snapshot(run, report, ctx: SourceContext, result: ReportResult) -> dict:
	report = get_report(report)
	definition = canonical(report)
	elements = []
	for row in sorted_elements(report):
		el = {k: v for k, v in canonical({"elements": [row]})["elements"][0].items() if k != "rule"}
		el["unavailable"] = result.unavailable.get(el["code"])
		elements.append(el)
	return {
		"schema": SNAPSHOT_SCHEMA,
		"report": {
			"name": report.name,
			"title": definition["title"],
			"status": definition["status"],
			"version": definition["version"],
			"note": definition["note"],
		},
		"period": {
			"from_date": str(ctx.from_date),
			"to_date": str(ctx.to_date),
			"period_type": run.period_type,
			"label": period_label(ctx.from_date, ctx.to_date, run.period_type),
		},
		"facility": facility_info(ctx.facility),
		"company": ctx.company,
		"age_bands": [
			{"label": b.label, "min_months": b.min_months, "max_months": b.max_months} for b in ctx.age_bands
		],
		"elements": elements,
		"cells": sorted([[k[0], k[1], k[2], v] for k, v in result.cells.items()]),
		"fingerprints": fingerprints(report, ctx),
		"generated_at": str(now_datetime()),
	}


def snapshot_of(run) -> dict | None:
	return parse_json(run.get("snapshot"))


def cells_of(snapshot: dict) -> dict[CellKey, int]:
	return {(c[0], c[1], c[2]): c[3] for c in snapshot.get("cells") or []}


# --- layout (exports and preview) ---------------------------------------------------------------------


def _bands_for(snapshot: dict, codes: set[str], cells: dict) -> list[str]:
	labels = [b["label"] for b in snapshot.get("age_bands") or []]
	extra = sorted({k[1] for k in cells if k[0] in codes and k[1] and k[1] not in labels} - {UNKNOWN_BAND})
	labels += extra
	if any(k[0] in codes and k[1] == UNKNOWN_BAND for k in cells):
		labels.append(UNKNOWN_BAND)
	return labels


def _sexes_for(codes: set[str], cells: dict) -> list[str]:
	extra = sorted({k[2] for k in cells if k[0] in codes and k[2] and k[2] not in DEFAULT_SEXES})
	return list(DEFAULT_SEXES) + extra


def _columns(disaggregation: str, bands: list[str], sexes: list[str]) -> list[tuple[str, str, str, str]]:
	"""(header, sub-header, band, sex) per value column; band/sex "" is a total over that dimension."""
	if disaggregation == DISAGGREGATION_AGE:
		return [(b, "", b, "") for b in bands] + [(TOTAL_LABEL, "", "", "")]
	if disaggregation == DISAGGREGATION_SEX:
		return [(s, "", "", s) for s in sexes] + [(TOTAL_LABEL, "", "", "")]
	if disaggregation == DISAGGREGATION_AGE_SEX:
		cols = [(b, s, b, s) for b in bands for s in sexes]
		cols += [(TOTAL_LABEL, s, "", s) for s in sexes] + [(TOTAL_LABEL, ALL_LABEL, "", "")]
		return cols
	return [(VALUE_LABEL, "", "", "")]


def element_note(el: dict) -> str:
	if el.get("availability") == AVAILABILITY_GAP:
		return _("Gap: {0}").format(el.get("gap_note")) if el.get("gap_note") else _("Gap")
	if el.get("unavailable"):
		return _("Not computed: {0}").format(el["unavailable"])
	return ""


def build_layout(snapshot: dict) -> list[dict]:
	"""Sections in order, each a list of blocks (consecutive elements sharing a disaggregation):

	``[{"title", "blocks": [{"disaggregation", "columns": [(header, sub, band, sex)], "has_sub",
	"rows": [{"code", "label", "values": [int | None], "note", "blank"}]}]}]``
	"""
	cells = cells_of(snapshot)
	sections: list[dict] = []
	for el in snapshot.get("elements") or []:
		title = el.get("section") or ""
		section = next((s for s in sections if s["title"] == title), None)
		if section is None:
			section = {"title": title, "blocks": []}
			sections.append(section)
		blocks = section["blocks"]
		if not blocks or blocks[-1]["disaggregation"] != el["disaggregation"]:
			blocks.append({"disaggregation": el["disaggregation"], "elements": []})
		blocks[-1]["elements"].append(el)

	for section in sections:
		for block in section["blocks"]:
			codes = {e["code"] for e in block["elements"]}
			by_age = block["disaggregation"] in (DISAGGREGATION_AGE, DISAGGREGATION_AGE_SEX)
			by_sex = block["disaggregation"] in (DISAGGREGATION_SEX, DISAGGREGATION_AGE_SEX)
			bands = _bands_for(snapshot, codes, cells) if by_age else []
			sexes = _sexes_for(codes, cells) if by_sex else []
			block["columns"] = _columns(block["disaggregation"], bands, sexes)
			block["has_sub"] = block["disaggregation"] == DISAGGREGATION_AGE_SEX
			rows = []
			for el in block.pop("elements"):
				blank = el.get("availability") == AVAILABILITY_GAP or bool(el.get("unavailable"))
				values = [
					None if blank else cells.get((el["code"], band, sex), 0)
					for _h, _s, band, sex in block["columns"]
				]
				rows.append(
					{
						"code": el["code"],
						"label": el["label"],
						"values": values,
						"note": element_note(el),
						"blank": blank,
					}
				)
			block["rows"] = rows
	return sections


def header_groups(columns: list[tuple]) -> list[tuple[str, int]]:
	"""Consecutive equal headers merged: [(header, span)]."""
	groups: list[tuple[str, int]] = []
	for header, *_rest in columns:
		if groups and groups[-1][0] == header:
			groups[-1] = (header, groups[-1][1] + 1)
		else:
			groups.append((header, 1))
	return groups


def is_provisional(snapshot: dict) -> bool:
	return (snapshot.get("report") or {}).get("status") != REPORT_STATUS_CONFIRMED


def dumps(snapshot: dict) -> str:
	return json.dumps(snapshot, separators=(",", ":"), default=str)
