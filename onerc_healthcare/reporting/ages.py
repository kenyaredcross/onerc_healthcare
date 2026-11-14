# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Age bands for MOH reports (W0 contract; R1 may add helpers but never changes these signatures).

Age is in completed months on the event date (encounter date or visit date). The SQL expression and the
Python oracle give the same answer, including month ends and 29 February birthdays. Like MariaDB's
TIMESTAMPDIFF(MONTH, ...), a month counts only once the birth day of month is reached: a 29 Feb 2024
birth is 11 months old on 28 Feb 2025 and 12 months on 1 Mar 2025 (dateutil's relativedelta would say 12
on 28 Feb, so it is not used).

A patient with no date of birth falls in `UNKNOWN_BAND`. Estimated dates of birth are used as they are
(DECISIONS: DOB convention); they are not excluded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import frappe
from frappe.utils import getdate

UNKNOWN_BAND = "Age unknown"
UNDER_FIVE_MONTHS = 60  # 705A: under 5 years; 705B: 5 years and over


@dataclass(frozen=True)
class AgeBand:
	label: str
	min_months: int  # inclusive
	max_months: int | None = None  # exclusive; None: no upper bound

	def contains(self, months: int) -> bool:
		return months >= self.min_months and (self.max_months is None or months < self.max_months)


def age_in_months(dob, on_date) -> int | None:
	"""Completed months between `dob` and `on_date`; None when dob is missing or after on_date."""
	if not dob or not on_date:
		return None
	dob, on_date = getdate(dob), getdate(on_date)
	if dob > on_date:
		return None
	months = (on_date.year - dob.year) * 12 + on_date.month - dob.month
	return months - 1 if on_date.day < dob.day else months


def band_for(months: int | None, bands: tuple[AgeBand, ...] | list[AgeBand]) -> str:
	"""Label of the first band containing `months`, else UNKNOWN_BAND."""
	if months is None:
		return UNKNOWN_BAND
	for band in bands:
		if band.contains(months):
			return band.label
	return UNKNOWN_BAND


def age_months_sql(dob_column: str, date_column: str) -> str:
	"""SQL for completed months (NULL when dob is NULL). Columns are trusted identifiers, e.g. `pat.dob`."""
	return f"TIMESTAMPDIFF(MONTH, {dob_column}, {date_column})"


def band_case_sql(dob_column: str, date_column: str, bands: tuple[AgeBand, ...] | list[AgeBand]) -> str:
	"""SQL CASE giving the band label for each row; same result as `band_for(age_in_months(...))`."""
	months = age_months_sql(dob_column, date_column)
	parts = [
		f"WHEN {dob_column} IS NULL OR {dob_column} > {date_column} THEN {frappe.db.escape(UNKNOWN_BAND)}"
	]
	for band in bands:
		upper = f" AND {months} < {int(band.max_months)}" if band.max_months is not None else ""
		parts.append(f"WHEN {months} >= {int(band.min_months)}{upper} THEN {frappe.db.escape(band.label)}")
	return f"(CASE {' '.join(parts)} ELSE {frappe.db.escape(UNKNOWN_BAND)} END)"


def as_date(value) -> date:
	return getdate(value)
