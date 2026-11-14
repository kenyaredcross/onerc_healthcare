# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Date of birth: exact, or estimated from an age (Phase 3 user decision 2026-11-13; MOH age bands).

Many refugees have no documented date of birth, so a new patient needs **either** an exact `dob` **or**
an estimated age (years 0-120, months 0-11, not both zero).

**Convention (W0 contract).** When only an age is known, `dob` = the date the age was given
(`krcs_dob_estimated_on`) minus the years and months, on the same day of the month, clamped at the end
of a shorter month (29 February minus one year is 28 February), and the record is flagged
`krcs_dob_estimated`. Reports then use `dob` like any other.

**Which value wins** (`resolve_mode`), comparing with the saved record:

| Saved now | Result |
|---|---|
| no age (years and months 0) | exact `dob`, or missing |
| an age, no `dob` | estimated |
| an age with the flag ticked (the form ticks it when the age is typed) | estimated, unless only `dob` changed on an existing record: that is an exact DOB entered later, which clears the flag |
| an age and a `dob`, flag not ticked | on an existing record whichever changed wins; on a new record both is a conflict |

Clearing the age of an estimated record while leaving its computed `dob` is refused: the date was never
known (enter the exact DOB, or keep an age).

Estimated: `dob` is recomputed from the **original** `krcs_dob_estimated_on` (set once, today by default),
so correcting the age never moves the reference date. Exact: the flag, the age and the date are cleared.

