# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Growth Monitoring and WHO z-scores (MCH-B; docs/MCH_CONTRACT.md §8, CON-17).

**Reference tables:** the WHO Child Growth Standards expanded z-score tables (L, M, S), bundled offline in
`mch/data/who_{wfa,lhfa,wfl,wfh}_{boys,girls}.json`: weight-for-age and length/height-for-age by day
(0-1856), weight-for-length 45-110 cm and weight-for-height 65-120 cm by 0.1 cm. Each file names its
`source` (the WHO URL) and the date it was `retrieved`. Values are never invented or approximated.

**z-score** (`zscore`): ``z = ((y / M) ** L - 1) / (L * S)``. For weight-for-age, -length and -height WHO
corrects the tails beyond ±3 SD (the "SD23" rule: distances beyond 3 SD are measured in units of the
distance between 2 and 3 SD on that side). Ages are looked up by whole day; a length or height between two
0.1 cm rows is interpolated linearly (as WHO Anthro does).

**Growth Monitoring** (submittable, one per visit, no cancel; `mch.common` lifecycle):

| Rule | |
|---|---|
| age | ``age_days`` at ``measured_on``, 0 to below `GROWTH_MAX_AGE_DAYS` (1856); ``measured_on`` not in the future |
| ranges | weight 0.5-40 kg, length/height 35-130 cm, MUAC 5-25 cm, head circumference 25-60 cm; 0 = not measured |
| position | lying before 731 days, standing from 731 days (blank: the expected one). The other position gets WHO's 0.7 cm correction (standing + 0.7 under 2 years, lying - 0.7 from 2 years), noted in ``zscore_note`` |
| z-scores | ``wfa_z``, ``lhfa_z``, ``wflh_z`` (weight-for-length under 731 days, 45-110 cm; weight-for-height from 731 days, 65-120 cm) as strings with 2 decimals; blank when not calculable, with the reason in ``zscore_note`` |
| underweight, stunting | Normal; Moderate below -2; Severe below -3 |
| wasting | SAM: WFLH < -3, MUAC < 11.5 cm at 6-59 months, or any bilateral pitting oedema; MAM: WFLH < -2 or MUAC 11.5-12.4 cm (6-59 months); otherwise Normal; blank with nothing to judge |
| estimated date of birth | copied from the Patient (``dob_estimated``) and noted: age-based z-scores are approximate |
"""

from __future__ import annotations

import json
import math
import os
from functools import lru_cache

import frappe
from frappe import _
from frappe.utils import cint, flt, formatdate, getdate, today

from onerc_healthcare.clinical.utils import has_any_role
from onerc_healthcare.constants import (
	CHILD_MCH_ROLES,
	GROWTH_MAX_AGE_DAYS,
	GROWTH_MONITORING_DOCTYPE,
	GROWTH_NORMAL,
	MCH_READ_ROLES,
	PATIENT_DOB_ESTIMATED_FIELD,
	STUNTING_MODERATE,
	STUNTING_SEVERE,
	UNDERWEIGHT_MODERATE,
	UNDERWEIGHT_SEVERE,
	WASTING_MAM,
	WASTING_SAM,
)
from onerc_healthcare.mch import common
from onerc_healthcare.mch.common import MCHRecordError

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
INDICATORS = ("wfa", "lhfa", "wfl", "wfh")
SD23_INDICATORS = ("wfa", "wfl", "wfh")  # WHO adjusts these beyond ±3 SD; not length/height-for-age
STANDING_FROM_DAYS = 731  # lying (length) before 2 years, standing (height) from 2 years
POSITION_CORRECTION_CM = 0.7
POSITION_LYING = "Lying (length)"
POSITION_STANDING = "Standing (height)"
MUAC_FROM_DAYS = 183  # MUAC classifies acute malnutrition from 6 months (6 x 30.4375 = 182.6 days)
MUAC_SAM_BELOW = 11.5
MUAC_MAM_BELOW = 12.5
OEDEMA_NONE = "None"
CHART_STEP_DAYS = 30
CHART_LINES = {"-3": -3, "-2": -2, "0": 0, "+2": 2, "+3": 3}
# WHO flags z-scores outside these limits as biologically implausible (WHO Anthro): kept, but noted.
IMPLAUSIBLE = {"wfa": (-6, 5), "lhfa": (-6, 6), "wflh": (-5, 5)}


class Range:
	def __init__(self, fieldname: str, unit: str, low: float, high: float):
		self.fieldname, self.unit, self.low, self.high = fieldname, unit, low, high


RANGES = (
	Range("weight_kg", "kg", 0.5, 40),
	Range("length_height_cm", "cm", 35, 130),
	Range("muac_cm", "cm", 5, 25),
	Range("head_circumference_cm", "cm", 25, 60),
)
CHART_READ_ROLES = (*CHILD_MCH_ROLES, *MCH_READ_ROLES)


# --------------------------------------------------------------------------------------- WHO tables


def table_sex(sex: str | None) -> str | None:
	"""'boys' / 'girls' from the Patient's sex (Male/Female), or None."""
	s = (sex or "").strip().lower()
	if s in ("male", "m", "boy", "boys"):
		return "boys"
	if s in ("female", "f", "girl", "girls"):
		return "girls"
	return None


