# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Access logging (FR-40, NFR-7, SDD §5.10, §6.2). Hooks wired in hooks.py. Owned by B4.

- `log_open` (doc_events ``onload`` on every DocType in ``ACCESS_LOGGED_DOCTYPES``): a desk form open
  (`frappe.desk.form.load.getdoc`) writes one Access Log Entry "Open".
  ``getdoc`` is a GET request, and Frappe **rolls back** every GET at the end of the request
  (`frappe.app.sync_database`), which also discards ``frappe.db.after_commit`` callbacks. The entry is
  therefore written with ``commit_after_response`` (as View Log does): the insert runs after the
  response is sent, inside its own savepoint, and is committed. Outside a request, and in tests, it runs
  immediately.
  The ``onload`` that `savedocs` runs after a save is not logged as an open: the change itself is in the
  record's Version history.
- `mirror_core_access_log` (core Access Log ``after_insert``): Frappe logs prints, exports, report
  exports and file downloads in its own Access Log, which Log Settings clears after 30 days and which
  only System Manager can read. Each one is copied into an Access Log Entry (retained; NFR-7), with the
  patient resolved when the reference is patient-linked. Only metadata is copied (never filters, page
  HTML or field values).
- Mental Health encounters are marked ``sensitive`` (SDD §6.2, DPR-16; provisional until OI-7).

Logging never raises: a failure is written to the app logger and the record still opens.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.database.utils import commit_after_response
from frappe.rate_limiter import rate_limit
from frappe.utils import cstr, now_datetime

from onerc_healthcare.constants import (
	ACCESS_LOG_DOCTYPE,
	ACCESS_LOGGED_DOCTYPES,
	DATA_ACCESS_REQUEST_DOCTYPE,
	DEPT_MENTAL_HEALTH,
	ENCOUNTER_DOCTYPE,
	ENCOUNTER_VISIT_FIELD,
	REPORT_ACCESS_AUDIT,
	REPORT_CONSENT_EXCEPTIONS,
	REPORT_DAILY_ENTRY,
	REPORT_LAB_WORKLIST,
	REPORT_PHARMACY_WORKLIST,
	SERVICE_DELIVERED_DOCTYPE,
	SERVICE_TYPE_DOCTYPE,
	VISIT_DOCTYPE,
)

ACTION_OPEN = "Open"
ACTION_OPEN_API = "Open (API)"
ACTION_READ_API = "Read (API)"
ACTION_LIST = "List"
ACTION_PRINT = "Print"
ACTION_EXPORT = "Export"
ACTION_REPORT = "Report"
ACTION_DOWNLOAD = "Download"
ACTIONS = (
	ACTION_OPEN,
	ACTION_OPEN_API,
	ACTION_READ_API,
	ACTION_LIST,
	ACTION_PRINT,
	ACTION_EXPORT,
	ACTION_REPORT,
	ACTION_DOWNLOAD,
)

# Our own reports (exact names from constants); their runs and exports are logged.
APP_REPORTS = (
	REPORT_LAB_WORKLIST,
	REPORT_PHARMACY_WORKLIST,
	REPORT_DAILY_ENTRY,
	REPORT_CONSENT_EXCEPTIONS,
	REPORT_ACCESS_AUDIT,
)

SKIPPED_USERS = ("Administrator", "Guest")
SAVE_METHODS = ("frappe.desk.form.save.savedocs",)
DETAILS_MAX_LENGTH = 500

logger = frappe.logger("onerc_healthcare.privacy")


def _skip_user(user: str | None) -> bool:
	return not user or user in SKIPPED_USERS


def _in_maintenance() -> bool:
	flags = frappe.flags
	return bool(flags.in_install or flags.in_migrate or flags.in_patch or flags.in_import)


def _is_save_request() -> bool:
	request = getattr(frappe.local, "request", None)
	path = cstr(getattr(request, "path", "")) if request else ""
	cmd = cstr((frappe.local.form_dict or {}).get("cmd")) if hasattr(frappe.local, "form_dict") else ""
	return any(m in path or m == cmd for m in SAVE_METHODS)