**Missing DOB.** Required on insert. On an existing record it is required only when the DOB fields are
being changed (DOB cleared, age or flag edited), so patients registered before this rule (without a DOB)
are never blocked on unrelated saves (identifier links, merges, name corrections).
"""

from __future__ import annotations

from datetime import date

import frappe
from dateutil.relativedelta import relativedelta
from frappe import _
from frappe.utils import cint, getdate, today

from onerc_healthcare.constants import (
	PATIENT_DOB_ESTIMATED_FIELD,
	PATIENT_DOB_ESTIMATED_ON_FIELD,
	PATIENT_ESTIMATED_AGE_MONTHS_FIELD,
	PATIENT_ESTIMATED_AGE_YEARS_FIELD,
)

MAX_AGE_YEARS = 120
MAX_AGE_MONTHS = 11

MODE_EXACT = "exact"
MODE_ESTIMATED = "estimated"
MODE_MISSING = "missing"

YEARS = PATIENT_ESTIMATED_AGE_YEARS_FIELD
MONTHS = PATIENT_ESTIMATED_AGE_MONTHS_FIELD
FLAG = PATIENT_DOB_ESTIMATED_FIELD
ESTIMATED_ON = PATIENT_DOB_ESTIMATED_ON_FIELD


class DateOfBirthRequiredError(frappe.ValidationError):
	"""A new patient (or a DOB edit) without an exact date of birth or an estimated age."""


class EstimatedAgeError(frappe.ValidationError):
	"""The estimated age is out of range, or conflicts with an exact date of birth."""


def estimate_dob(estimated_on, years: int, months: int = 0) -> date:
	"""`estimated_on` minus `years` and `months`, same day of month, clamped at month end."""
	return getdate(estimated_on) - relativedelta(years=cint(years), months=cint(months))


def validate_age(years, months) -> tuple[int, int]:
	"""(years, months) as ints; raises EstimatedAgeError when out of range."""
	y, m = cint(years), cint(months)
	if y < 0 or y > MAX_AGE_YEARS:
		frappe.throw(
			_("Estimated age in years must be between 0 and {0}.").format(MAX_AGE_YEARS),
			EstimatedAgeError,
			title=_("Invalid Age"),
		)
	if m < 0 or m > MAX_AGE_MONTHS:
		frappe.throw(
			_("Estimated age in months must be between 0 and {0}; enter whole years in Years.").format(
				MAX_AGE_MONTHS
			),
			EstimatedAgeError,
			title=_("Invalid Age"),
		)
	return y, m


def _date(value) -> date | None:
	return getdate(value) if value else None


def resolve_mode(doc, before=None) -> str:
	"""MODE_EXACT, MODE_ESTIMATED or MODE_MISSING for `doc` (see the module table)."""
	years, months = cint(doc.get(YEARS)), cint(doc.get(MONTHS))
	age_given = bool(years or months)
	dob = _date(doc.get("dob"))
	if not age_given:
		return MODE_EXACT if dob else MODE_MISSING
	if not dob:
		return MODE_ESTIMATED
	dob_changed = age_changed = True
	if before is not None:
		dob_changed = dob != _date(before.get("dob"))
		age_changed = (years, months) != (cint(before.get(YEARS)), cint(before.get(MONTHS)))
	if cint(doc.get(FLAG)):
		if before is not None and dob_changed and not age_changed:
			return MODE_EXACT  # an exact DOB entered later
		return MODE_ESTIMATED
	if before is not None and age_changed and not dob_changed:
		return MODE_ESTIMATED
	if before is not None and dob_changed and not age_changed:
		return MODE_EXACT
	if before is not None and not dob_changed and not age_changed:
		return MODE_ESTIMATED if cint(before.get(FLAG)) else MODE_EXACT
	frappe.throw(
		_("Enter either the exact date of birth or the estimated age, not both."),
		EstimatedAgeError,
		title=_("Date of Birth"),
	)


def _dob_fields_changed(doc, before) -> bool:
	return (
		_date(doc.get("dob")) != _date(before.get("dob"))
		or cint(doc.get(YEARS)) != cint(before.get(YEARS))
		or cint(doc.get(MONTHS)) != cint(before.get(MONTHS))
		or cint(doc.get(FLAG)) != cint(before.get(FLAG))
	)


def apply_dob_rules(doc) -> None:
	"""Patient `validate`: set or clear the estimate, and require a DOB on insert / DOB edits."""
	before = None if doc.is_new() else doc.get_doc_before_save()
	validate_age(doc.get(YEARS), doc.get(MONTHS))
	# Compared with the saved record before the fields are normalised below (DEFECT-P3-8: a flag ticked
	# alone on a patient without a DOB is an edit of the DOB fields).
	fields_changed = before is None or _dob_fields_changed(doc, before)
	mode = resolve_mode(doc, before)
	if mode == MODE_EXACT and _estimate_abandoned(doc, before):
		# DEFECT-P3-7: the age was cleared but the DOB is still the computed estimate, not a known date.
		_require_dob()

	if mode == MODE_ESTIMATED:
		years, months = cint(doc.get(YEARS)), cint(doc.get(MONTHS))
		on = _estimated_on(doc, before)
		doc.set(ESTIMATED_ON, on)
		doc.set(FLAG, 1)
		doc.dob = estimate_dob(on, years, months)
		return

	doc.set(FLAG, 0)
	doc.set(YEARS, 0)
	doc.set(MONTHS, 0)
	doc.set(ESTIMATED_ON, None)
	if mode == MODE_EXACT:
		return
	if fields_changed:
		_require_dob()


def _estimate_abandoned(doc, before) -> bool:
	"""The saved record was estimated, the age is now empty and the DOB was not changed."""
	return (
		before is not None
		and bool(cint(before.get(FLAG)))
		and not (cint(doc.get(YEARS)) or cint(doc.get(MONTHS)))
		and _date(doc.get("dob")) == _date(before.get("dob"))
	)


def _require_dob() -> None:
	frappe.throw(
		_(
			"Enter the date of birth. If it is not known, enter the estimated age in years (and months"
			" for a young child) instead."
		),
		DateOfBirthRequiredError,
		title=_("Date of Birth Required"),
	)


def _estimated_on(doc, before) -> date:
	"""The date the age was given: kept from the saved record; on a new estimate the supplied date (not
	in the future, e.g. back-entry of a paper registration) or today."""
	if before is not None and cint(before.get(FLAG)) and before.get(ESTIMATED_ON):
		return getdate(before.get(ESTIMATED_ON))
	given = _date(doc.get(ESTIMATED_ON))
	now = getdate(today())
	if before is None and given and given <= now:
		return given
	return now
