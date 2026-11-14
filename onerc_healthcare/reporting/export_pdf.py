# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""PDF export of a MOH report run (FR-30): the same generic layout as the Excel file
(`export_xlsx.export_context`), rendered with ``templates/moh_report_run.html`` and
`frappe.utils.pdf.get_pdf`. The form preview renders the same template.

Photographs attached to the run (``photo_1..3``) are embedded as data URIs, because wkhtmltopdf runs
with local file access disabled and cannot read private files.
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


def _data_uri(file_url: str) -> str | None:
	try:
		file_doc = frappe.get_doc("File", {"file_url": file_url})
		content = file_doc.get_content()
	except Exception:
		logger.exception(f"MOH report photo {file_url} could not be read")
		return None
	if isinstance(content, str):
		content = content.encode()
	mime = mimetypes.guess_type(file_url)[0] or "image/png"
	return f"data:{mime};base64,{base64.b64encode(content).decode()}"


def photo_sources(run, embed: bool) -> list[str]:
	urls = [run.get(f) for f in PHOTO_FIELDS if run.get(f)]
	if not embed:
		return urls
	return [uri for uri in (_data_uri(u) for u in urls) if uri]


def render_html(run, snapshot: dict | None = None, preview: bool = False) -> str:
	snapshot = snapshot or engine.snapshot_of(run) or {}
	return frappe.render_template(
		TEMPLATE,
		{
			"ctx": export_context(run, snapshot),
			"preview": preview,
			"photos": photo_sources(run, embed=not preview),
			"header_groups": engine.header_groups,
		},
	)


def build_pdf(run, snapshot: dict | None = None) -> bytes:
	from frappe.utils.pdf import get_pdf

	return get_pdf(render_html(run, snapshot), dict(PDF_OPTIONS))


def attach_pdf(run) -> str:
	snapshot = engine.snapshot_of(run)
	return attach_file(run, "pdf_file", f"{file_base_name(run, snapshot)}.pdf", build_pdf(run, snapshot))
