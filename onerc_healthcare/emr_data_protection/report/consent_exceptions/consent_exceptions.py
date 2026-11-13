# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Consent Exceptions (FR-37, DPR-04; SDD §5.10): patients whose current consent is not Given, or who
have none, with their last visit date. Merged (source) patients are left out. Each run is written to
the Access Log Entry as action Report. Roles: Medical In-Charge, HIS Officer (report JSON)."""

import frappe
from frappe import _
from frappe.utils import cint

from onerc_healthcare.constants import (
	CONSENT_DOCTYPE,
	REPORT_CONSENT_EXCEPTIONS,
	ROLE_HIS,
	ROLE_MIC,
	VISIT_DOCTYPE,
)
from onerc_healthcare.privacy.access_log import log_report_run
from onerc_healthcare.privacy.consent import OUTCOME_GIVEN
from onerc_healthcare.privacy.lifecycle import has_any_role
from onerc_healthcare.visit.patients import MERGED_INTO_FIELD

REPORT_ROLES = (ROLE_MIC, ROLE_HIS)
NO_CONSENT = "No consent recorded"
ROW_LIMIT = 5000


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not has_any_role(REPORT_ROLES):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	data = get_data(filters)
	log_report_run(REPORT_CONSENT_EXCEPTIONS, patient=filters.get("patient"), filters=filters)
	return get_columns(), data


def get_columns():
	return [
		{
			"fieldname": "patient",
			"label": _("Patient"),
			"fieldtype": "Link",
			"options": "Patient",
			"width": 170,
		},
		{"fieldname": "patient_name", "label": _("Patient Name"), "fieldtype": "Data", "width": 180},
		{"fieldname": "outcome", "label": _("Consent"), "fieldtype": "Data", "width": 150},
		{"fieldname": "consent_date", "label": _("Recorded At"), "fieldtype": "Datetime", "width": 160},
		{
			"fieldname": "statement_version",
			"label": _("Statement Version"),
			"fieldtype": "Data",
			"width": 120,
		},
		{"fieldname": "reason", "label": _("Reason"), "fieldtype": "Data", "width": 220},
		{"fieldname": "last_visit_date", "label": _("Last Visit"), "fieldtype": "Date", "width": 110},
		{
			"fieldname": "consent",
			"label": _("Consent Record"),
			"fieldtype": "Link",
			"options": CONSENT_DOCTYPE,
			"width": 150,
		},
	]


def get_data(filters) -> list[dict]:
	conditions = ["(c.name IS NULL OR c.outcome != %(given)s)"]
	values = {"given": OUTCOME_GIVEN}
	if frappe.get_meta("Patient").has_field(MERGED_INTO_FIELD):
		conditions.append(f"COALESCE(p.`{MERGED_INTO_FIELD}`, '') = ''")
	if filters.get("patient"):
		conditions.append("p.name = %(patient)s")
		values["patient"] = filters.patient
	outcome = filters.get("outcome")
	if outcome == NO_CONSENT:
		conditions.append("c.name IS NULL")
	elif outcome:
		conditions.append("c.outcome = %(outcome)s")
		values["outcome"] = outcome
	if cint(filters.get("with_visits_only")):
		conditions.append("v.last_visit_date IS NOT NULL")
	if filters.get("visited_since"):
		conditions.append("v.last_visit_date >= %(visited_since)s")
		values["visited_since"] = filters.visited_since

	rows = frappe.db.sql(
		f"""
		SELECT p.name AS patient, p.patient_name, c.outcome, c.recorded_at AS consent_date,
			c.statement_version, c.reason, c.name AS consent, v.last_visit_date
		FROM `tabPatient` p
		LEFT JOIN `tab{CONSENT_DOCTYPE}` c ON c.name = (
			SELECT c2.name FROM `tab{CONSENT_DOCTYPE}` c2
			WHERE c2.patient = p.name AND c2.docstatus = 1
			ORDER BY c2.recorded_at DESC, c2.creation DESC LIMIT 1
		)
		LEFT JOIN (
			SELECT patient, MAX(visit_date) AS last_visit_date FROM `tab{VISIT_DOCTYPE}` GROUP BY patient
		) v ON v.patient = p.name
		WHERE {" AND ".join(conditions)}
		ORDER BY v.last_visit_date DESC, p.creation DESC
		LIMIT {ROW_LIMIT}
		""",
		values,
		as_dict=True,
	)
	for row in rows:
		if not row.outcome:
			row.outcome = _(NO_CONSENT)
	return rows
