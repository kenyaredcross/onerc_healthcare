# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Reporting periods for MOH report runs (FR-30: monthly, quarterly or as required).

- **Monthly:** a whole calendar month, labelled "November 2026".
- **Quarterly:** a whole calendar quarter (Q1 = January to March, as in KHIS/DHIS2; not the KRCS fiscal
  year of D10), labelled "Q4 2026".
- **Custom:** any range, labelled "1 Nov 2026 to 15 Dec 2026" (or a single day, "1 Nov 2026").

A period must start on or before its end, and must not end after today: a month is reported once it is
over.
"""

from __future__ import annotations

import calendar
from datetime import date

import frappe
from frappe import _
from frappe.utils import date_diff, getdate, today

PERIOD_MONTHLY = "Monthly"
PERIOD_QUARTERLY = "Quarterly"
PERIOD_CUSTOM = "Custom"
PERIOD_TYPES = (PERIOD_MONTHLY, PERIOD_QUARTERLY, PERIOD_CUSTOM)


class InvalidPeriodError(frappe.ValidationError):
	"""The reporting period is not valid for its type, reversed, or in the future."""


def month_period(year: int, month: int) -> tuple[date, date]:
	return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def quarter_of(d) -> int:
	return (getdate(d).month - 1) // 3 + 1


def quarter_period(year: int, quarter: int) -> tuple[date, date]:
	if quarter not in (1, 2, 3, 4):
		frappe.throw(_("A quarter is 1 to 4."), InvalidPeriodError)
	first_month = 3 * (quarter - 1) + 1
	return date(year, first_month, 1), month_period(year, first_month + 2)[1]


def period_containing(d, period_type: str) -> tuple[date, date]:
	"""The month or quarter that contains `d` (Custom: the day itself)."""
	d = getdate(d)
	if period_type == PERIOD_MONTHLY:
		return month_period(d.year, d.month)
	if period_type == PERIOD_QUARTERLY:
		return quarter_period(d.year, quarter_of(d))
	return d, d


def previous_period(period_type: str, on_date=None) -> tuple[date, date]:
	"""The last complete month or quarter before `on_date` (default today)."""
	start, _end = period_containing(on_date or today(), period_type)
	return period_containing(date.fromordinal(start.toordinal() - 1), period_type)


def is_whole_month(from_date, to_date) -> bool:
	f, t = getdate(from_date), getdate(to_date)
	return (f, t) == month_period(f.year, f.month)


def is_whole_quarter(from_date, to_date) -> bool:
	f, t = getdate(from_date), getdate(to_date)
	return (f, t) == quarter_period(f.year, quarter_of(f))


def _day(d: date) -> str:
	return f"{d.day} {calendar.month_abbr[d.month]} {d.year}"


def period_label(from_date, to_date, period_type: str | None = None) -> str:
	""" "November 2026", "Q4 2026" or "1 Nov 2026 to 15 Dec 2026"; a whole month or quarter is named
	as such whatever the period type."""
	f, t = getdate(from_date), getdate(to_date)
	if is_whole_month(f, t):
		return f"{calendar.month_name[f.month]} {f.year}"
	if is_whole_quarter(f, t):
		return f"Q{quarter_of(f)} {f.year}"
	return _day(f) if f == t else f"{_day(f)} to {_day(t)}"


def period_days(from_date, to_date) -> int:
	"""Number of days in the period, both ends included."""
	return date_diff(to_date, from_date) + 1


def validate_period(from_date, to_date, period_type: str = PERIOD_CUSTOM, on_date=None) -> None:
	"""Raise InvalidPeriodError unless the period is valid for its type and not in the future."""
	if not from_date or not to_date:
		frappe.throw(_("Enter the period's start and end dates."), InvalidPeriodError)
	f, t = getdate(from_date), getdate(to_date)
	if period_type not in PERIOD_TYPES:
		frappe.throw(_("Unknown period type {0}.").format(period_type), InvalidPeriodError)
	if f > t:
		frappe.throw(_("The period starts after it ends."), InvalidPeriodError)
	if t > getdate(on_date or today()):
		frappe.throw(
			_("The period ends on {0}, after today. Generate it once the period is over.").format(_day(t)),
			InvalidPeriodError,
		)
	if period_type == PERIOD_MONTHLY and not is_whole_month(f, t):
		frappe.throw(
			_("A monthly period runs from the first to the last day of one month."), InvalidPeriodError
		)
	if period_type == PERIOD_QUARTERLY and not is_whole_quarter(f, t):
		frappe.throw(
			_("A quarterly period is a whole calendar quarter (for example 1 October to 31 December)."),
			InvalidPeriodError,
		)
