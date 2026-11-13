# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Vital Signs doc_events (wired in hooks.py): triage findings and plausibility checks (FR-9, NFR-10)."""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import flt

from onerc_healthcare.constants import VISIT_CLOSED_STATUSES, VISIT_DOCTYPE, VISIT_STATUS_REGISTERED
from onerc_healthcare.visit import engine
from onerc_healthcare.visit.patients import assert_patient_active


class ImplausibleVitalSignsError(frappe.ValidationError):
	"""A vital sign is not a number or is outside the physiologically plausible range."""


@dataclass(frozen=True)
class VitalRange:
	fieldname: str
	unit: str
	low: float
	high: float
	example: str
	whole_number: bool = False


# Plausible (not normal) ranges: wide enough for neonates and critically ill adults, narrow enough to
# catch typing errors such as a missing decimal point or a value in the wrong unit.
TEXT_VITALS = (
	VitalRange("temperature", "°C", 25, 45, "36.8"),
	VitalRange("pulse", "beats/min", 20, 250, "72", whole_number=True),
	VitalRange("respiratory_rate", "breaths/min", 4, 100, "16", whole_number=True),
	VitalRange("bp_systolic", "mmHg", 40, 300, "120", whole_number=True),
	VitalRange("bp_diastolic", "mmHg", 20, 200, "80", whole_number=True),
)
NUMERIC_VITALS = (
	VitalRange("weight", "kg", 0.3, 350, "62.5"),
	VitalRange("height", "m", 0.2, 2.5, "1.65"),
)
# Marley v16 has no SpO2 field; checked when a field with one of these names exists.
SPO2_FIELDNAMES = ("spo2", "krcs_spo2", "oxygen_saturation")
SPO2 = (50, 100, "97")


def vital_signs_validate(doc, method=None) -> None:
	if doc.patient:
		assert_patient_active(doc.patient)
	errors = check_vital_signs(doc)
	if errors:
		frappe.throw(errors, ImplausibleVitalSignsError, title=_("Check the vital signs"), as_list=True)
	if doc.bp_systolic and doc.bp_diastolic:
		doc.bp = f"{doc.bp_systolic}/{doc.bp_diastolic} mmHg"
	validate_visit_link(doc)


def check_vital_signs(doc) -> list[str]:
	"""Validate and normalise the vital sign values on `doc`. Returns a list of error messages."""
	errors: list[str] = []
	label = doc.meta.get_label

	for spec in TEXT_VITALS:
		value = _parse_text(doc, spec.fieldname)
		if value is _BLANK:
			continue
		if value is None:
			errors.append(
				_("{0} must be a number in {1}, for example {2}; got {3}.").format(
					frappe.bold(_(label(spec.fieldname))),
					spec.unit,
					spec.example,
					frappe.bold(doc.get(spec.fieldname)),
				)
			)
			continue
		if spec.whole_number and value != int(value):
			errors.append(
				_("{0} must be a whole number of {1}, for example {2}.").format(
					frappe.bold(_(label(spec.fieldname))), spec.unit, spec.example
				)
			)
			continue
		message = _range_error(doc, spec, value)
		if message:
			errors.append(message)
			continue
		doc.set(spec.fieldname, _format(value, spec.whole_number))

	for spec in NUMERIC_VITALS:
		if not doc.meta.has_field(spec.fieldname) or not flt(doc.get(spec.fieldname)):
			continue
		value = flt(doc.get(spec.fieldname))
		message = _range_error(doc, spec, value)
		if message:
			errors.append(message)

	for fieldname in SPO2_FIELDNAMES:
		if doc.meta.has_field(fieldname) and doc.get(fieldname) not in (None, ""):
			low, high, example = SPO2
			value = _to_number(doc.get(fieldname))
			if value is None or not low <= value <= high:
				errors.append(
					_("{0} must be a percentage between {1} and {2}, for example {3}.").format(
						frappe.bold(_(label(fieldname))), low, high, example
					)
				)

	errors.extend(_check_blood_pressure(doc))
	return errors


