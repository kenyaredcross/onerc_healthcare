# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Access Audit (FR-40, NFR-7; SDD §5.10): who opened, printed, exported or reported on patient
records, and who created or changed them, filtered by patient, user, date and action.

Sources:
- Access Log Entry rows (Open / Print / Export / Report / Download).
- Record creation (owner, creation) and Version metadata (who changed which record and when) for the
  patient-level DocTypes. Only the names of the changed fields are shown, never their values: the
  report is metadata-only for every reader, including the EMR System Administrator (D15).

Roles: Medical In-Charge, EMR System Administrator (report JSON; re-checked here). Each run is itself
written to the Access Log Entry as action Report.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import add_days, cint, get_datetime, getdate, today

from onerc_healthcare.constants import (
	ACCESS_LOG_DOCTYPE,
	ACCESS_LOGGED_DOCTYPES,
	CLINICAL_DOCTYPES,
	REPORT_ACCESS_AUDIT,
)
from onerc_healthcare.privacy.access_log import ACTIONS, log_report_run
from onerc_healthcare.privacy.lifecycle import has_any_role
from onerc_healthcare.privacy.permissions import ACCESS_LOG_READERS

ACTION_CREATE = "Create"
ACTION_CHANGE = "Change"
ALL_ACTIONS = (*ACTIONS, ACTION_CREATE, ACTION_CHANGE)
ROW_LIMIT = 5000
DEFAULT_DAYS = 7
SOURCE_ACCESS = "Access"
SOURCE_RECORD = "Record"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not has_any_role(ACCESS_LOG_READERS):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	data = get_data(filters)
	log_report_run(REPORT_ACCESS_AUDIT, patient=filters.get("patient"), filters=filters)
	message = None
	if len(data) >= ROW_LIMIT:
		message = _("Showing the latest {0} rows. Narrow the filters to see more.").format(ROW_LIMIT)
	return get_columns(), data, message


def get_columns():
	return [
		{"fieldname": "timestamp", "label": _("Timestamp"), "fieldtype": "Datetime", "width": 165},
		{"fieldname": "action", "label": _("Action"), "fieldtype": "Data", "width": 90},
		{"fieldname": "user", "label": _("User"), "fieldtype": "Link", "options": "User", "width": 190},
		{
			"fieldname": "reference_doctype",
			"label": _("Record Type"),
			"fieldtype": "Link",
			"options": "DocType",
			"width": 140,
		},
		{
			"fieldname": "reference_name",
			"label": _("Record"),
			"fieldtype": "Dynamic Link",
			"options": "reference_doctype",
			"width": 170,
		},
		{"fieldname": "patient", "label": _("Patient"), "fieldtype": "Data", "width": 160},
		{"fieldname": "sensitive", "label": _("Sensitive"), "fieldtype": "Check", "width": 80},
		{"fieldname": "ip", "label": _("IP Address"), "fieldtype": "Data", "width": 110},
		{"fieldname": "details", "label": _("Details"), "fieldtype": "Data", "width": 260},
		{"fieldname": "source", "label": _("Source"), "fieldtype": "Data", "width": 80},
	]


def _date_range(filters) -> tuple:
	to_date = getdate(filters.get("to_date") or today())
	from_date = getdate(filters.get("from_date") or add_days(to_date, -DEFAULT_DAYS))
	return get_datetime(f"{from_date} 00:00:00"), get_datetime(f"{to_date} 23:59:59.999999")


def get_data(filters) -> list[dict]:
	action = filters.get("action")
	if action and action not in ALL_ACTIONS:
		frappe.throw(_("Unknown action {0}.").format(action))
	start, end = _date_range(filters)
	rows: list[dict] = []
	if not action or action in ACTIONS:
		rows += access_rows(filters, start, end)
	if (not action or action in (ACTION_CREATE, ACTION_CHANGE)) and not cint(filters.get("sensitive_only")):
		rows += record_rows(filters, start, end)
	rows.sort(key=lambda r: get_datetime(r["timestamp"]), reverse=True)
	return rows[:ROW_LIMIT]


def access_rows(filters, start, end) -> list[dict]:
	conditions = {"timestamp": ("between", [start, end])}
	for f in ("user", "action", "patient", "reference_doctype"):
		if filters.get(f):
			conditions[f] = filters.get(f)
	if cint(filters.get("sensitive_only")):
		conditions["sensitive"] = 1
	rows = frappe.get_all(
		ACCESS_LOG_DOCTYPE,
		filters=conditions,
		fields=[
			"timestamp",
			"action",
			"user",
			"reference_doctype",
			"reference_name",
			"patient",
			"sensitive",
			"ip",
			"details",
		],
		order_by="timestamp desc",
		limit=ROW_LIMIT,
		ignore_permissions=True,
	)
	for r in rows:
		r["source"] = SOURCE_ACCESS
	return rows