def is_logged_doctype(doctype: str | None) -> bool:
	return bool(doctype) and doctype in ACCESS_LOGGED_DOCTYPES


def is_logged_report(report_name: str | None) -> bool:
	"""One of our reports, or a report on a logged DocType."""
	if not report_name:
		return False
	if report_name in APP_REPORTS:
		return True
	return is_logged_doctype(frappe.db.get_value("Report", report_name, "ref_doctype", cache=True))


def resolve_patient(doctype: str | None, name: str | None, _depth: int = 0) -> str | None:
	"""Patient ID behind a record: the Patient itself, its `patient` field, or a File's attachment (or
	the Data Access Request whose extract it is)."""
	if not doctype or not name or _depth > 1:
		return None
	if doctype == "Patient":
		return name
	if doctype == "File":
		row = frappe.db.get_value(
			"File", name, ["attached_to_doctype", "attached_to_name", "file_url"], as_dict=True
		)
		if not row:
			return None
		if row.attached_to_doctype and row.attached_to_name:
			return resolve_patient(row.attached_to_doctype, row.attached_to_name, _depth + 1)
		if row.file_url:
			return frappe.db.get_value(DATA_ACCESS_REQUEST_DOCTYPE, {"extract_file": row.file_url}, "patient")
		return None
	if not frappe.db.exists("DocType", doctype):
		return None
	meta = frappe.get_meta(doctype)
	df = meta.get_field("patient")
	if not df or df.fieldtype != "Link" or df.options != "Patient" or meta.issingle:
		return None
	return frappe.db.get_value(doctype, name, "patient")


def _department_of(values: dict) -> str | None:
	"""Department of an encounter: B1's `krcs_department` if present, else Marley's `medical_department`,
	else the linked visit's current department."""
	return (
		values.get("krcs_department") or values.get("medical_department") or values.get("_visit_department")
	)


def _encounter_values(doctype: str, source) -> dict:
	meta = frappe.get_meta(doctype)
	fields = [
		f for f in ("krcs_department", "medical_department", ENCOUNTER_VISIT_FIELD) if meta.has_field(f)
	]
	if isinstance(source, str):
		values = (frappe.db.get_value(doctype, source, fields, as_dict=True) or {}) if fields else {}
	else:
		values = {f: source.get(f) for f in fields}
	if not values.get("krcs_department") and not values.get("medical_department"):
		visit = values.get(ENCOUNTER_VISIT_FIELD)
		if visit:
			values["_visit_department"] = frappe.db.get_value(VISIT_DOCTYPE, visit, "current_department")
	return values


def is_sensitive(doctype: str | None, source) -> bool:
	"""A Mental Health encounter, or a Service Delivered whose Visit Service Type is marked Sensitive
	(mental health, alcohol and drug; Phase 3 L13). `source` is the document or its name. Provisional
	scope (OI-7)."""
	if not source:
		return False
	if doctype == SERVICE_DELIVERED_DOCTYPE:
		return is_sensitive_service(source)
	if doctype != ENCOUNTER_DOCTYPE:
		return False
	return _department_of(_encounter_values(doctype, source)) == DEPT_MENTAL_HEALTH


def is_sensitive_service(source) -> bool:
	"""Service Delivered (document or name) of a service type marked Sensitive."""
	service_type = (
		frappe.db.get_value(SERVICE_DELIVERED_DOCTYPE, source, "service_type")
		if isinstance(source, str)
		else source.get("service_type")
	)
	if not service_type or not frappe.get_meta(SERVICE_TYPE_DOCTYPE).has_field("sensitive"):
		return False
	return bool(frappe.db.get_value(SERVICE_TYPE_DOCTYPE, service_type, "sensitive"))


def build_entry(
	action: str,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	patient: str | None = None,
	sensitive: bool = False,
	details: str | None = None,
	user: str | None = None,
	ip: str | None = None,
	timestamp=None,
):
	"""An unsaved Access Log Entry with every value captured now (the insert may run after the response)."""
	return frappe.get_doc(
		{
			"doctype": ACCESS_LOG_DOCTYPE,
			"user": user or frappe.session.user,
			"action": action,
			"reference_doctype": reference_doctype,
			"reference_name": cstr(reference_name) or None,
			"patient": patient,
			"sensitive": 1 if sensitive else 0,
			"ip": ip if ip is not None else getattr(frappe.local, "request_ip", None),
			"timestamp": timestamp or now_datetime(),
			"details": cstr(details)[:DETAILS_MAX_LENGTH] or None,
		}
	)


