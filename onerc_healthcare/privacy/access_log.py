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

import frappe
from frappe.database.utils import commit_after_response
from frappe.utils import cstr, now_datetime

from onerc_healthcare.constants import (
	ACCESS_LOG_DOCTYPE,
	DEPT_MENTAL_HEALTH,
	ENCOUNTER_DOCTYPE,
	ENCOUNTER_VISIT_FIELD,
	VISIT_DOCTYPE,
)

ACTION_OPEN = "Open"
ACTION_PRINT = "Print"
ACTION_EXPORT = "Export"
ACTION_REPORT = "Report"
ACTION_DOWNLOAD = "Download"
ACTIONS = (ACTION_OPEN, ACTION_PRINT, ACTION_EXPORT, ACTION_REPORT, ACTION_DOWNLOAD)

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


def resolve_patient(doctype: str | None, name: str | None, _depth: int = 0) -> str | None:
	"""Patient ID behind a record: the Patient itself, its `patient` field, or a File's attachment."""
	if not doctype or not name or _depth > 1:
		return None
	if doctype == "Patient":
		return name
	if doctype == "File":
		row = frappe.db.get_value("File", name, ["attached_to_doctype", "attached_to_name"], as_dict=True)
		return resolve_patient(row.attached_to_doctype, row.attached_to_name, _depth + 1) if row else None
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
	"""A Mental Health encounter (`source` is the document or its name). Provisional scope (OI-7)."""
	if doctype != ENCOUNTER_DOCTYPE or not source:
		return False
	return _department_of(_encounter_values(doctype, source)) == DEPT_MENTAL_HEALTH


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


def log_open(doc, method=None) -> None:
	"""doc_events onload for every DocType in constants.ACCESS_LOGGED_DOCTYPES (desk form open)."""
	try:
		user = frappe.session.user
		if _skip_user(user) or _in_maintenance() or doc.is_new() or _is_save_request():
			return
		patient = doc.name if doc.doctype == "Patient" else (doc.get("patient") or None)
		entry = build_entry(
			ACTION_OPEN,
			reference_doctype=doc.doctype,
			reference_name=doc.name,
			patient=patient,
			sensitive=is_sensitive(doc.doctype, doc),
			user=user,
		)
		# GET requests are rolled back: write after the response and commit (see module docstring).
		commit_after_response(lambda: _safe_insert(entry))
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


def mirror_core_access_log(doc, method=None) -> None:
	"""doc_events after_insert on Frappe's core Access Log (prints, exports, reports, downloads)."""
	try:
		user = doc.get("user")
		if not user or user == "Guest":
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


def log_report_run(report_name: str, patient: str | None = None, filters: dict | None = None) -> None:
	"""Log a run of one of our patient-level reports as action Report (filter names only, no values)."""
	try:
		user = frappe.session.user
		if _skip_user(user):
			return
		used = sorted(k for k, v in (filters or {}).items() if v not in (None, "", 0))
		entry = build_entry(
			ACTION_REPORT,
			reference_doctype="Report",
			reference_name=report_name,
			patient=patient,
			details=("Filters: " + ", ".join(used)) if used else None,
			user=user,
		)
		# Report runs may be GET requests, which are rolled back: same mechanism as log_open.
		commit_after_response(lambda: _safe_insert(entry))
	except Exception:
		logger.exception("log_report_run failed")
