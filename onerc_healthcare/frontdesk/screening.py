# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Entrance screening (FR-42, BR-13, SDD §5.11).

Parameters are configured by the Medical In-Charge (Screening Parameter). Each Screening Record row is a
**snapshot** of its parameter (label, unit, thresholds, plausible range, options), taken while the record is
a draft and frozen at submit, so later changes to a parameter never alter a submitted record.

Row flags:

| Type    | Flag                                                                                   |
|---------|----------------------------------------------------------------------------------------|
| Numeric | Low below `low_threshold`, High above `high_threshold`, else Normal. A value outside the |
|         | plausible range is refused as an entry error. A limit of 0 means "not set".            |
| Select  | Abnormal when the value is one of `abnormal_options`, else Normal; must be an option.   |
| Check   | Abnormal when answered Yes, Normal when No.                                            |

The record outcome is "Outside Expected Range" when any row is flagged (Low, High, Abnormal), else
"Normal". Rows without a value carry no flag; required parameters must have a value before submit.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from onerc_healthcare.constants import SCREENING_DOCTYPE
from onerc_healthcare.constants import SCREENING_PARAMETER_DOCTYPE as PARAMETER_DOCTYPE

TYPE_NUMERIC = "Numeric"
TYPE_SELECT = "Select"
TYPE_CHECK = "Check"
PARAM_TYPES = (TYPE_NUMERIC, TYPE_SELECT, TYPE_CHECK)

FLAG_NORMAL = "Normal"
FLAG_LOW = "Low"
FLAG_HIGH = "High"
FLAG_ABNORMAL = "Abnormal"
OUT_OF_RANGE_FLAGS = (FLAG_LOW, FLAG_HIGH, FLAG_ABNORMAL)

OUTCOME_NORMAL = "Normal"
OUTCOME_OUT_OF_RANGE = "Outside Expected Range"

CHECK_YES = ("yes", "y", "1", "true", "checked")
CHECK_NO = ("no", "n", "0", "false", "unchecked")

# Fields copied from the parameter into each result row.
SNAPSHOT_FIELDS = (
	"label",
	"param_type",
	"unit",
	"required",
	"low_threshold",
	"high_threshold",
	"plausible_min",
	"plausible_max",
	"options",
	"abnormal_options",
)

# Default parameter (decision 2026-11-13; provisional until the Medical In-Charge confirms it).
DEFAULT_PARAMETERS = (
	{
		"label": "Temperature",
		"param_type": TYPE_NUMERIC,
		"unit": "°C",
		"plausible_min": 25,
		"plausible_max": 45,
		"high_threshold": 37.5,
		"required": 1,
		"active": 1,
		"display_order": 1,
		"description": "PROVISIONAL: flagged above 37.5 °C until the Medical In-Charge confirms the threshold.",
	},
)


class ImplausibleValueError(frappe.ValidationError):
	"""A screening value outside the parameter's plausible range or not one of its options."""


def lines(text: str | None) -> list[str]:
	return [s.strip() for s in (text or "").splitlines() if s.strip()]


def active_parameters() -> list[dict]:
	return frappe.get_all(
		PARAMETER_DOCTYPE,
		filters={"active": 1},
		fields=["name", *SNAPSHOT_FIELDS],
		order_by="display_order asc, label asc",
	)


@frappe.whitelist()
def get_active_parameters() -> list[dict]:
	"""Active screening parameters, in display order (Screening Record form)."""
	frappe.has_permission(SCREENING_DOCTYPE, "read", throw=True)
	return active_parameters()


def snapshot(row, parameter: dict) -> None:
	for field in SNAPSHOT_FIELDS:
		row.set(field, parameter.get(field))


def evaluate_row(row) -> str | None:
	"""Validate a result row's value against its snapshot; set `numeric_value` and return the flag."""
	value = (row.value or "").strip()
	row.value = value or None
	row.numeric_value = 0
	if not value:
		return None
	label = row.label or row.parameter
	if row.param_type == TYPE_NUMERIC:
		try:
			number = float(value.replace(",", "."))
		except ValueError:
			frappe.throw(_("{0}: enter a number.").format(label), ImplausibleValueError)
		lo, hi = flt(row.plausible_min), flt(row.plausible_max)
		if (lo and number < lo) or (hi and number > hi):
			frappe.throw(
				_("{0}: {1} {2} is not plausible (expected between {3} and {4}). Check the entry.").format(
					label, value, row.unit or "", lo or "-", hi or "-"
				),
				ImplausibleValueError,
				title=_("Implausible Value"),
			)
		row.numeric_value = number
		if flt(row.low_threshold) and number < flt(row.low_threshold):
			return FLAG_LOW
		if flt(row.high_threshold) and number > flt(row.high_threshold):
			return FLAG_HIGH
		return FLAG_NORMAL
	if row.param_type == TYPE_SELECT:
		options = lines(row.options)
		if options and value not in options:
			frappe.throw(
				_("{0}: choose one of {1}.").format(label, ", ".join(options)), ImplausibleValueError
			)
		return FLAG_ABNORMAL if value in lines(row.abnormal_options) else FLAG_NORMAL
	if row.param_type == TYPE_CHECK:
		lowered = value.lower()
		if lowered in CHECK_YES:
			row.value = "Yes"
			return FLAG_ABNORMAL
		if lowered in CHECK_NO:
			row.value = "No"
			return FLAG_NORMAL
		frappe.throw(_("{0}: answer Yes or No.").format(label), ImplausibleValueError)
	frappe.throw(_("{0}: unknown parameter type {1}.").format(label, row.param_type))


def outcome_of(flags: list[str | None]) -> str:
	return OUTCOME_OUT_OF_RANGE if any(f in OUT_OF_RANGE_FLAGS for f in flags) else OUTCOME_NORMAL


def latest_screening_for_visit(visit: str) -> str | None:
	"""Latest submitted Screening Record of a Patient Visit (shown on the visit, SDD §5.11)."""
	rows = frappe.get_all(
		SCREENING_DOCTYPE,
		filters={"visit": visit, "docstatus": 1},
		pluck="name",
		order_by="screened_at desc, creation desc",
		limit=1,
	)
	return rows[0] if rows else None


def latest_screening_for_visitor(visitor_record: str) -> str | None:
	rows = frappe.get_all(
		SCREENING_DOCTYPE,
		filters={"visitor_record": visitor_record, "docstatus": 1},
		pluck="name",
		order_by="screened_at desc, creation desc",
		limit=1,
	)
	return rows[0] if rows else None


def seed_default_parameters() -> list[str]:
	"""Create the default parameters that do not exist yet (never edits an existing one)."""
	created = []
	for values in DEFAULT_PARAMETERS:
		if frappe.db.exists(PARAMETER_DOCTYPE, values["label"]):
			continue
		frappe.get_doc({"doctype": PARAMETER_DOCTYPE, **values}).insert(ignore_permissions=True)
		created.append(values["label"])
	return created
