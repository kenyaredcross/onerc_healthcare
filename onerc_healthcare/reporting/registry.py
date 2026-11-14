# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Data-element sources for MOH reports (W0 contract). Builders implement the evaluators; never rename them.

A MOH Report has data elements. Each element names a `source_type` (a key of `SOURCE_TYPES`) and a JSON
`rule` whose format belongs to that source. R1's engine groups the report's Available elements by source
type and calls the source's evaluator once:

    evaluate(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]

CellKey is ``(element_code, age_band_label, sex)``. Use "" for a dimension the element is not
disaggregated by (``ElementSpec.disaggregation``). Missing cells count as 0. An evaluator must:

- apply the base filters in `reporting.filters` (facility, period, archived, merged, excluded closures and
  the ``krcs_report_exclusions`` hook);
- be read-only, run as SQL aggregates where possible, and never depend on the user's permissions;
- record data problems with ``ctx.add_exception`` (for example an unmapped ICD-11 code or a missing
  date of birth). The run stores them and shows them to the HIS Officer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import frappe

from onerc_healthcare.reporting.ages import AgeBand

CellKey = tuple[str, str, str]

DISAGGREGATION_NONE = "None"
DISAGGREGATION_AGE = "Age Band"
DISAGGREGATION_SEX = "Sex"
DISAGGREGATION_AGE_SEX = "Age Band and Sex"
DISAGGREGATIONS = (DISAGGREGATION_NONE, DISAGGREGATION_AGE, DISAGGREGATION_SEX, DISAGGREGATION_AGE_SEX)

AVAILABILITY_AVAILABLE = "Available"
AVAILABILITY_GAP = "Gap"  # the EMR does not capture this yet: shown blank with a note, never computed

# source type -> dotted path of its evaluator. Owners: R2 diagnosis; R4 everything else.
SOURCE_TYPES: dict[str, str] = {
	"Diagnosis Category": "onerc_healthcare.reporting.diagnosis.evaluate",  # R2 (705A/705B)
	"Visit Attendance": "onerc_healthcare.reporting.workload.evaluate_attendance",  # R4
	"Laboratory Tests": "onerc_healthcare.reporting.workload.evaluate_lab_tests",  # R4
	"Prescriptions": "onerc_healthcare.reporting.workload.evaluate_prescriptions",  # R4
	"Dispensing": "onerc_healthcare.reporting.workload.evaluate_dispensing",  # R4
	"Referrals": "onerc_healthcare.reporting.workload.evaluate_referrals",  # R4
	"Visit Outcome": "onerc_healthcare.reporting.workload.evaluate_outcomes",  # R4
	"Service Delivered": "onerc_healthcare.reporting.workload.evaluate_services",  # R4
	"Community Activity": "onerc_healthcare.reporting.workload.evaluate_community",  # R4
}


@dataclass(frozen=True)
class ElementSpec:
	code: str
	label: str
	source_type: str
	rule: dict
	disaggregation: str = DISAGGREGATION_NONE


@dataclass
class SourceContext:
	report: str
	from_date: date
	to_date: date
	facility: str | None = None  # Healthcare Service Unit (facility group); None: every facility
	company: str | None = None
	age_bands: tuple[AgeBand, ...] = ()
	exceptions: list[dict] = field(default_factory=list)

	def add_exception(
		self,
		kind: str,
		detail: str,
		reference_doctype: str | None = None,
		reference_name: str | None = None,
		element: str | None = None,
	) -> None:
		"""A data problem found while counting. Never put patient names here; use record IDs."""
		self.exceptions.append(
			{
				"kind": kind,
				"detail": detail,
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"element": element,
			}
		)


def get_evaluator(source_type: str):
	path = SOURCE_TYPES.get(source_type)
	if not path:
		frappe.throw(frappe._("Unknown MOH data source {0}.").format(source_type))
	return frappe.get_attr(path)


def cell_key(element: ElementSpec, band: str | None = None, sex: str | None = None) -> CellKey:
	"""Cell key with the dimensions the element is not disaggregated by blanked."""
	by_age = element.disaggregation in (DISAGGREGATION_AGE, DISAGGREGATION_AGE_SEX)
	by_sex = element.disaggregation in (DISAGGREGATION_SEX, DISAGGREGATION_AGE_SEX)
	return (element.code, (band or "") if by_age else "", (sex or "") if by_sex else "")