def patient_doctypes() -> list[str]:
	"""Patient-level DocTypes whose creation and changes are audited (Patient + `patient` Link)."""
	out = []
	for doctype in dict.fromkeys((*CLINICAL_DOCTYPES, *ACCESS_LOGGED_DOCTYPES)):
		if doctype == ACCESS_LOG_DOCTYPE or not frappe.db.exists("DocType", doctype):
			continue
		meta = frappe.get_meta(doctype)
		if meta.istable or meta.issingle or not frappe.db.table_exists(doctype):
			continue
		if doctype == "Patient" or meta.has_field("patient"):
			out.append(doctype)
	return out


def _names_for_patient(doctype: str, patient: str) -> list[str]:
	if doctype == "Patient":
		return [patient] if frappe.db.exists("Patient", patient) else []
	return frappe.get_all(doctype, filters={"patient": patient}, pluck="name", ignore_permissions=True)


def _patients_of(doctype: str, names: list[str]) -> dict[str, str]:
	if doctype == "Patient":
		return {n: n for n in names}
	rows = frappe.get_all(
		doctype, filters={"name": ("in", names)}, fields=["name", "patient"], ignore_permissions=True
	)
	return {r.name: r.patient for r in rows}


def _changed_fields(data: str | None) -> str:
	"""Names of the fields a Version touched (never the old or new values)."""
	try:
		parsed = json.loads(data or "{}")
	except ValueError:
		return ""
	names = [c[0] for c in parsed.get("changed") or [] if c]
	names += [t[0] for t in parsed.get("row_changed") or [] if t]
	names += [t[0] for t in parsed.get("added") or [] if t]
	names += [t[0] for t in parsed.get("removed") or [] if t]
	names = list(dict.fromkeys(str(n) for n in names))
	return ("Changed: " + ", ".join(names)) if names else ""


def record_rows(filters, start, end) -> list[dict]:
	action = filters.get("action")
	user = filters.get("user")
	patient = filters.get("patient")
	doctypes = patient_doctypes()
	if filters.get("reference_doctype"):
		doctypes = [d for d in doctypes if d == filters.reference_doctype]
	rows: list[dict] = []
	for doctype in doctypes:
		names = _names_for_patient(doctype, patient) if patient else None
		if names is not None and not names:
			continue
		if action in (None, "", ACTION_CREATE):
			conditions = {"creation": ("between", [start, end])}
			if user:
				conditions["owner"] = user
			if names is not None:
				conditions["name"] = ("in", names)
			fields = ["name", "owner", "creation"] + (["patient"] if doctype != "Patient" else [])
			for r in frappe.get_all(
				doctype,
				filters=conditions,
				fields=fields,
				order_by="creation desc",
				limit=ROW_LIMIT,
				ignore_permissions=True,
			):
				rows.append(
					{
						"timestamp": r.creation,
						"action": ACTION_CREATE,
						"user": r.owner,
						"reference_doctype": doctype,
						"reference_name": r.name,
						"patient": r.name if doctype == "Patient" else r.get("patient"),
						"sensitive": 0,
						"ip": None,
						"details": None,
						"source": SOURCE_RECORD,
					}
				)
		if action in (None, "", ACTION_CHANGE):
			conditions = {"ref_doctype": doctype, "creation": ("between", [start, end])}
			if user:
				conditions["owner"] = user
			if names is not None:
				conditions["docname"] = ("in", names)
			versions = frappe.get_all(
				"Version",
				filters=conditions,
				fields=["docname", "owner", "creation", "data"],
				order_by="creation desc",
				limit=ROW_LIMIT,
				ignore_permissions=True,
			)
			patients = _patients_of(doctype, list({v.docname for v in versions})) if versions else {}
			for v in versions:
				rows.append(
					{
						"timestamp": v.creation,
						"action": ACTION_CHANGE,
						"user": v.owner,
						"reference_doctype": doctype,
						"reference_name": v.docname,
						"patient": patients.get(v.docname),
						"sensitive": 0,
						"ip": None,
						"details": _changed_fields(v.data),
						"source": SOURCE_RECORD,
					}
				)
	return rows