def insert_entry(entry) -> None:
	"""Insert an Access Log Entry built by `build_entry`; only our code inserts (ignore_permissions)."""
	entry.flags.ignore_links = True
	entry.insert(ignore_permissions=True)


def write_entry(action: str, **kwargs):
	"""Insert an Access Log Entry now, in the current transaction. Returns it."""
	entry = build_entry(action, **kwargs)
	insert_entry(entry)
	return entry


def _safe_insert(entry) -> None:
	try:
		insert_entry(entry)
	except Exception:
		logger.exception("Access Log Entry insert failed")


def log_access(
	action: str,
	reference_doctype: str | None,
	reference_name: str | None,
	patient: str | None = None,
	sensitive: bool = False,
	detail: str | None = None,
) -> None:
	"""Public helper: log a read of patient data as the current user (e.g. B1's history panel).

	Same persistence as a desk open (`commit_after_response`, so it survives a GET request's rollback).
	Administrator, Guest and install/migrate are skipped. Never raises. `detail` is metadata only
	(field or filter names, row counts), never values.
	"""
	try:
		user = frappe.session.user
		if _skip_user(user) or _in_maintenance():
			return
		entry = build_entry(
			action,
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			patient=patient,
			sensitive=sensitive,
			details=detail,
			user=user,
		)
		# GET requests are rolled back: write after the response and commit (see module docstring).
		commit_after_response(lambda: _safe_insert(entry))
	except Exception:
		logger.exception("log_access failed")


def log_open(doc, method=None) -> None:
	"""doc_events onload for every DocType in constants.ACCESS_LOGGED_DOCTYPES (desk form open)."""
	try:
		if doc.is_new() or _is_save_request():
			return
		patient = doc.name if doc.doctype == "Patient" else (doc.get("patient") or None)
		log_access(
			ACTION_OPEN,
			doc.doctype,
			doc.name,
			patient=patient,
			sensitive=is_sensitive(doc.doctype, doc),
		)
	except Exception:
		logger.exception("log_open failed")


def _core_action(doc) -> str:
	method = cstr(doc.get("method"))
	file_type = cstr(doc.get("file_type"))
	if method in ("Print", "PDF"):
		return ACTION_PRINT
	if doc.get("export_from") == "File" or doc.get("report_name") == "Backup" or file_type == "vcf":
		return ACTION_DOWNLOAD
	if doc.get("report_name") and not file_type and method not in ("Export",):
		return ACTION_REPORT
	return ACTION_EXPORT


def _core_details(doc) -> str:
	parts = []
	if doc.get("report_name"):
		parts.append(f"Report: {doc.report_name}")
	if doc.get("file_type"):
		parts.append(f"File type: {doc.file_type}")
	page = cstr(doc.get("page"))
	if page.startswith("Print Format:"):
		parts.append(page[:140])
	method = cstr(doc.get("method"))
	if method and method not in ("Print", "PDF", "Export"):
		parts.append(f"Method: {method[:140]}")
	parts.append("Source: Frappe Access Log")
	return "; ".join(parts)


def logged_by_wrapper(doc) -> bool:
	"""The core Access Log row describes an export that `privacy.read_log` already logged (with its row
	count), so mirroring it would log the same export twice."""
	method = cstr(doc.get("method"))
	# Query report export: the report page calls make_access_log("Export", ...) after exporting.
	if method == "Export" and doc.get("report_name") and is_logged_report(doc.report_name):
		return True
	# Report view / list export (frappe.desk.reportview.export_query): no method, no document, no columns.
	return bool(
		not method
		and not doc.get("reference_document")
		and not doc.get("columns")
		and doc.get("file_type")
		and is_logged_doctype(doc.get("export_from"))
	)


