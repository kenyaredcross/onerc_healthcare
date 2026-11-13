# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient record extract for a Data Access Request (FR-38, SDD §5.10).

`create_extract_file(request)` builds a simple HTML summary of the patient's record on the server and
saves it as a **private** File attached to the request; the generation is written to the Access Log
Entry as "Export". Sections are read with `frappe.get_all` (no user permission filter: the action is
limited to the HIS Officer after Medical In-Charge approval) and skipped when the DocType or a column
does not exist on the site. Draft and cancelled clinical records are left out.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model import no_value_fields
from frappe.utils import escape_html, now_datetime
from frappe.utils.formatters import format_value

from onerc_healthcare.constants import (
	CONSENT_DOCTYPE,
	DATA_ACCESS_REQUEST_DOCTYPE,
	ENCOUNTER_DOCTYPE,
	PATIENT_IDENTIFIER_DOCTYPE,
	VISIT_DOCTYPE,
)

PATIENT_FIELDS = ("patient_name", "sex", "dob", "blood_group", "mobile", "email", "status")

# (title, doctype, columns, order_by, submitted_only)
SECTIONS = (
	(
		"Visits",
		VISIT_DOCTYPE,
		("name", "visit_date", "facility", "current_department", "status"),
		"visit_date desc",
		False,
	),
	(
		"Vital Signs",
		"Vital Signs",
		("signs_date", "temperature", "pulse", "respiratory_rate", "bp", "height", "weight", "bmi"),
		"signs_date desc",
		True,
	),
	(
		"Laboratory Results",
		"Observation",
		("posting_date", "observation_template", "result", "permitted_unit", "result_flag", "status"),
		"posting_date desc",
		True,
	),
	(
		"Prescriptions",
		"Medication Request",
		("order_date", "medication", "dosage", "period", "quantity", "order_description"),
		"order_date desc",
		True,
	),
	(
		"Referrals",
		"Referral",
		("name", "creation", "status"),
		"creation desc",
		False,
	),
	(
		"Dispensing",
		"Dispensing Record",
		("name", "creation"),
		"creation desc",
		True,
	),
	(
		"Consent",
		CONSENT_DOCTYPE,
		("recorded_at", "outcome", "statement_version", "given_by", "reason"),
		"recorded_at desc",
		True,
	),
)
ENCOUNTER_COLUMNS = ("encounter_date", "medical_department", "practitioner_name")


def _label(meta, fieldname: str) -> str:
	if fieldname == "name":
		return _("ID")
	if fieldname == "creation":
		return _("Created")
	return _(meta.get_label(fieldname))


def _fmt(meta, fieldname: str, value) -> str:
	if value in (None, ""):
		return ""
	df = meta.get_field(fieldname)
	text = format_value(value, df) if df else str(value)
	return escape_html(frappe.utils.strip_html(str(text)))


def _columns(meta, columns) -> list[str]:
	return [c for c in columns if c in ("name", "creation") or meta.has_field(c)]


def _table(meta, columns, rows) -> str:
	if not rows:
		return f"<p><em>{escape_html(_('None recorded.'))}</em></p>"
	head = "".join(f"<th>{escape_html(_label(meta, c))}</th>" for c in columns)
	body = "".join(
		"<tr>" + "".join(f"<td>{_fmt(meta, c, row.get(c))}</td>" for c in columns) + "</tr>" for row in rows
	)
	return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _section(title: str, doctype: str, columns, order_by: str, submitted_only: bool, patient: str) -> str:
	if not frappe.db.exists("DocType", doctype) or not frappe.db.table_exists(doctype):
		return ""
	meta = frappe.get_meta(doctype)
	if not meta.has_field("patient"):
		return ""
	cols = _columns(meta, columns)
	filters = {"patient": patient}
	if submitted_only and meta.is_submittable:
		filters["docstatus"] = 1
	elif meta.is_submittable:
		filters["docstatus"] = ("<", 2)
	order_field = order_by.split()[0]
	order = (
		order_by if order_field in ("creation", "name") or meta.has_field(order_field) else "creation desc"
	)
	rows = frappe.get_all(doctype, filters=filters, fields=cols, order_by=order)
	return f"<h2>{escape_html(_(title))}</h2>" + _table(meta, cols, rows)