def _range_error(doc, spec: VitalRange, value: float) -> str | None:
	if spec.low <= value <= spec.high:
		return None
	label = frappe.bold(_(doc.meta.get_label(spec.fieldname)))
	if spec.fieldname == "temperature" and 77 <= value <= 113:
		return _("{0} of {1} looks like degrees Fahrenheit. Enter it in °C, for example 36.8.").format(
			label, value
		)
	if spec.fieldname == "height" and 20 <= value <= 250:
		return _("{0} is recorded in metres, not centimetres: enter {1} as {2}.").format(
			label, value, round(value / 100, 2)
		)
	return _("{0} of {1} {2} is not plausible. Expected between {3} and {4} {2}.").format(
		label, value, spec.unit, spec.low, spec.high
	)


def _check_blood_pressure(doc) -> list[str]:
	sys_, dia = _to_number(doc.bp_systolic), _to_number(doc.bp_diastolic)
	has_sys, has_dia = bool((doc.bp_systolic or "").strip()), bool((doc.bp_diastolic or "").strip())
	if has_sys != has_dia:
		return [_("Enter both systolic and diastolic blood pressure, or neither.")]
	if sys_ is not None and dia is not None and sys_ <= dia:
		return [
			_("Systolic blood pressure ({0}) must be higher than diastolic ({1}).").format(
				_format(sys_, True), _format(dia, True)
			)
		]
	return []


_BLANK = object()


def _parse_text(doc, fieldname: str):
	"""Number in a Data vital field: _BLANK when empty, None when not a number."""
	raw = doc.get(fieldname)
	if raw is None or str(raw).strip() == "":
		return _BLANK
	return _to_number(raw)


def _to_number(raw) -> float | None:
	if raw is None:
		return None
	text = str(raw).strip().replace(",", ".")
	if not text:
		return None
	try:
		value = float(text)
	except ValueError:
		return None
	if value != value or value in (float("inf"), float("-inf")):
		return None
	return value


def _format(value: float, whole: bool) -> str:
	if whole:
		return str(int(value))
	return f"{value:g}"


def validate_visit_link(doc) -> None:
	"""The linked visit must belong to the same patient and still be open."""
	if not doc.get("krcs_visit"):
		return
	visit = frappe.db.get_value(VISIT_DOCTYPE, doc.krcs_visit, ["patient", "status", "company"], as_dict=True)
	if not visit:
		frappe.throw(_("Patient Visit {0} does not exist.").format(doc.krcs_visit))
	if visit.patient != doc.patient:
		frappe.throw(
			_("Patient Visit {0} belongs to a different patient.").format(frappe.bold(doc.krcs_visit)),
			title=_("Wrong Visit"),
		)
	if doc.docstatus == 0 and visit.status in VISIT_CLOSED_STATUSES:
		frappe.throw(
			_("Patient Visit {0} is already {1}.").format(frappe.bold(doc.krcs_visit), _(visit.status)),
			title=_("Visit Closed"),
		)
	if not doc.company and visit.company:
		doc.company = visit.company


def vital_signs_on_submit(doc, method=None) -> None:
	"""Submitting triage vital signs starts triage on a Registered visit; routing stays explicit."""
	if not doc.get("krcs_visit"):
		return
	visit = frappe.db.get_value(VISIT_DOCTYPE, doc.krcs_visit, ["status", "triage_vital_signs"], as_dict=True)
	if not visit:
		return
	if visit.status == VISIT_STATUS_REGISTERED:
		engine.transition(doc.krcs_visit, engine.ACTION_START_TRIAGE, system=True, vital_signs=doc.name)
	elif visit.status not in VISIT_CLOSED_STATUSES and not visit.triage_vital_signs:
		visit_doc = frappe.get_doc(VISIT_DOCTYPE, doc.krcs_visit, for_update=True)
		engine.link_vital_signs(visit_doc, doc.name)