@lru_cache(maxsize=16)
def load_table(indicator: str, sex: str) -> dict:
	"""The WHO file for (indicator, 'boys'/'girls'): {source, retrieved, rows: [[x, L, M, S], ...]}."""
	with open(os.path.join(DATA_DIR, f"who_{indicator}_{sex}.json"), encoding="utf-8") as f:
		return json.load(f)


def lms(indicator: str, sex: str | None, x: float) -> tuple[float, float, float] | None:
	"""L, M, S at `x` (day for wfa/lhfa, cm for wfl/wfh), or None outside the table."""
	sex = table_sex(sex)
	if indicator not in INDICATORS or sex is None or x is None:
		return None
	rows = load_table(indicator, sex)["rows"]
	if indicator in ("wfa", "lhfa"):
		day = round(flt(x))
		first = int(rows[0][0])
		if not first <= day <= int(rows[-1][0]):
			return None
		row = rows[day - first]
		return row[1], row[2], row[3]
	# by 0.1 cm: index = (x - first) * 10, interpolated between neighbours
	pos = round((flt(x) - rows[0][0]) * 10, 6)
	if pos < 0 or pos > len(rows) - 1:
		return None
	lo = math.floor(pos)
	frac = pos - lo
	if frac < 1e-6:
		r = rows[lo]
		return r[1], r[2], r[3]
	a, b = rows[lo], rows[lo + 1]
	return tuple(a[i] + (b[i] - a[i]) * frac for i in (1, 2, 3))


def value_at(L: float, M: float, S: float, z: float) -> float:
	"""The measurement at z-score `z` (inverse LMS)."""
	if L == 0:
		return M * math.exp(S * z)
	return M * (1 + L * S * z) ** (1 / L)


def zscore(indicator: str, sex: str, x: float, measure: float) -> float | None:
	"""WHO LMS z-score. `indicator` is one of "wfa", "lhfa", "wfl", "wfh"; `x` is the age in days (wfa,
	lhfa) or the length/height in cm (wfl, wfh); `measure` is the weight (kg) or length/height (cm).
	None outside the table."""
	if not measure or flt(measure) <= 0:
		return None
	params = lms(indicator, sex, x)
	if params is None:
		return None
	L, M, S = params
	y = flt(measure)
	z = math.log(y / M) / S if L == 0 else ((y / M) ** L - 1) / (L * S)
	if indicator in SD23_INDICATORS:
		if z > 3:
			sd3 = value_at(L, M, S, 3)
			sd23 = sd3 - value_at(L, M, S, 2)
			z = 3 + (y - sd3) / sd23
		elif z < -3:
			sd3 = value_at(L, M, S, -3)
			sd23 = value_at(L, M, S, -2) - sd3
			z = -3 + (y - sd3) / sd23
	return z


def fmt_z(z: float | None) -> str | None:
	if z is None:
		return None
	text = f"{z:.2f}"
	return "0.00" if text == "-0.00" else text


def classify(z: float | None, moderate: str, severe: str) -> str | None:
	if z is None:
		return None
	if z < -3:
		return severe
	if z < -2:
		return moderate
	return GROWTH_NORMAL


def classify_wasting(wflh_z: float | None, muac_cm: float, oedema: str | None, age_days: int) -> str | None:
	"""SAM / MAM / Normal from weight-for-length/height, MUAC (6-59 months) and bilateral oedema; None when
	there is nothing to judge."""
	muac = flt(muac_cm) if flt(muac_cm) > 0 and age_days >= MUAC_FROM_DAYS else None
	has_oedema = bool(oedema) and oedema != OEDEMA_NONE
	if has_oedema or (wflh_z is not None and wflh_z < -3) or (muac is not None and muac < MUAC_SAM_BELOW):
		return WASTING_SAM
	if (wflh_z is not None and wflh_z < -2) or (muac is not None and muac < MUAC_MAM_BELOW):
		return WASTING_MAM
	if wflh_z is None and muac is None:
		return None
	return GROWTH_NORMAL


# --------------------------------------------------------------------------------------- controller


def growth_validate(doc) -> None:
	if common.guard_draft(doc):
		return
	common.check_visit(doc)
	common.guard_one_per_visit(doc)
	common.set_recorded_by(doc)
	child = _child(doc.patient)
	_check_age(doc, child)
	_check_ranges(doc)
	calculate(doc)


