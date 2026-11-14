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
| frappe.client.get_list                     | List         | doctype in ACCESS_LOGGED_DOCTYPES            |
| frappe.desk.reportview.get_list            | List         | doctype in ACCESS_LOGGED_DOCTYPES            |
| frappe.client.validate_link_and_fetch      | Read (API)   | logged doctype and `fields_to_fetch` given   |
| frappe.www.printview.get_html_and_style    | Print        | doctype in ACCESS_LOGGED_DOCTYPES (print preview) |
| frappe.core...data_import.download_template | (refused)   | logged doctype with records: System Manager only, logged as Export |

Data Import "Export Data" with records of a logged DocType is refused for everyone except System
Manager / Administrator: bulk export of clinical data is not an EMR workflow (a blank template is
still allowed). A System Manager export is logged as Export.

The entry is written after the upstream call returns, so a refused call (no permission) logs nothing,
and the row count is known. `details` holds filter and field **names** and the row count, never values.
`patient` is set when the call is about one patient (by name, or a `patient` filter with "=").

`after_request` (hooks ``after_request``) logs successful GET reads of a logged record through the REST
API (`/api/resource/<doctype>/<name>`, `/api/v1/...`, `/api/v2/document/<doctype>/<name>`) as
"Open (API)". It is an *after*-request hook on purpose: API-key and token users are authenticated by
`validate_auth()` after the ``before_request`` hooks run, so they are still Guest there; after the
request the user is known and the response status tells whether the read succeeded. List GETs
(`/api/resource/<doctype>`, `/api/v2/document/<doctype>`) are logged as "List", once per request: when
a list wrapper above already logged during the request (``frappe.flags.krcs_list_logged``), the hook
does not log again.
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
import frappe.www.printview
from frappe import _

from onerc_healthcare.privacy.access_log import (
	ACTION_EXPORT,
	ACTION_LIST,
	ACTION_OPEN_API,
	ACTION_PRINT,
	ACTION_READ_API,
	is_logged_doctype,
	is_logged_report,
	is_sensitive,
	log_access,
	logger,
	resolve_patient,
)
from onerc_healthcare.privacy.lifecycle import has_any_role

REST_LIST_PATTERNS = (
	re.compile(r"^/api(?:/v1)?/resource/(?P<doctype>[^/]+)/?$"),
	re.compile(r"^/api/v2/document/(?P<doctype>[^/]+)/?$"),
)
BULK_EXPORT_ROLES = ("System Manager",)

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


def _log_list(doctype: str, method: str, filters, or_filters, rows) -> None:
	log_access(
		ACTION_LIST,
		doctype,
		None,
		patient=patient_from_filters(doctype, filters),
		detail=_detail(method=method, filters=filter_names(filters) + filter_names(or_filters), rows=rows),
	)
	frappe.flags.krcs_list_logged = True


@frappe.whitelist()
def get_list(
	doctype: str,
	fields: str | list[str | dict[str, Any]] | None = None,
	filters: str | list | dict[str, Any] | None = None,
	group_by: str | list[str] | None = None,
	order_by: str | list[str] | None = None,
	limit_start: int | str | None = None,
	limit_page_length: int | str = 20,
	parent: str | None = None,
	debug: bool | int = False,
	as_dict: bool | int = True,
	or_filters: str | list[list] | dict[str, Any] | None = None,
	expand: str | list[str] | None = None,
):
	"""Logging wrapper of frappe.client.get_list."""
	result = frappe.client.get_list(
		doctype,
		fields=fields,
		filters=filters,
		group_by=group_by,
		order_by=order_by,
		limit_start=limit_start,
		limit_page_length=limit_page_length,
		parent=parent,
		debug=debug,
		as_dict=as_dict,
		or_filters=or_filters,
		expand=expand,
	)
	if is_logged_doctype(doctype):
		_log_list(doctype, "frappe.client.get_list", filters, or_filters, _row_count(result))
	return result


@frappe.whitelist()
@frappe.read_only()
def reportview_get_list():
	"""Logging wrapper of frappe.desk.reportview.get_list."""
	form = frappe._dict(frappe.local.form_dict)
	data = frappe.desk.reportview.get_list()
	if is_logged_doctype(form.doctype):
		_log_list(
			form.doctype, "frappe.desk.reportview.get_list", form.filters, form.or_filters, _row_count(data)
		)
	return data


