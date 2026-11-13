# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient eligibility for visits and triage: no visits for disabled or merged patients (FR-6, NFR-9)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import get_link_to_form

# A1 sets this on the source patient of a non-destructive merge.
MERGED_INTO_FIELD = "krcs_merged_into"


class InactivePatientError(frappe.ValidationError):
	"""The patient is disabled or merged into another record."""


def lock_patient(patient: str) -> None:
	"""Row-lock the Patient until commit, so concurrent registrations for one patient serialise."""
	frappe.db.sql("SELECT `name` FROM `tabPatient` WHERE `name`=%s FOR UPDATE", (patient,))


def assert_patient_active(patient: str) -> None:
	"""Refuse a Patient whose status is Disabled or who has been merged into another record."""
	fields = ["name", "status", "patient_name"]
	has_merge = frappe.get_meta("Patient").has_field(MERGED_INTO_FIELD)
	if has_merge:
		fields.append(MERGED_INTO_FIELD)
	row = frappe.db.get_value("Patient", patient, fields, as_dict=True)
	if not row:
		frappe.throw(_("Patient {0} does not exist.").format(patient), InactivePatientError)
	if has_merge and row.get(MERGED_INTO_FIELD):
		frappe.throw(
			_("Patient {0} was merged into {1}. Use that record instead.").format(
				frappe.bold(row.patient_name or patient),
				get_link_to_form("Patient", row.get(MERGED_INTO_FIELD)),
			),
			InactivePatientError,
			title=_("Merged Patient"),
		)
	if row.status == "Disabled":
		frappe.throw(
			_("Patient {0} is disabled. Re-enable the record before registering a visit.").format(
				frappe.bold(row.patient_name or patient)
			),
			InactivePatientError,
			title=_("Disabled Patient"),
		)