def _encounters(patient: str) -> str:
	if not frappe.db.exists("DocType", ENCOUNTER_DOCTYPE):
		return ""
	meta = frappe.get_meta(ENCOUNTER_DOCTYPE)
	cols = _columns(meta, ENCOUNTER_COLUMNS)
	# B1's clinical note fields (krcs_ text fields) when present.
	note_fields = [
		df.fieldname
		for df in meta.fields
		if df.fieldname.startswith("krcs_")
		and df.fieldtype in ("Small Text", "Text", "Long Text", "Text Editor", "Data")
		and df.fieldtype not in no_value_fields
	]
	rows = frappe.get_all(
		ENCOUNTER_DOCTYPE,
		filters={"patient": patient, "docstatus": 1},
		fields=["name", *cols, *note_fields],
		order_by="encounter_date desc",
	)
	out = [f"<h2>{escape_html(_('Consultations'))}</h2>"]
	if not rows:
		out.append(f"<p><em>{escape_html(_('None recorded.'))}</em></p>")
	for row in rows:
		out.append(_table(meta, cols, [row]))
		for f in note_fields:
			if row.get(f):
				out.append(
					f"<p><strong>{escape_html(_label(meta, f))}:</strong> {_fmt(meta, f, row.get(f))}</p>"
				)
		codes = frappe.get_all(
			"Codification Table",
			filters={"parent": row.name, "parenttype": ENCOUNTER_DOCTYPE},
			fields=["code", "display"],
			order_by="idx asc",
		)
		if codes:
			items = "".join(
				f"<li>{escape_html(c.code or '')} {escape_html(c.display or '')}</li>" for c in codes
			)
			out.append(f"<p><strong>{escape_html(_('Diagnoses'))}:</strong></p><ul>{items}</ul>")
	return "".join(out)


def build_patient_extract_html(patient: str, request: str | None = None) -> str:
	meta = frappe.get_meta("Patient")
	values = (
		frappe.db.get_value(
			"Patient", patient, [f for f in PATIENT_FIELDS if meta.has_field(f)], as_dict=True
		)
		or {}
	)
	parts = [
		"<!doctype html><html><head><meta charset='utf-8'>",
		f"<title>{escape_html(_('Patient Record Extract'))}</title>",
		"<style>body{font-family:sans-serif;font-size:13px}table{border-collapse:collapse;margin-bottom:12px}"
		"th,td{border:1px solid #999;padding:3px 6px;text-align:left;vertical-align:top}</style>",
		"</head><body>",
		f"<h1>{escape_html(_('Patient Record Extract'))}</h1>",
		"<p>"
		+ escape_html(
			_("Patient {0}. Request {1}. Generated {2} by {3}.").format(
				patient,
				request or "-",
				format_value(now_datetime(), {"fieldtype": "Datetime"}),
				frappe.session.user,
			)
		)
		+ "</p>",
		f"<h2>{escape_html(_('Demographics'))}</h2>",
		_table(meta, [f for f in PATIENT_FIELDS if f in values], [values]),
	]
	if frappe.db.exists("DocType", PATIENT_IDENTIFIER_DOCTYPE):
		ident_meta = frappe.get_meta(PATIENT_IDENTIFIER_DOCTYPE)
		idents = frappe.get_all(
			PATIENT_IDENTIFIER_DOCTYPE,
			filters={"parent": patient, "parenttype": "Patient"},
			fields=["identifier_type", "identifier_value"],
			order_by="idx asc",
		)
		parts.append(f"<h2>{escape_html(_('Identifiers'))}</h2>")
		parts.append(_table(ident_meta, ["identifier_type", "identifier_value"], idents))
	parts.append(_encounters(patient))
	for title, doctype, columns, order_by, submitted_only in SECTIONS:
		parts.append(_section(title, doctype, columns, order_by, submitted_only, patient))
	parts.append("</body></html>")
	return "".join(parts)


def create_extract_file(request_doc):
	"""Generate the extract for `request_doc`, save it as a private File, and log it as an Export."""
	from onerc_healthcare.privacy.access_log import ACTION_EXPORT, write_entry

	html = build_patient_extract_html(request_doc.patient, request_doc.name)
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"{request_doc.name}-record-extract.html",
			"attached_to_doctype": DATA_ACCESS_REQUEST_DOCTYPE,
			"attached_to_name": request_doc.name,
			"attached_to_field": "extract_file",
			"is_private": 1,
			"content": html,
		}
	)
	file_doc.insert(ignore_permissions=True)
	write_entry(
		ACTION_EXPORT,
		reference_doctype=DATA_ACCESS_REQUEST_DOCTYPE,
		reference_name=request_doc.name,
		patient=request_doc.patient,
		details=f"Patient record extract (HTML), file {file_doc.name}",
	)
	return file_doc
