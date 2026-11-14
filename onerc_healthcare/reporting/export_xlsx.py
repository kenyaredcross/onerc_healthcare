# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Generic Excel export of a MOH report run (FR-30), built from the run's snapshot only.

The official form layouts are not available (OI-5), so every report uses one generic layout:

- a PROVISIONAL banner unless the definition was Confirmed when the run was generated;
- the header: report, facility name and code, county, sub-county, period, report version, run;
- each section, its elements in sort order. Disaggregated elements form a table with age bands as
  columns (sex as sub-columns where used) and totals; Gap elements are blank with their note;
- the narrative, and the compiled-by block (name, designation, date, signature line).

No patient data and no exceptions are exported. Files are private attachments on the run.
"""

from __future__ import annotations

import io
import re

import frappe
from frappe import _
from frappe.utils import formatdate, get_datetime, today

from onerc_healthcare.reporting import engine

PROVISIONAL_BANNER = "PROVISIONAL — HIS Officer to confirm (OI-5)"
SIGNATURE_LINE = "______________________________"
CODE_COLUMN = "Code"
ELEMENT_COLUMN = "Element"
NOTE_COLUMN = "Note"


def full_name(user: str | None) -> str | None:
	if not user:
		return None
	return frappe.db.get_value("User", user, "full_name") or user


def export_context(run, snapshot: dict | None = None) -> dict:
	"""Everything both exports show, from the snapshot plus the run's compiled-by and narrative."""
	snapshot = snapshot or engine.snapshot_of(run) or {}
	report = snapshot.get("report") or {}
	facility = snapshot.get("facility") or {}
	period = snapshot.get("period") or {}
	provisional = engine.is_provisional(snapshot)
	generated = snapshot.get("generated_at")
	note = (report.get("note") or "").strip()
	return {
		"provisional": provisional,
		"banner": PROVISIONAL_BANNER if provisional else None,
		"banner_note": note if provisional and note and note != PROVISIONAL_BANNER else None,
		"title": f"{report.get('name') or ''} — {report.get('title') or ''}".strip(" —"),
		"header": [
			(_("Facility"), facility.get("label")),
			(_("Facility code"), facility.get("code")),
			(_("County"), facility.get("county")),
			(_("Sub-county"), facility.get("sub_county")),
			(
				_("Period"),
				f"{period.get('label') or ''} ({period.get('from_date')} to {period.get('to_date')})",
			),
			(_("Report version"), report.get("version")),
			(_("Report status"), report.get("status")),
			(_("Run"), f"{run.name} ({run.status})"),
			(_("Generated"), str(get_datetime(generated).replace(microsecond=0)) if generated else None),
		],
		"sections": engine.build_layout(snapshot),
		"narrative": (run.get("narrative") or "").strip() or None,
		"compiled_by": full_name(run.get("compiled_by")),
		"designation": run.get("compiled_by_designation"),
		"date": formatdate(today()),
		"signature": SIGNATURE_LINE,
	}


def file_base_name(run, snapshot: dict | None = None) -> str:
	snapshot = snapshot or engine.snapshot_of(run) or {}
	report = (snapshot.get("report") or {}).get("name") or run.report
	label = (snapshot.get("period") or {}).get("label") or run.period_label or ""
	stamp = get_datetime(snapshot.get("generated_at") or None).strftime("%Y%m%d-%H%M%S")
	raw = f"{report} {label} {run.name} {stamp}"
	return re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-")


def attach_file(run, fieldname: str, file_name: str, content: bytes) -> str:
	"""Save `content` as a private File attached to `run` and set `run.<fieldname>` to its URL."""
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"attached_to_doctype": run.doctype,
			"attached_to_name": run.name,
			"attached_to_field": fieldname,
			"is_private": 1,
			"content": content,
		}
	)
	frappe.flags.krcs_moh_exporting = True  # reporting.permissions lets the export code attach
	try:
		file_doc.insert(ignore_permissions=True)
	finally:
		frappe.flags.krcs_moh_exporting = False
	run.set(fieldname, file_doc.file_url)
	return file_doc.file_url