@frappe.whitelist(methods=["GET", "POST"])
def validate_link_and_fetch(
	doctype: str,
	docname: str | int,
	fields_to_fetch: list[str] | str | None = None,
	# search_widget parameters
	query: str | None = None,
	filters: dict | list | str | None = None,
	**search_args,
):
	"""Logging wrapper of frappe.client.validate_link_and_fetch (link validation that fetches values)."""
	result = frappe.client.validate_link_and_fetch(
		doctype, docname, fields_to_fetch=fields_to_fetch, query=query, filters=filters, **search_args
	)
	fields = _parse(fields_to_fetch)
	if fields and result and is_logged_doctype(doctype):
		fields = fields if isinstance(fields, list | tuple) else [fields]
		name = result.get("name") or docname
		log_access(
			ACTION_READ_API,
			doctype,
			name,
			patient=resolve_patient(doctype, name),
			sensitive=is_sensitive(doctype, name),
			detail=_detail(method="frappe.client.validate_link_and_fetch", fields=[str(f) for f in fields]),
		)
	return result


@frappe.whitelist()
def get_html_and_style(
	doc: str | dict,
	name: str | None = None,
	print_format: str | None = None,
	no_letterhead: bool | None = None,
	letterhead: str | None = None,
	trigger_print: bool = False,
	style: str | None = None,
	settings: str | dict | None = None,
) -> dict[str, str | None]:
	"""Logging wrapper of frappe.www.printview.get_html_and_style (desk print preview)."""
	result = frappe.www.printview.get_html_and_style(
		doc,
		name=name,
		print_format=print_format,
		no_letterhead=no_letterhead,
		letterhead=letterhead,
		trigger_print=trigger_print,
		style=style,
		settings=settings,
	)
	if isinstance(doc, str) and isinstance(name, str):
		doctype, docname = doc, name
	else:
		parsed = frappe.parse_json(doc) or {}
		doctype, docname = parsed.get("doctype"), parsed.get("name")
	if is_logged_doctype(doctype):
		log_access(
			ACTION_PRINT,
			doctype,
			docname,
			patient=resolve_patient(doctype, docname),
			sensitive=is_sensitive(doctype, docname),
			detail=_detail(method="frappe.www.printview.get_html_and_style", print_format=print_format),
		)
	return result


@frappe.whitelist()
def download_template(
	doctype: str,
	export_fields: str | dict[str, list[str]] | None = None,
	export_records: str | None = None,
	export_filters: str | dict[str, Any] | list[list[Any]] | None = None,
	file_type: str = "CSV",
):
	"""Guard of frappe.core.doctype.data_import.data_import.download_template (Data Import export).

	Records of a logged DocType are exported only by System Manager / Administrator, and logged."""
	from frappe.core.doctype.data_import import data_import

	exports_records = bool(export_records) and export_records != "blank_template"
	if exports_records and is_logged_doctype(doctype) and not has_any_role(BULK_EXPORT_ROLES):
		frappe.throw(
			_("Bulk export of {0} records is not part of the EMR. Use the reports instead.").format(
				_(doctype)
			),
			frappe.PermissionError,
		)
	result = data_import.download_template(
		doctype,
		export_fields=export_fields,
		export_records=export_records,
		export_filters=export_filters,
		file_type=file_type,
	)
	if exports_records and is_logged_doctype(doctype):
		log_access(
			ACTION_EXPORT,
			doctype,
			None,
			patient=patient_from_filters(doctype, export_filters),
			detail=_detail(
				method="data_import.download_template",
				records=export_records,
				file_type=file_type,
				filters=filter_names(export_filters),
				rows=export_row_count(),
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


def match_rest_list(path: str) -> str | None:
	path = unquote(path or "")
	for pattern in REST_LIST_PATTERNS:
		m = pattern.match(path)
		if m:
			return m.group("doctype")
	return None


def _response_rows(response) -> int | None:
	try:
		payload = json.loads(response.get_data(as_text=True))
		data = payload.get("data", payload.get("message"))
		return len(data) if isinstance(data, list) else None
	except Exception:
		return None


def after_request(response=None, request=None) -> None:
	"""hooks ``after_request``: log a successful REST GET of a logged record ("Open (API)") or list
	("List", once per request)."""
	try:
		request = request or getattr(frappe.local, "request", None)
		if not request or request.method != "GET" or not response or response.status_code != 200:
			return
		list_doctype = match_rest_list(request.path)
		if list_doctype:
			if is_logged_doctype(list_doctype) and not frappe.flags.krcs_list_logged:
				form = frappe.local.form_dict or {}
				_log_list(
					list_doctype,
					"REST GET",
					form.get("filters"),
					form.get("or_filters"),
					_response_rows(response),
				)
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
