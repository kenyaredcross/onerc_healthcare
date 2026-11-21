# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MOH counts from the MCH records (MCH-A; provisional, OI-5 / OI-8, D62): source type "MCH Records".

MOH 717 B07 (CWC: Immunisation + Growth Monitoring, unit visits), B08 (ANC: ANC Contact, records) and B09
(PNC: PNC Visit, records). The evaluator has the W0 signature ``evaluate(ctx, elements) -> {CellKey: int}``
(`reporting.registry`) and is registered through `workload.evaluate_mch`. Read-only aggregate SQL; never
depends on the user's permissions.

What is counted: submitted (``docstatus = 1``), non-archived (``krcs_archived``) records of patients not
merged into another record (``krcs_merged_into``), at the run's facilities (the record's ``facility``, the
facility group and every unit under it; the company, `workload._facility_conditions`), dated in the period:

| Record | Date field |
|---|---|
| ANC Contact | ``contact_date`` |
| PNC Visit | ``pnc_date`` |
| Immunisation | ``given_on`` |
| Growth Monitoring | ``measured_on`` |

Rule keys (`workload.RULE_SCHEMAS[SOURCE_MCH]`):

- ``record_types`` (required): a list of `MCH_RECORD_DOCTYPES`.
- ``unit``:
  - "records" (default): one per record;
  - "visits": distinct ``visit`` across all the listed record types, so a visit with both an Immunisation
    and a Growth Monitoring counts once. A ward PNC Visit (on a Maternity Admission, no visit) counts as its
    own visit;
  - "patients": distinct patients.
- ``pnc_subjects``: [Mother / Baby], filters PNC Visit only (``record_types`` must include it).

Disaggregation: any (age band and / or sex). Age is the patient's (the mother for ANC, the child or the
baby for CWC and a baby's PNC) at the record's date; sex is the patient's. For unit "visits" a visit is
placed at its earliest record date in the period, and for "patients" a patient at their earliest record
date, so each counts in one cell only.

The ``krcs_report_exclusions`` hook (written for the visit aliases) is not applied, as for the maternity
sources.
"""

from __future__ import annotations

from onerc_healthcare.constants import (
	ANC_CONTACT_DOCTYPE,
	GROWTH_MONITORING_DOCTYPE,
	IMMUNISATION_DOCTYPE,
	PNC_VISIT_DOCTYPE,
)
from onerc_healthcare.reporting import filters, workload
from onerc_healthcare.reporting.registry import CellKey, ElementSpec, SourceContext

SOURCE_MCH = workload.SOURCE_MCH
DATE_FIELDS = {
	ANC_CONTACT_DOCTYPE: "contact_date",
	PNC_VISIT_DOCTYPE: "pnc_date",
	IMMUNISATION_DOCTYPE: "given_on",
	GROWTH_MONITORING_DOCTYPE: "measured_on",
}
UNIT_RECORDS, UNIT_VISITS, UNIT_PATIENTS = "records", "visits", "patients"
EVENT_DATE_SQL = "rec.event_date"


def rule_errors(rule: dict, disaggregation: str | None = None) -> list[str]:
	"""Cross-key checks `workload.validate_rule` adds (keys and values are checked there). Every
	disaggregation is allowed."""
	errors: list[str] = []
	types = rule.get("record_types")
	if "pnc_subjects" in rule and isinstance(types, list) and PNC_VISIT_DOCTYPE not in types:
		errors.append("pnc_subjects applies when record_types includes PNC Visit")
	return errors


def _part(ctx: SourceContext, doctype: str, rule: dict, params: dict) -> str:
	"""One SELECT (visit key, patient, event date) of the records of `doctype` counted in the period."""
	date_col = f"r.`{DATE_FIELDS[doctype]}`"
	conds = [
		"r.docstatus = 1",
		workload._not_archived("r", doctype),
		f"{date_col} BETWEEN %(mch_from)s AND %(mch_to)s",
		*workload._facility_conditions(ctx, "r.facility", params),
	]
	visit_key = "r.visit"
	if doctype == PNC_VISIT_DOCTYPE:
		workload._in(conds, params, "r.subject", "mch_pnc_subjects", rule.get("pnc_subjects"))
		visit_key = "COALESCE(NULLIF(r.visit, ''), r.name)"  # a ward check: its own visit
	return (
		f"SELECT {visit_key} AS visit, r.patient AS patient, {date_col} AS event_date"
		f" FROM `tab{doctype}` r WHERE {' AND '.join(f'({c})' for c in conds)}"
	)


def _from_sql(union: str, unit: str) -> str:
	if unit == UNIT_VISITS:
		inner = (
			"SELECT u.visit, MIN(u.patient) AS patient, MIN(u.event_date) AS event_date"
			f" FROM ({union}) u GROUP BY u.visit"
		)
	elif unit == UNIT_PATIENTS:
		inner = f"SELECT u.patient, MIN(u.event_date) AS event_date FROM ({union}) u GROUP BY u.patient"
	else:
		inner = union
	return f"({inner}) rec JOIN `tabPatient` pat ON pat.name = rec.patient"


def evaluate(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]:
	if workload._source_missing(ctx, tuple(DATE_FIELDS), SOURCE_MCH):
		return {}
	out: dict[CellKey, int] = {}
	for element in elements:
		rule = workload._rule(ctx, element)
		if rule is None:
			continue
		params = {"mch_from": ctx.from_date, "mch_to": ctx.to_date}
		types = list(dict.fromkeys(rule["record_types"]))
		union = " UNION ALL ".join(_part(ctx, doctype, rule, params) for doctype in types)
		out.update(
			workload._aggregate(
				ctx,
				element,
				from_sql=_from_sql(union, rule.get("unit") or UNIT_RECORDS),
				conditions=[f"IFNULL(pat.`{filters.MERGED_INTO_FIELD}`, '') = ''"],
				params=params,
				event_date=EVENT_DATE_SQL,
			)
		)
	return out