def growth_before_submit(doc) -> None:
	common.before_submit(doc)


def growth_before_cancel(doc) -> None:
	common.refuse_cancel(doc)


def growth_before_update_after_submit(doc) -> None:
	common.guard_after_submit(doc)


def _child(patient: str) -> frappe._dict:
	child = frappe.db.get_value(
		"Patient", patient, ["name", "dob", "sex", PATIENT_DOB_ESTIMATED_FIELD], as_dict=True
	)
	if not child:
		frappe.throw(_("Patient {0} does not exist.").format(patient), MCHRecordError)
	if not child.dob:
		frappe.throw(
			_("Record the child's date of birth on the Patient first."),
			MCHRecordError,
			title=_("Missing Date of Birth"),
		)
	return child


def _check_age(doc, child) -> None:
	if not doc.measured_on:
		frappe.throw(_("Enter the date of the measurements."), MCHRecordError, title=_("Missing Value"))
	measured_on = getdate(doc.measured_on)
	if measured_on > getdate(today()):
		frappe.throw(
			_("The date ({0}) is in the future.").format(formatdate(measured_on)),
			MCHRecordError,
			title=_("Check the date"),
		)
	days = common.age_in_days(child.dob, measured_on)
	if days < 0:
		frappe.throw(
			_("The date ({0}) is before the child's date of birth ({1}).").format(
				formatdate(measured_on), formatdate(child.dob)
			),
			MCHRecordError,
			title=_("Check the date"),
		)
	if days >= GROWTH_MAX_AGE_DAYS:
		frappe.throw(
			_(
				"Growth monitoring with the WHO Child Growth Standards is for children under 5 years; the "
				"child was {0} on {1}."
			).format(common.age_label(days), formatdate(measured_on)),
			MCHRecordError,
			title=_("Not a Child Under 5"),
		)
	doc.age_days = days
	doc.age_label = common.age_label(days)
	doc.sex = child.sex
	doc.dob_estimated = cint(child.get(PATIENT_DOB_ESTIMATED_FIELD))


def _check_ranges(doc) -> None:
	errors = []
	for spec in RANGES:
		value = flt(doc.get(spec.fieldname))
		if value < 0 or (value and not spec.low <= value <= spec.high):
			errors.append(
				_(
					"{0} of {1} {2} is not plausible. Expected between {3} and {4} {2} (0 = not measured)."
				).format(
					frappe.bold(_(doc.meta.get_label(spec.fieldname))), value, spec.unit, spec.low, spec.high
				)
			)
	if errors:
		frappe.throw(errors, MCHRecordError, title=_("Check the measurements"), as_list=True)
	oedema = doc.get("oedema") or OEDEMA_NONE
	doc.oedema = oedema
	if not any(flt(doc.get(s.fieldname)) for s in RANGES) and oedema == OEDEMA_NONE:
		frappe.throw(
			_("Enter at least one measurement (weight, length/height, MUAC or head circumference)."),
			MCHRecordError,
			title=_("Missing Value"),
		)
	if not flt(doc.length_height_cm):
		doc.measurement_position = None


