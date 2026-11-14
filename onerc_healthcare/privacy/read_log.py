# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Read-access logging outside the desk form (FR-40, NFR-7; audit finding M4).

Logging wrappers, wired through ``override_whitelisted_methods``. Each keeps the upstream signature,
annotations and decorators, delegates to the upstream function, then writes **one** Access Log Entry
(through `access_log.log_access`, which persists after the response like a desk open):

| Upstream method                            | Action       | Logged when                                  |
|--------------------------------------------|--------------|----------------------------------------------|
| frappe.client.get                          | Open (API)   | doctype in ACCESS_LOGGED_DOCTYPES            |
| frappe.client.get_value                    | Read (API)   | doctype in ACCESS_LOGGED_DOCTYPES            |
| frappe.desk.reportview.get                 | List         | doctype in ACCESS_LOGGED_DOCTYPES            |
| frappe.desk.reportview.export_query        | Export       | doctype in ACCESS_LOGGED_DOCTYPES            |
| frappe.desk.query_report.export_query      | Export       | one of our reports, or a report on a logged DocType |

The entry is written after the upstream call returns, so a refused call (no permission) logs nothing,
and the row count is known. `details` holds filter and field **names** and the row count, never values.
`patient` is set when the call is about one patient (by name, or a `patient` filter with "=").

`after_request` (hooks ``after_request``) logs successful GET reads of a logged record through the REST
API (`/api/resource/<doctype>/<name>`, `/api/v1/...`, `/api/v2/document/<doctype>/<name>`) as
"Open (API)". It is an *after*-request hook on purpose: API-key and token users are authenticated by
`validate_auth()` after the ``before_request`` hooks run, so they are still Guest there; after the
request the user is known and the response status tells whether the read succeeded.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any
from urllib.parse import unquote

import frappe
import frappe.client
import frappe.desk.query_report
import frappe.desk.reportview

from onerc_healthcare.privacy.access_log import (
	ACTION_EXPORT,
	ACTION_LIST,
	ACTION_OPEN_API,
	ACTION_READ_API,
	is_logged_doctype,
	is_logged_report,
	is_sensitive,
	log_access,
	logger,
	resolve_patient,
)

REST_READ_PATTERNS = (
	re.compile(r"^/api(?:/v1)?/resource/(?P<doctype>[^/]+)/(?P<name>.+?)/?$"),
	re.compile(r"^/api/v2/document/(?P<doctype>[^/]+)/(?P<name>.+?)(?:/copy)?/?$"),
)


# --- helpers -----------------------------------------------------------------------------------------
def _parse(value):
	if isinstance(value, str):
		try:
			return json.loads(value)
		except ValueError:
			return value
	return value


def filter_names(filters) -> list[str]:
	"""Field names used in `filters` (dict, list of lists, or JSON of either). Values are dropped."""
	filters = _parse(filters)
	names: list[str] = []
	if isinstance(filters, dict):
		names = [str(k) for k in filters]
	elif isinstance(filters, list | tuple):
		for f in filters:
			if isinstance(f, list | tuple) and f:
				# [doctype, field, op, value] or [field, op, value]
				names.append(str(f[1] if len(f) >= 4 else f[0]))
	elif isinstance(filters, str) and filters:
		names = ["name"]
	return list(dict.fromkeys(names))


def patient_from_filters(doctype: str, filters) -> str | None:
	"""The one patient a call is about: `patient` (or `name` on Patient) compared with "="."""
	filters = _parse(filters)
	key = "name" if doctype == "Patient" else "patient"
	if isinstance(filters, str) and filters:
		return resolve_patient(doctype, filters)
	if isinstance(filters, dict):
		value = filters.get(key)
		if isinstance(value, list | tuple):
			return value[1] if len(value) == 2 and value[0] == "=" and isinstance(value[1], str) else None
		return value if isinstance(value, str) else None
	if isinstance(filters, list | tuple):
		for f in filters:
			if not isinstance(f, list | tuple):
				continue
			field, op, value = (f[1], f[2], f[3]) if len(f) >= 4 else ([*f, None, None])[:3]
			if field == key and op == "=" and isinstance(value, str):
				return value
	return None


def _detail(**parts) -> str | None:
	out = []
	for label, value in parts.items():
		if value in (None, "", []):
			continue
		if isinstance(value, list | tuple):
			value = ", ".join(str(v) for v in value)
		out.append(f"{label.replace('_', ' ').capitalize()}: {value}")
	return "; ".join(out) or None


def _row_count(data) -> int | None:
	if isinstance(data, dict) and "values" in data:
		return len(data["values"] or [])
	if isinstance(data, list):
		return len(data)
	return None


def export_row_count() -> int | None:
	"""Data rows in the file an export just put on the response (header row excluded), or None."""
	try:
		response = frappe.local.response
		content = response.get("filecontent")
		filename = (response.get("filename") or "").lower()
		if not content:
			return None
		if filename.endswith(".csv"):
			text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
			return max(sum(1 for row in csv.reader(io.StringIO(text)) if row) - 1, 0)
		if filename.endswith(".xlsx"):
			from openpyxl import load_workbook

			sheet = load_workbook(io.BytesIO(content), read_only=True).active
			return max((sheet.max_row or 0) - 1, 0)
	except Exception:
		logger.exception("export row count failed")
	return None


