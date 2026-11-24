# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""PNC Visit rules (MCH-A; docs/MCH_CONTRACT.md §6, BRD FR-14, SDD §5.3, D62). Provisional (OI-8).

A PNC Visit is made either during an open visit of the patient (`mch.common.check_visit`; one per visit,
subject and patient) or on the ward against the mother's open Maternity Admission (open, not archived,
locked first; one per admission and patient; facility from the admission). Exactly one of the two.

| Subject | Patient | ``mother`` | ``delivery_record`` | ``days_after_delivery`` |
|---|---|---|---|---|
| Mother | a female patient | the patient | her latest submitted, non-archived Delivery Record | from its ``delivered_at`` |
| Baby | under 1 year | ``Patient.krcs_mother`` (blank when born elsewhere) | the submitted, non-archived Delivery Record listing the baby as ``newborn_patient`` | from the date of birth |

On the ward the patient is the admitted mother (subject Mother) or a baby whose ``krcs_mother`` is the
admitted mother (subject Baby). The PNC date is not in the future, nor before the delivery (Mother) or the
birth (Baby). Fields of the other subject are cleared. Plausible ranges as ANC (0 = not recorded).
Submittable, no cancel; corrections by addendum (`mch.common`).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_years, cint, date_diff, getdate, today

from onerc_healthcare.clinical.utils import require_roles
from onerc_healthcare.constants import (
	ADMISSION_OPEN_STATUSES,
	DELIVERY_BABY_DOCTYPE,
	DELIVERY_RECORD_DOCTYPE,
	MATERNITY_ADMISSION_DOCTYPE,
	MCH_ROLES,
	PATIENT_MOTHER_FIELD,
	PNC_SUBJECT_BABY,
	PNC_SUBJECT_MOTHER,
	PNC_TIMINGS,
)
from onerc_healthcare.mch import common
from onerc_healthcare.mch.anc import ImplausibleANCValueError, check_female, check_ranges

ARCHIVED_FIELD = common.ARCHIVED_FIELD
MOTHER_FIELDS = (
	"m_bp_systolic",
	"m_bp_diastolic",
	"m_temperature",
	"m_pulse",
	"m_breasts",
	"m_uterus",
	"m_lochia",
	"m_perineum",
	"m_cs_wound",
	"m_mood",
	"fp_counselled",
	"fp_method",
	"m_danger_signs",
)
BABY_FIELDS = (
	"b_temperature",
	"b_weight_kg",
	"b_cord",
	"b_feeding",
	"b_jaundice",
	"b_eyes",
	"b_danger_signs",
)
MOTHER_RANGES = (
	("m_bp_systolic", 50, 250, "mmHg"),
	("m_bp_diastolic", 30, 150, "mmHg"),
	("m_temperature", 30, 43, "°C"),
	("m_pulse", 30, 220, "beats/min"),
)
BABY_RANGES = (
	("b_temperature", 30, 43, "°C"),
	("b_weight_kg", 0.5, 10, "kg"),
)


def _fmt_date(value) -> str:
	return frappe.format(value, {"fieldtype": "Date"})


def _clear(doc, fieldnames) -> None:
	"""Clear the other subject's fields (numbers and ticks to 0, their stored "not recorded")."""
	for fieldname in fieldnames:
		df = doc.meta.get_field(fieldname)
		doc.set(fieldname, 0 if df and df.fieldtype in ("Int", "Float", "Check") else None)


def visit_validate(doc) -> None:
	if common.guard_draft(doc):
		return
	if bool(doc.get("visit")) == bool(doc.get("maternity_admission")):
		frappe.throw(
			_("Choose either the patient's visit or the mother's Maternity Admission (one of the two)."),
			common.MCHRecordError,
			title=_("Missing Value"),
		)
	if doc.get("visit"):
		common.check_visit(doc)
		common.guard_one_per_visit(doc, ("visit", "subject", "patient"))
	else:
		_check_admission(doc)
		common.guard_one_per_visit(doc, ("maternity_admission", "patient"))
	common.set_recorded_by(doc)
	if doc.is_new():
		require_roles(MCH_ROLES, _("record a PNC check"))
	if doc.subject not in (PNC_SUBJECT_MOTHER, PNC_SUBJECT_BABY):
		frappe.throw(
			_("Choose whom the check is of: {0} or {1}.").format(_(PNC_SUBJECT_MOTHER), _(PNC_SUBJECT_BABY)),
			common.MCHRecordError,
			title=_("Missing Value"),
		)
	if doc.get("timing") and doc.timing not in PNC_TIMINGS:
		frappe.throw(
			_("Timing must be one of {0}.").format(", ".join(_(t) for t in PNC_TIMINGS)),
			common.MCHRecordError,
		)
	pnc_date = getdate(doc.pnc_date or today())
	if pnc_date > getdate(today()):
		frappe.throw(
			_("The PNC date cannot be in the future."), ImplausibleANCValueError, title=_("Check the dates")
		)
	if doc.subject == PNC_SUBJECT_MOTHER:
		_set_mother(doc, pnc_date)
	else:
		_set_baby(doc, pnc_date)


