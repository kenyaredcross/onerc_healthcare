# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Due Immunisations (CON-16; docs/MCH_CONTRACT.md §7): the vaccine doses due or overdue in a date window, for
defaulter tracing at the child welfare clinic.

**Children:** under 5 years on the first day of the window, not merged or disabled, with at least one
submitted, not archived Immunisation (at the chosen facility, when one is chosen).

**Doses:** each child's schedule is computed on the window's first day (`mch.immunisation.compute_schedule`):
a dose that is Due or Overdue on that day is listed with that status; an Upcoming dose whose due date falls
in the window is listed as Due. Given and Missed doses are not listed.

Filters: facility, from / to date (default today to today + 7 days), status (Due, Overdue or both). Four
queries in all (children, given doses, schedule rows, last immunisation), whatever the number of children.
Each run is written to the access log (FR-40, NFR-7).
"""

from __future__ import annotations

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, today

from onerc_healthcare.clinical.utils import require_roles
from onerc_healthcare.constants import (
	CHILD_MCH_ROLES,
	GROWTH_MAX_AGE_DAYS,
	IMMUNISATION_DOCTYPE,
	KEPI_SCHEDULE_DOCTYPE,
	PATIENT_DOB_ESTIMATED_FIELD,
	PATIENT_MOTHER_FIELD,
	REPORT_DUE_IMMUNISATIONS,
	ROLE_HIS,
	SCHEDULE_DUE,
	SCHEDULE_OVERDUE,
	SCHEDULE_UPCOMING,
)
from onerc_healthcare.mch import common, immunisation
from onerc_healthcare.privacy.access_log import log_report_run
from onerc_healthcare.registration.identifiers import MERGED_INTO_FIELD

REPORT_ROLES = (*CHILD_MCH_ROLES, ROLE_HIS)
DEFAULT_DAYS_AHEAD = 7
MAX_WINDOW_DAYS = 366
STATUS_BOTH = "Due and Overdue"
STATUS_FILTERS = {
	STATUS_BOTH: (SCHEDULE_DUE, SCHEDULE_OVERDUE),
	SCHEDULE_DUE: (SCHEDULE_DUE,),
	SCHEDULE_OVERDUE: (SCHEDULE_OVERDUE,),
}


def execute(filters: dict | None = None):
	filters = frappe._dict(filters or {})
	require_roles(REPORT_ROLES, _("see the Due Immunisations list"))
	# Patient-level report: every run goes to the Access Log Entry (FR-40, NFR-7).
	log_report_run(REPORT_DUE_IMMUNISATIONS, filters=filters)
	return get_columns(), get_data(filters)


def get_columns() -> list[dict]:
	return [
		{
			"label": _("Child"),
			"fieldname": "patient",
			"fieldtype": "Link",
			"options": "Patient",
			"width": 130,
		},
		{"label": _("Child Name"), "fieldname": "patient_name", "fieldtype": "Data", "width": 170},
		{"label": _("Sex"), "fieldname": "sex", "fieldtype": "Data", "width": 70},
		{"label": _("Age"), "fieldname": "age", "fieldtype": "Data", "width": 110},
		{
			"label": _("Mother"),
			"fieldname": "mother",
			"fieldtype": "Link",
			"options": "Patient",
			"width": 130,
		},
		{"label": _("Mother Name"), "fieldname": "mother_name", "fieldtype": "Data", "width": 160},
		{"label": _("Phone"), "fieldname": "phone", "fieldtype": "Data", "width": 120},
		{
			"label": _("Dose"),
			"fieldname": "vaccine",
			"fieldtype": "Link",
			"options": KEPI_SCHEDULE_DOCTYPE,
			"width": 120,
		},
		{"label": _("Due Date"), "fieldname": "due_date", "fieldtype": "Date", "width": 110},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 100},
		{"label": _("Last Immunisation"), "fieldname": "last_given_on", "fieldtype": "Date", "width": 130},
		{"label": _("DOB Estimated"), "fieldname": "dob_estimated", "fieldtype": "Check", "width": 90},
	]


def _window(filters) -> tuple:
	from_date = getdate(filters.from_date or today())
	to_date = getdate(filters.to_date or add_days(from_date, DEFAULT_DAYS_AHEAD))
	if to_date < from_date:
		frappe.throw(_("The To Date is before the From Date."), title=_("Check the dates"))
	if (to_date - from_date).days > MAX_WINDOW_DAYS:
		frappe.throw(
			_("Choose a window of at most {0} days.").format(MAX_WINDOW_DAYS), title=_("Check the dates")
		)
	return from_date, to_date


def get_data(filters: frappe._dict) -> list[dict]:
	from_date, to_date = _window(filters)
	wanted = STATUS_FILTERS.get(filters.status or STATUS_BOTH)
	if wanted is None:
		frappe.throw(_("Choose the status: {0}.").format(", ".join(STATUS_FILTERS)))
	children = _children(filters.facility, from_date)
	if not children:
		return []
	schedules = immunisation.schedule_rows(children, from_date)
	last = _last_given(list(schedules))
	rows = []
	for child in children:
		for dose in schedules.get(child.name, []):
			status = dose["status"]
			if status == SCHEDULE_UPCOMING and dose["due_date"] <= to_date:
				status = SCHEDULE_DUE  # falls due in the window
			if status not in wanted:
				continue
			rows.append(
				{
					"patient": child.name,
					"patient_name": child.patient_name,
					"sex": child.sex,
					"age": common.age_label(common.age_in_days(child.dob, from_date)),
					"mother": child.mother,
					"mother_name": child.mother_name,
					"phone": child.mother_phone or child.phone,
					"vaccine": dose["vaccine"],
					"due_date": dose["due_date"],
					"status": status,
					"last_given_on": last.get(child.name),
					"dob_estimated": cint(child.get(PATIENT_DOB_ESTIMATED_FIELD)),
				}
			)
	rows.sort(key=lambda r: (r["due_date"], r["patient_name"] or "", r["patient"]))
	return rows


def _children(facility: str | None, on) -> list[frappe._dict]:
	"""Children under 5 on `on` with a submitted, not archived Immunisation (at `facility`), with the mother's
	name and phone (one query)."""
	params = {"min_dob": on - timedelta(days=GROWTH_MAX_AGE_DAYS - 1), "on": on}
	conds = ["imm.docstatus = 1", immunisation.not_archived_sql("imm")]
	if facility:
		conds.append("imm.facility = %(facility)s")
		params["facility"] = facility
	patient_meta = frappe.get_meta("Patient")
	has_mother = patient_meta.has_field(PATIENT_MOTHER_FIELD)
	mother_join = f"LEFT JOIN `tabPatient` m ON m.name = p.`{PATIENT_MOTHER_FIELD}`" if has_mother else ""
	mother_cols = (
		f"p.`{PATIENT_MOTHER_FIELD}` AS mother, m.patient_name AS mother_name, m.mobile AS mother_phone"
		if has_mother
		else "NULL AS mother, NULL AS mother_name, NULL AS mother_phone"
	)
	merged = (
		f"AND IFNULL(p.`{MERGED_INTO_FIELD}`, '') = ''" if patient_meta.has_field(MERGED_INTO_FIELD) else ""
	)
	estimated = (
		f"p.`{PATIENT_DOB_ESTIMATED_FIELD}`" if patient_meta.has_field(PATIENT_DOB_ESTIMATED_FIELD) else "0"
	)
	return frappe.db.sql(
		f"""
		SELECT p.name, p.patient_name, p.sex, p.dob, p.mobile AS phone,
			{estimated} AS `{PATIENT_DOB_ESTIMATED_FIELD}`, {mother_cols}
		FROM `tabPatient` p
		{mother_join}
		WHERE p.name IN (
				SELECT imm.patient FROM `tab{IMMUNISATION_DOCTYPE}` imm WHERE {" AND ".join(conds)}
			)
			AND p.dob IS NOT NULL AND p.dob >= %(min_dob)s AND p.dob <= %(on)s
			AND IFNULL(p.status, '') != 'Disabled' {merged}
		""",
		params,
		as_dict=True,
	)


def _last_given(patients: list[str]) -> dict:
	"""{patient: date of the latest submitted, not archived Immunisation} (one query)."""
	if not patients:
		return {}
	rows = frappe.db.sql(
		f"""
		SELECT imm.patient, MAX(imm.given_on) AS last_given_on
		FROM `tab{IMMUNISATION_DOCTYPE}` imm
		WHERE imm.docstatus = 1 AND {immunisation.not_archived_sql("imm")} AND imm.patient IN %(patients)s
		GROUP BY imm.patient
		""",
		{"patients": tuple(patients)},
		as_dict=True,
	)
	return {r.patient: r.last_given_on for r in rows}