# --- wrappers ----------------------------------------------------------------------------------------
@frappe.whitelist()
def get(
	doctype: str,
	name: str | int | None = None,
	filters: str | list | dict[str, Any] | None = None,
	parent: str | None = None,
):
	"""Logging wrapper of frappe.client.get."""
	result = frappe.client.get(doctype, name=name, filters=filters, parent=parent)
	if is_logged_doctype(doctype):
		docname = (result or {}).get("name")
		log_access(
			ACTION_OPEN_API,
			doctype,
			docname,
			patient=resolve_patient(doctype, docname),
			sensitive=is_sensitive(doctype, docname),
			detail=_detail(method="frappe.client.get", filters=filter_names(filters) if not name else None),
		)
	return result


@frappe.whitelist()
def get_value(
	doctype: str,
	fieldname: str | list[str] | dict[str, Any],
	filters: str | list | dict[str, Any] | None = None,
	as_dict: int | bool = True,
	debug: int | bool = False,
	parent: str | None = None,
):
	"""Logging wrapper of frappe.client.get_value."""
	result = frappe.client.get_value(
		doctype, fieldname, filters=filters, as_dict=as_dict, debug=debug, parent=parent
	)
	if is_logged_doctype(doctype):
		fields = _parse(fieldname)
		fields = list(fields) if isinstance(fields, list | tuple | dict) else [fieldname]
		single_name = (
			filters if isinstance(filters, str) and not str(filters).startswith(("{", "[")) else None
		)
		log_access(
			ACTION_READ_API,
			doctype,
			single_name,
			patient=patient_from_filters(doctype, filters),
			detail=_detail(
				method="frappe.client.get_value",
				fields=[str(f) for f in fields],
				filters=filter_names(filters),
				rows=1 if result not in (None, {}, []) else 0,
			),
		)
	return result


@frappe.whitelist()
@frappe.read_only()
def reportview_get():
	"""Logging wrapper of frappe.desk.reportview.get (list and report views)."""
	form = frappe._dict(frappe.local.form_dict)
	data = frappe.desk.reportview.get()
	if is_logged_doctype(form.doctype):
		log_access(
			ACTION_LIST,
			form.doctype,
			None,
			patient=patient_from_filters(form.doctype, form.filters),
			detail=_detail(
				method="frappe.desk.reportview.get",
				filters=filter_names(form.filters) + filter_names(form.or_filters),
				rows=_row_count(data),
			),
		)
	return data


@frappe.whitelist()
@frappe.read_only()
def reportview_export_query():
	"""Logging wrapper of frappe.desk.reportview.export_query (report builder / list export)."""
	form = frappe._dict(frappe.local.form_dict)
	result = frappe.desk.reportview.export_query()
	if is_logged_doctype(form.doctype):
		background = bool(frappe.utils.cint(form.export_in_background))
		log_access(
			ACTION_EXPORT,
			form.doctype,
			None,
			patient=patient_from_filters(form.doctype, form.filters),
			detail=_detail(
				method="frappe.desk.reportview.export_query",
				report=form.report_name,
				file_type=form.file_format_type,
				filters=filter_names(form.filters),
				rows="background export" if background else export_row_count(),
			),
		)
	return result


@frappe.whitelist()
def query_report_export_query():
	"""Logging wrapper of frappe.desk.query_report.export_query (script/query report export)."""
	form = frappe._dict(frappe.local.form_dict)
	report_name = form.report_name
	logged = is_logged_report(report_name)
	frappe.flags.krcs_report_export = logged  # the export is logged once, not also as a report run
	try:
		result = frappe.desk.query_report.export_query()
	finally:
		frappe.flags.krcs_report_export = False
	if logged:
		filters = _parse(form.filters) or {}
		patient = filters.get("patient") if isinstance(filters, dict) else None
		background = bool(frappe.utils.cint(form.export_in_background))
		log_access(
			ACTION_EXPORT,
			"Report",
			report_name,
			patient=patient if isinstance(patient, str) else None,
			detail=_detail(
				method="frappe.desk.query_report.export_query",
				file_type=form.file_format_type,
				filters=[k for k, v in filters.items() if v not in (None, "", 0)]
				if isinstance(filters, dict)
				else filter_names(filters),
				rows="background export" if background else export_row_count(),
			),
		)
	return result


# --- REST reads --------------------------------------------------------------------------------------
def match_rest_read(path: str) -> tuple[str, str] | None:
	path = unquote(path or "")
	for pattern in REST_READ_PATTERNS:
		m = pattern.match(path)
		if m and "/method/" not in m.group("name"):
			return m.group("doctype"), m.group("name")
	return None


def after_request(response=None, request=None) -> None:
	"""hooks ``after_request``: log a successful REST GET of a logged record as "Open (API)"."""
	try:
		request = request or getattr(frappe.local, "request", None)
		if not request or request.method != "GET" or not response or response.status_code != 200:
			return
		matched = match_rest_read(request.path)
		if not matched:
			return
		doctype, name = matched
		if not is_logged_doctype(doctype) or "run_method" in (frappe.local.form_dict or {}):
			return
		log_access(
			ACTION_OPEN_API,
			doctype,
			name,
			patient=resolve_patient(doctype, name),
			sensitive=is_sensitive(doctype, name),
			detail=_detail(method="REST GET"),
		)
	except Exception:
		logger.exception("REST read logging failed")