def mirror_core_access_log(doc, method=None) -> None:
	"""doc_events after_insert on Frappe's core Access Log (prints, exports, reports, downloads)."""
	try:
		user = doc.get("user")
		if not user or user == "Guest" or logged_by_wrapper(doc):
			return
		if not (doc.get("export_from") or doc.get("reference_document") or doc.get("report_name")):
			# e.g. frappe.utils.print_format.report_to_pdf: no reference, and the report page has already
			# logged the print with its report name through make_access_log.
			return
		reference_doctype = doc.get("export_from") or None
		reference_name = doc.get("reference_document") or None
		if doc.get("report_name") and not reference_name:
			reference_doctype, reference_name = "Report", doc.report_name
		patient = resolve_patient(reference_doctype, reference_name) if reference_name else None
		sensitive = is_sensitive(reference_doctype, reference_name) if reference_name else False
		write_entry(
			_core_action(doc),
			reference_doctype=reference_doctype if frappe.db.exists("DocType", reference_doctype) else None,
			reference_name=reference_name,
			patient=patient,
			sensitive=sensitive,
			details=_core_details(doc),
			user=user,
			ip="",
			timestamp=doc.get("timestamp") or doc.get("creation") or now_datetime(),
		)
	except Exception:
		logger.exception("mirror_core_access_log failed")


# Background export jobs: the export was logged as Export when it was queued (privacy.read_log).
EXPORT_JOB_METHODS = ("frappe.desk.query_report.run_export_query_job",)


def _in_export_job() -> bool:
	job = getattr(frappe.local, "job", None)
	return bool(job) and job.get("method") in EXPORT_JOB_METHODS


def log_report_run(report_name: str, patient: str | None = None, filters: dict | None = None) -> None:
	"""Log a run of one of our patient-level reports as action Report (filter names only, no values).

	Skipped while `privacy.read_log` exports the report: the export is logged once, as Export."""
	if frappe.flags.krcs_report_export or _in_export_job():
		return
	used = sorted(k for k, v in (filters or {}).items() if v not in (None, "", 0))
	log_access(
		ACTION_REPORT,
		"Report",
		report_name,
		patient=patient,
		detail=("Filters: " + ", ".join(used)) if used else None,
	)


def _check_make_access_log(doctype: str | None, document, report_name: str | None) -> None:
	"""Every reference given must be readable: the report (if any) and the DocType / document (if any).
	A readable report never vouches for a document. A call with no reference at all is refused."""
	report_ok = False
	if report_name and frappe.db.exists("Report", report_name):
		if not frappe.get_doc("Report", report_name).is_permitted():
			frappe.throw(_("Not permitted"), frappe.PermissionError)
		report_ok = True
	if not doctype and document in (None, ""):
		if report_ok:
			return
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if not doctype or not frappe.db.exists("DocType", doctype):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if document not in (None, ""):
		if not frappe.db.exists(doctype, document) or not frappe.has_permission(
			doctype, "read", doc=document
		):
			frappe.throw(_("Not permitted"), frappe.PermissionError)
	elif not frappe.has_permission(doctype, "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


@frappe.whitelist()
@rate_limit(limit=60, seconds=60)
def make_access_log(
	doctype: str | None = None,
	document: str | int | None = None,
	method: str | None = None,
	file_type: str | None = None,
	report_name: str | None = None,
	filters: str | list | dict[str, Any] | None = None,
	page: str | None = None,
	columns: str | None = None,
):
	"""HTTP override of frappe.core.doctype.access_log.access_log.make_access_log (L6).

	Over HTTP anyone could write an Access Log row (and so an Access Log Entry) for any record. This
	override requires read on the document / DocType, or permission on the report, and rate-limits the
	call. Server-side callers import the core function directly and are unaffected.
	"""
	from frappe.core.doctype.access_log.access_log import make_access_log as core_make_access_log

	_check_make_access_log(doctype, document, report_name)
	return core_make_access_log(
		doctype=doctype,
		document=document,
		method=method,
		file_type=file_type,
		report_name=report_name,
		filters=filters,
		page=page,
		columns=columns,
	)
