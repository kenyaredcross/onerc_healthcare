# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MOH Report: one MOH reporting form as coded data elements (FR-29, SDD §5.8 "Open interface").

The name is the report code ("MOH 705A"). Seeds come from ``reporting/data`` (`reporting.definitions`);
the HIS Officer corrects categories, rules and elements here without a code change, and seeds never
overwrite an edited definition. A Blocked definition cannot be generated (`reporting.run`).
"""

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr

from onerc_healthcare.reporting import registry

DIAGNOSIS_SOURCE = "Diagnosis Category"
WORKLOAD_MODULE = "onerc_healthcare.reporting.workload"


class MOHReportDefinitionError(frappe.ValidationError):
	"""The definition is inconsistent (rule JSON, duplicate code, unknown source, bad age band)."""


class MOHReport(Document):
	def validate(self):
		self.report_code = cstr(self.report_code).strip()
		if self.status != "Blocked":
			self.blocked_reason = None
		self.dimensions = self._json_or_none(self.dimensions, _("Dimensions"))
		self.validate_age_bands()
		self.validate_elements()

	def _json_or_none(self, value, label: str, row=None):
		if value in (None, "") or (isinstance(value, str) and not value.strip()):
			return None
		if isinstance(value, dict | list):
			return json.dumps(value)
		try:
			json.loads(value)
		except ValueError as e:
			where = _("row {0}").format(row.idx) if row is not None else ""
			frappe.throw(
				_("{0} {1} is not valid JSON: {2}").format(_(label), where, e),
				MOHReportDefinitionError,
			)
		return value

	def validate_age_bands(self):
		seen = set()
		for band in self.age_bands:
			band.label = cstr(band.label).strip()
			if band.label in seen:
				frappe.throw(
					_("Age band {0} appears twice.").format(frappe.bold(band.label)), MOHReportDefinitionError
				)
			seen.add(band.label)
			if cint(band.min_months) < 0:
				frappe.throw(
					_("Age band {0} starts below 0 months.").format(band.label), MOHReportDefinitionError
				)
			if cint(band.max_months) and cint(band.max_months) <= cint(band.min_months):
				frappe.throw(
					_("Age band {0} must end after it starts.").format(band.label), MOHReportDefinitionError
				)

	def validate_elements(self):
		seen = set()
		for row in self.elements:
			row.code = cstr(row.code).strip()
			if row.code in seen:
				frappe.throw(
					_("Element code {0} appears twice.").format(frappe.bold(row.code)),
					MOHReportDefinitionError,
				)
			seen.add(row.code)
			row.availability = row.availability or registry.AVAILABILITY_AVAILABLE
			row.disaggregation = row.disaggregation or registry.DISAGGREGATION_NONE
			row.rule = self._json_or_none(row.rule, _("Rule"), row)
			if row.rule is not None and not isinstance(json.loads(row.rule), dict):
				frappe.throw(
					_("Rule in row {0} must be a JSON object.").format(row.idx), MOHReportDefinitionError
				)
			if row.disaggregation not in registry.DISAGGREGATIONS:
				frappe.throw(
					_("Row {0}: unknown disaggregation {1}.").format(row.idx, row.disaggregation),
					MOHReportDefinitionError,
				)
			if row.availability != registry.AVAILABILITY_AVAILABLE:
				continue
			if row.source_type not in registry.SOURCE_TYPES:
				frappe.throw(
					_("Element {0}: choose a source ({1}).").format(
						row.code, ", ".join(registry.SOURCE_TYPES)
					),
					MOHReportDefinitionError,
				)
			self.validate_source_rule(row)
			if (
				row.disaggregation in (registry.DISAGGREGATION_AGE, registry.DISAGGREGATION_AGE_SEX)
				and not self.age_bands
			):
				frappe.throw(
					_("Element {0} is disaggregated by age band, but the report has no age bands.").format(
						row.code
					),
					MOHReportDefinitionError,
				)

	def validate_source_rule(self, row):
		"""Refuse a bad rule at save time for the workload sources (R4 `workload.validate_rule`), rather
		than as an "Invalid rule" exception at generation. Checked only when the element's source is
		served by the workload module (so a test or future source pointed elsewhere is not checked
		against workload schemas); Diagnosis Category rules belong to R2 and are not checked here."""
		if row.source_type == DIAGNOSIS_SOURCE:
			return
		if not cstr(registry.SOURCE_TYPES.get(row.source_type)).startswith(WORKLOAD_MODULE + "."):
			return
		try:
			from onerc_healthcare.reporting.workload import validate_rule
		except ImportError:
			return  # workload rules not available on this build
		errors = validate_rule(row.source_type, row.rule, row.disaggregation)
		if errors:
			frappe.throw(
				_("Element {0} ({1}): {2}.").format(row.code, row.source_type, "; ".join(errors)),
				MOHReportDefinitionError,
			)