def _check_admission(doc) -> None:
	"""The admission (locked first, D41) is open and not archived; the patient is the admitted mother (subject
	Mother) or her baby (subject Baby). Facility from the admission."""
	adm = frappe.db.get_value(
		MATERNITY_ADMISSION_DOCTYPE,
		doc.maternity_admission,
		["name", "patient", "facility", "status", ARCHIVED_FIELD],
		as_dict=True,
		for_update=True,
	)
	if not adm:
		frappe.throw(
			_("Maternity Admission {0} does not exist.").format(doc.maternity_admission),
			common.MCHRecordError,
		)
	if doc.is_new() and (adm.status not in ADMISSION_OPEN_STATUSES or cint(adm.get(ARCHIVED_FIELD))):
		frappe.throw(
			_("Maternity Admission {0} is {1}. A ward PNC check is made during an open admission.").format(
				adm.name, _("archived") if cint(adm.get(ARCHIVED_FIELD)) else _(adm.status)
			),
			common.MCHRecordError,
			title=_("Admission Closed"),
		)
	if doc.patient == adm.patient:
		expected = PNC_SUBJECT_MOTHER
	elif doc.patient and frappe.db.get_value("Patient", doc.patient, PATIENT_MOTHER_FIELD) == adm.patient:
		expected = PNC_SUBJECT_BABY
	else:
		frappe.throw(
			_("{0} is neither the mother admitted on {1} nor her baby.").format(doc.patient, adm.name),
			common.MCHRecordError,
			title=_("Wrong Patient"),
		)
	if doc.subject != expected:
		frappe.throw(
			_("{0} is the {1} on this admission: set Check Of to {2}.").format(
				doc.patient, _(expected).lower(), _(expected)
			),
			common.MCHRecordError,
			title=_("Wrong Subject"),
		)
	doc.facility = adm.facility  # DEFECT-MCH-1: never the client's value
	doc.encounter = None  # a ward check has no outpatient consultation


def _set_mother(doc, pnc_date) -> None:
	check_female(doc.patient, _("A mother's PNC check"))
	doc.mother = doc.patient
	delivery = _latest_delivery(doc.patient)
	doc.delivery_record = delivery.name if delivery else None
	doc.days_after_delivery = 0
	if delivery:
		delivered = getdate(delivery.delivered_at)
		if pnc_date < delivered:
			frappe.throw(
				_("The PNC date cannot be before the delivery ({0}, {1}).").format(
					_fmt_date(delivered), delivery.name
				),
				ImplausibleANCValueError,
				title=_("Check the dates"),
			)
		doc.days_after_delivery = date_diff(pnc_date, delivered)
	_clear(doc, BABY_FIELDS)
	check_ranges(doc, MOTHER_RANGES, ("m_bp_systolic", "m_bp_diastolic"))


def _set_baby(doc, pnc_date) -> None:
	patient = frappe.db.get_value("Patient", doc.patient, ["dob", PATIENT_MOTHER_FIELD], as_dict=True)
	dob = getdate(patient.dob) if patient and patient.dob else None
	if not dob:
		frappe.throw(
			_("Record the baby's date of birth on {0} first.").format(doc.patient),
			common.MCHRecordError,
			title=_("Missing Value"),
		)
	if pnc_date < dob:
		frappe.throw(
			_("The PNC date cannot be before the baby's birth ({0}).").format(_fmt_date(dob)),
			ImplausibleANCValueError,
			title=_("Check the dates"),
		)
	if getdate(add_years(dob, 1)) <= pnc_date:
		frappe.throw(
			_("A baby's PNC check is for a baby under 1 year; {0} was born on {1}.").format(
				doc.patient, _fmt_date(dob)
			),
			common.MCHRecordError,
			title=_("Wrong Patient"),
		)
	doc.mother = patient.get(PATIENT_MOTHER_FIELD) or None
	doc.delivery_record = _baby_delivery(doc.patient)
	doc.days_after_delivery = date_diff(pnc_date, dob)
	_clear(doc, MOTHER_FIELDS)
	check_ranges(doc, BABY_RANGES)


def _not_archived(alias: str) -> str:
	if frappe.db.has_column(DELIVERY_RECORD_DOCTYPE, ARCHIVED_FIELD):
		return f"IFNULL({alias}.`{ARCHIVED_FIELD}`, 0) = 0"
	return "1 = 1"


def _latest_delivery(mother: str):
	"""The mother's latest submitted, non-archived Delivery Record ({name, delivered_at}) or None."""
	rows = frappe.db.sql(
		f"""SELECT dr.name, dr.delivered_at FROM `tab{DELIVERY_RECORD_DOCTYPE}` dr
		WHERE dr.patient = %(mother)s AND dr.docstatus = 1 AND {_not_archived("dr")}
		ORDER BY dr.delivered_at DESC, dr.creation DESC LIMIT 1""",
		{"mother": mother},
		as_dict=True,
	)
	return rows[0] if rows else None


def _baby_delivery(baby: str) -> str | None:
	"""The submitted, non-archived Delivery Record with a babies row for `baby` (newborn_patient)."""
	rows = frappe.db.sql(
		f"""SELECT dr.name FROM `tab{DELIVERY_RECORD_DOCTYPE}` dr
		JOIN `tab{DELIVERY_BABY_DOCTYPE}` b ON b.parent = dr.name
			AND b.parenttype = '{DELIVERY_RECORD_DOCTYPE}' AND b.parentfield = 'babies'
		WHERE b.newborn_patient = %(baby)s AND dr.docstatus = 1 AND {_not_archived("dr")}
		ORDER BY dr.delivered_at DESC, dr.creation DESC LIMIT 1""",
		{"baby": baby},
	)
	return rows[0][0] if rows else None


def visit_before_submit(doc) -> None:
	common.before_submit(doc)


def visit_before_cancel(doc) -> None:
	common.refuse_cancel(doc)


def visit_before_update_after_submit(doc) -> None:
	common.guard_after_submit(doc)
