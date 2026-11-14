# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""PDF export of a MOH report run (FR-30): the same generic layout as the Excel file
(`export_xlsx.export_context`), rendered with ``templates/moh_report_run.html`` and
`frappe.utils.pdf.get_pdf`. The form preview renders the same template.

Photographs attached to the run (``photo_1..3``) are embedded as data URIs, because wkhtmltopdf runs
with local file access disabled and cannot read private files. Only Files attached to this run are
used; any other URL in a photo field is left out with a note.
"""

from __future__ import annotations

import base64
import mimetypes

import frappe

from onerc_healthcare.reporting import engine
from onerc_healthcare.reporting.export_xlsx import attach_file, export_context, file_base_name

TEMPLATE = "onerc_healthcare/reporting/templates/moh_report_run.html"
PHOTO_FIELDS = ("photo_1", "photo_2", "photo_3")
PDF_OPTIONS = {"orientation": "Landscape", "page-size": "A4"}

logger = frappe.logger("onerc_healthcare.reporting")


def _own_file(run, file_url: str):
	"""The File behind `file_url` if it is attached to this run, else None (review L6: a photo field
	must not pull another record's private file into the export)."""
	name = frappe.db.get_value(
		"File",
		{"file_url": file_url, "attached_to_doctype": run.doctype, "attached_to_name": run.name},
		"name",
	)
	return frappe.get_doc("File", name) if name else None


def _data_uri(file_doc) -> str | None:
	try:
		content = file_doc.get_content()
	except Exception:
		logger.exception(f"MOH report photo {file_doc.file_url} could not be read")
		return None
	if isinstance(content, str):
		content = content.encode()
	mime = mimetypes.guess_type(file_doc.file_url)[0] or "image/png"
	return f"data:{mime};base64,{base64.b64encode(content).decode()}"


def photo_sources(run, embed: bool) -> tuple[list[str], int]:
	"""(image sources, number of photo fields skipped because the file is not attached to this run)."""
	sources, skipped = [], 0
	for url in (run.get(f) for f in PHOTO_FIELDS if run.get(f)):
		file_doc = _own_file(run, url)
		if file_doc is None:
			skipped += 1
			continue
		src = _data_uri(file_doc) if embed else file_doc.file_url
		if src:
			sources.append(src)
	return sources, skipped


def render_html(run, snapshot: dict | None = None, preview: bool = False) -> str:
	snapshot = snapshot or engine.snapshot_of(run) or {}
	photos, skipped = photo_sources(run, embed=not preview)
	return frappe.render_template(
		TEMPLATE,
		{
			"ctx": export_context(run, snapshot),
			"preview": preview,
			"photos": photos,
			"photos_skipped": skipped,
			"header_groups": engine.header_groups,
		},
	)


def build_pdf(run, snapshot: dict | None = None) -> bytes:
	from frappe.utils.pdf import get_pdf

	return get_pdf(render_html(run, snapshot), dict(PDF_OPTIONS))


def attach_pdf(run) -> str:
	snapshot = engine.snapshot_of(run)
	return attach_file(run, "pdf_file", f"{file_base_name(run, snapshot)}.pdf", build_pdf(run, snapshot))