def build_xlsx(run, snapshot: dict | None = None) -> bytes:
	from openpyxl import Workbook
	from openpyxl.styles import Alignment, Font, PatternFill
	from openpyxl.utils import get_column_letter

	ctx = export_context(run, snapshot)
	wb = Workbook()
	ws = wb.active
	ws.title = (re.sub(r"[\[\]:*?/\\]", "", (ctx["title"].split(" — ")[0] or "Report")) or "Report")[:31]
	bold = Font(bold=True)
	header_fill = PatternFill("solid", fgColor="DDDDDD")
	row = 1

	if ctx["banner"]:
		ws.cell(row, 1, ctx["banner"]).font = Font(bold=True, color="FFFFFF", size=12)
		ws.cell(row, 1).fill = PatternFill("solid", fgColor="C0392B")
		ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
		row += 1
		if ctx["banner_note"]:
			ws.cell(row, 1, ctx["banner_note"]).font = Font(italic=True, color="C0392B")
			row += 1
	ws.cell(row, 1, ctx["title"]).font = Font(bold=True, size=14)
	row += 1
	for label, value in ctx["header"]:
		ws.cell(row, 1, label).font = bold
		ws.cell(row, 2, value or "")
		row += 1
	row += 1

	max_col = 3
	for section in ctx["sections"]:
		if section["title"]:
			ws.cell(row, 1, section["title"]).font = Font(bold=True, size=12)
			row += 1
		for block in section["blocks"]:
			columns = block["columns"]
			last = 2 + len(columns) + 1
			max_col = max(max_col, last)
			# header row(s)
			for c, text in ((1, CODE_COLUMN), (2, ELEMENT_COLUMN), (last, NOTE_COLUMN)):
				ws.cell(row, c, _(text)).font = bold
				ws.cell(row, c).fill = header_fill
			col = 3
			for header, span in engine.header_groups(columns):
				ws.cell(row, col, header).font = bold
				ws.cell(row, col).fill = header_fill
				ws.cell(row, col).alignment = Alignment(horizontal="center")
				if span > 1:
					ws.merge_cells(start_row=row, start_column=col, end_row=row, end_column=col + span - 1)
				col += span
			if block["has_sub"]:
				row += 1
				for i, (_h, sub, _b, _s) in enumerate(columns):
					ws.cell(row, 3 + i, sub).font = bold
					ws.cell(row, 3 + i).fill = header_fill
			row += 1
			for line in block["rows"]:
				ws.cell(row, 1, line["code"])
				ws.cell(row, 2, line["label"])
				for i, value in enumerate(line["values"]):
					if value is not None:
						ws.cell(row, 3 + i, value)
				if line["note"]:
					ws.cell(row, last, line["note"]).font = Font(italic=True)
				row += 1
			row += 1

	if ctx["narrative"]:
		ws.cell(row, 1, _("Narrative")).font = bold
		row += 1
		ws.cell(row, 1, ctx["narrative"]).alignment = Alignment(wrap_text=True, vertical="top")
		ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=max(8, max_col))
		ws.row_dimensions[row].height = min(
			400, 15 * (1 + ctx["narrative"].count("\n") + len(ctx["narrative"]) // 120)
		)
		row += 2

	for label, value in (
		(_("Compiled by"), ctx["compiled_by"]),
		(_("Designation"), ctx["designation"]),
		(_("Date"), ctx["date"]),
		(_("Signature"), ctx["signature"]),
	):
		ws.cell(row, 1, label).font = bold
		ws.cell(row, 2, value or "")
		row += 1

	ws.column_dimensions["A"].width = 16
	ws.column_dimensions["B"].width = 45
	for c in range(3, max_col + 1):
		ws.column_dimensions[get_column_letter(c)].width = 11
	ws.column_dimensions[get_column_letter(max_col)].width = 40
	ws.sheet_view.showGridLines = True

	out = io.BytesIO()
	wb.save(out)
	return out.getvalue()


def attach_xlsx(run) -> str:
	snapshot = engine.snapshot_of(run)
	return attach_file(run, "xlsx_file", f"{file_base_name(run, snapshot)}.xlsx", build_xlsx(run, snapshot))