def calculate(doc) -> None:
	"""Set the z-scores, the classifications and ``zscore_note`` from the measurements (needs ``age_days``,
	``sex`` and ``dob_estimated`` set)."""
	days = cint(doc.age_days)
	notes = []
	sex = table_sex(doc.sex)
	if sex is None:
		notes.append(_("Z-scores need the child's sex (Male or Female) on the Patient."))
	weight = flt(doc.weight_kg)
	length = flt(doc.length_height_cm)
	standing_expected = days >= STANDING_FROM_DAYS

	# Length/height: lying under 2 years, standing from 2 years; correct the other position (WHO, 0.7 cm).
	adjusted = None
	if length:
		if not doc.measurement_position:
			doc.measurement_position = POSITION_STANDING if standing_expected else POSITION_LYING
		standing = doc.measurement_position == POSITION_STANDING
		adjusted = length
		if standing and not standing_expected:
			adjusted = round(length + POSITION_CORRECTION_CM, 2)
			notes.append(
				_("Measured standing before 2 years: {0} cm added (WHO); {1} cm used as length.").format(
					POSITION_CORRECTION_CM, adjusted
				)
			)
		elif not standing and standing_expected:
			adjusted = round(length - POSITION_CORRECTION_CM, 2)
			notes.append(
				_("Measured lying from 2 years: {0} cm subtracted (WHO); {1} cm used as height.").format(
					POSITION_CORRECTION_CM, adjusted
				)
			)

	wfa = lhfa = wflh = None
	if sex:
		if weight:
			wfa = zscore("wfa", sex, days, weight)
		if adjusted:
			lhfa = zscore("lhfa", sex, days, adjusted)
		if weight and adjusted:
			indicator, low, high = ("wfh", 65, 120) if standing_expected else ("wfl", 45, 110)
			wflh = zscore(indicator, sex, adjusted, weight)
			if wflh is None:
				notes.append(
					_("Weight-for-{0} not calculated: {1} cm is outside the WHO table ({2}-{3} cm).").format(
						_("height") if standing_expected else _("length"), adjusted, low, high
					)
				)
		missing = []
		if not weight:
			missing.append(_("weight-for-age"))
		if not adjusted:
			missing.append(_("length/height-for-age"))
		if not (weight and adjusted):
			missing.append(_("weight-for-length/height"))
		if missing:
			notes.append(_("Not calculated (measurement missing): {0}.").format(", ".join(missing)))

	for key, z in (("wfa", wfa), ("lhfa", lhfa), ("wflh", wflh)):
		low, high = IMPLAUSIBLE[key]
		if z is not None and not low <= z <= high:
			notes.append(
				_("{0} z-score {1} is outside {2} to {3} (WHO): check the measurement.").format(
					key.upper(), fmt_z(z), low, high
				)
			)
	if flt(doc.muac_cm) and days < MUAC_FROM_DAYS:
		notes.append(_("MUAC is used to classify acute malnutrition from 6 months of age."))
	if cint(doc.dob_estimated):
		notes.append(
			_(
				"The date of birth is estimated: weight-for-age and length/height-for-age are approximate "
				"(weight-for-length/height does not depend on age)."
			)
		)

	doc.wfa_z, doc.lhfa_z, doc.wflh_z = fmt_z(wfa), fmt_z(lhfa), fmt_z(wflh)
	# Classify from the stored (2-decimal) values so the label always agrees with the number shown.
	doc.underweight = classify(_parsed(doc.wfa_z), UNDERWEIGHT_MODERATE, UNDERWEIGHT_SEVERE)
	doc.stunting = classify(_parsed(doc.lhfa_z), STUNTING_MODERATE, STUNTING_SEVERE)
	doc.wasting = classify_wasting(_parsed(doc.wflh_z), doc.muac_cm, doc.oedema, days)
	doc.zscore_note = "\n".join(notes) or None


def _parsed(text: str | None) -> float | None:
	return None if text in (None, "") else float(text)


# --------------------------------------------------------------------------------------- chart


@frappe.whitelist(methods=["GET"])
def chart_data(patient: str) -> dict:
	"""Submitted measurements of the child plus the WHO reference curves (-3, -2, 0, +2, +3) for the
	weight-for-age chart on the Patient form:
	{sex, dob, dob_estimated, points: [{age_days, weight_kg, measured_on, name}], curves: {"-3": [[day, kg]]}}.
	The caller reads the Patient and Growth Monitoring records."""
	if not has_any_role(CHART_READ_ROLES):
		frappe.throw(_("You are not allowed to see the growth chart."), frappe.PermissionError)
	frappe.has_permission("Patient", "read", doc=patient, throw=True)
	child = frappe.db.get_value(
		"Patient", patient, ["name", "dob", "sex", PATIENT_DOB_ESTIMATED_FIELD], as_dict=True
	)
	if not child:
		return {}
	filters = {"patient": patient, "docstatus": 1, "weight_kg": (">", 0)}
	if frappe.get_meta(GROWTH_MONITORING_DOCTYPE).has_field(common.ARCHIVED_FIELD):
		filters[common.ARCHIVED_FIELD] = 0
	points = frappe.get_all(
		GROWTH_MONITORING_DOCTYPE,
		filters=filters,
		fields=["name", "age_days", "weight_kg", "measured_on"],
		order_by="measured_on asc, creation asc",
	)
	return {
		"sex": child.sex,
		"dob": child.dob,
		"dob_estimated": cint(child.get(PATIENT_DOB_ESTIMATED_FIELD)),
		"points": [
			{
				"age_days": cint(p.age_days),
				"weight_kg": flt(p.weight_kg),
				"measured_on": p.measured_on,
				"name": p.name,
			}
			for p in points
		],
		"curves": wfa_curves(child.sex),
	}


def wfa_curves(sex: str | None) -> dict[str, list]:
	"""Weight-for-age reference lines every `CHART_STEP_DAYS` days to 1856 days ({} for an unknown sex)."""
	s = table_sex(sex)
	if s is None:
		return {}
	last = int(load_table("wfa", s)["rows"][-1][0])
	days = list(range(0, last + 1, CHART_STEP_DAYS))
	if days[-1] != last:
		days.append(last)
	curves = {}
	for key, z in CHART_LINES.items():
		line = []
		for day in days:
			L, M, S = lms("wfa", s, day)
			line.append([day, round(value_at(L, M, S, z), 3)])
		curves[key] = line
	return curves
