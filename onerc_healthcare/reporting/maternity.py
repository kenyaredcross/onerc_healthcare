# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 5M (M3): maternity sources for MOH 717 sections J (inpatient) and K (maternity). Provisional (OI-5).

Both evaluators have the W0 signature ``evaluate(ctx, elements) -> {CellKey: int}`` (`reporting.registry`)
and are registered through thin wrappers in `reporting.workload` (so the rules are checked when the HIS
Officer saves a definition, like every workload source). Rule keys are checked by
`workload.validate_rule` (``RULE_SCHEMAS``) plus `rule_errors` below. Read-only aggregate SQL; never
depends on the user's permissions.

Base filters. These records have no visit, so `filters.visit_conditions` cannot apply. The same rules are
applied to the Maternity Admission (alias ``adm``) and the mother (``pat``):

- **facility** = the admission's ``facility`` (the facility group and every unit under it), **company** =
  the facility's company (`workload._facility_conditions`);
- **archived** admissions (``krcs_archived``, the correction of a wrong admission) and archived Delivery
  Records never count;
- a mother **merged** into another record (``krcs_merged_into``) does not count: merging moves the
  admission to the surviving record, so anything left on a merged record is an error;
- the ``krcs_report_exclusions`` hook (written for the visit aliases) is not applied, as for Community
  Activity.

Rule formats
------------

**Delivery** (`evaluate_delivery`; event date ``DATE(delivered_at)``; submitted Delivery Records only:
drafts never count, and a Delivery Record is never cancelled)

- ``measure``: "deliveries" (default; one per Delivery Record, whatever the number of babies) |
  "babies" (one per Delivery Baby row).
- ``delivery_modes``: [mode of delivery], e.g. ["Caesarean section"]; omitted: every mode.
- ``born_before_arrival``: "include" (default) | "exclude" | "only" (the ``born_before_arrival`` tick,
  independent of the mode).
- ``baby_outcomes``: [Live birth / Fresh stillbirth / Macerated stillbirth]; measure "babies" only.
- Disaggregation: none, or "Sex" for babies (the baby's sex: Male, Female, else "Other"). No age bands.

**Maternity Admission** (`evaluate_maternity_admission`)

- ``measure`` (required):
  - "admissions": admissions whose ``DATE(admitted_at)`` is in the period;
  - "discharges": Discharged admissions whose ``DATE(discharged_at)`` is in the period (deaths:
    ``outcomes`` ["Died"]);
  - "bed_days": inpatient days spent in the period by every admission that overlaps it (below);
  - "stay_days": the total length of stay of the period's discharges (below), the numerator of the
    average length of stay. MOH cells hold whole numbers, so the average itself is not a cell: it is
    ``stay_days / discharges`` (MOH 717-J05 stays a Gap with that note).
- ``outcomes``: [discharge outcome]; "discharges" and "stay_days" only.
- No disaggregation.

Day counting (midnight census): a stay occupies each date from ``DATE(admitted_at)`` up to the day before
``DATE(discharged_at)``; a stay discharged on its admission day counts one day. An admission not yet
discharged occupies every date up to the period end (but not after today). ``bed_days`` is the number of
those dates inside the period; ``stay_days`` of a discharge is ``max(1, discharge date - admission date)``.
"""

from __future__ import annotations

from frappe.utils import add_days, getdate, today

from onerc_healthcare.constants import (
	ADMISSION_DISCHARGED,
	DELIVERY_BABY_DOCTYPE,
	DELIVERY_RECORD_DOCTYPE,
	MATERNITY_ADMISSION_DOCTYPE,
)
from onerc_healthcare.reporting import filters, workload
from onerc_healthcare.reporting.registry import (
	DISAGGREGATION_AGE,
	DISAGGREGATION_AGE_SEX,
	DISAGGREGATION_NONE,
	DISAGGREGATION_SEX,
	CellKey,
	ElementSpec,
	SourceContext,
)

SOURCE_DELIVERY = "Delivery"
SOURCE_MATERNITY_ADMISSION = "Maternity Admission"

DELIVERY_MEASURES = ("deliveries", "babies")
BBA_CHOICES = ("include", "exclude", "only")
ADMISSION_MEASURES = ("admissions", "discharges", "bed_days", "stay_days")
OUTCOME_MEASURES = ("discharges", "stay_days")

ADMISSION_FROM = f"`tab{MATERNITY_ADMISSION_DOCTYPE}` adm JOIN `tabPatient` pat ON pat.name = adm.patient"
DELIVERY_FROM = (
	f"`tab{DELIVERY_RECORD_DOCTYPE}` dr"
	f" JOIN `tab{MATERNITY_ADMISSION_DOCTYPE}` adm ON adm.name = dr.admission"
	" JOIN `tabPatient` pat ON pat.name = adm.patient"
)
BABY_FROM = (
	f"{DELIVERY_FROM} JOIN `tab{DELIVERY_BABY_DOCTYPE}` baby ON baby.parent = dr.name"
	f" AND baby.parenttype = '{DELIVERY_RECORD_DOCTYPE}' AND baby.parentfield = 'babies'"
)
BABY_SEX_SQL = "(CASE WHEN baby.sex IN ('Male', 'Female') THEN baby.sex ELSE 'Other' END)"
DELIVERY_DATE_SQL = "DATE(dr.delivered_at)"
ADMIT_DATE_SQL = "DATE(adm.admitted_at)"
DISCHARGE_DATE_SQL = "DATE(adm.discharged_at)"
# First date after the stay (exclusive end): the discharge date, at least the day after admission; for a
# stay not yet discharged, the day after the period end or today, whichever is earlier.
STAY_END_SQL = (
	f"(CASE WHEN adm.status = %(m3_discharged)s AND adm.discharged_at IS NOT NULL"
	f" THEN GREATEST({DISCHARGE_DATE_SQL}, DATE_ADD({ADMIT_DATE_SQL}, INTERVAL 1 DAY))"
	" ELSE %(m3_open_end)s END)"
)
BED_DAYS_SQL = (
	f"SUM(GREATEST(0, DATEDIFF(LEAST({STAY_END_SQL}, %(m3_period_end)s),"
	f" GREATEST({ADMIT_DATE_SQL}, %(m3_from)s))))"
)
STAY_DAYS_SQL = f"SUM(GREATEST(1, DATEDIFF({DISCHARGE_DATE_SQL}, {ADMIT_DATE_SQL})))"


def rule_errors(source_type: str, rule: dict, disaggregation: str | None) -> list[str]:
	"""Cross-key checks `workload.validate_rule` adds for the maternity sources (keys and values are
	checked there, through ``RULE_SCHEMAS``)."""
	errors: list[str] = []
	disaggregation = disaggregation or DISAGGREGATION_NONE
	if source_type == SOURCE_DELIVERY:
		babies = rule.get("measure") == "babies"
		if disaggregation in (DISAGGREGATION_AGE, DISAGGREGATION_AGE_SEX):
			errors.append("deliveries are not split by age band: use no age disaggregation")
		if disaggregation == DISAGGREGATION_SEX and not babies:
			errors.append("only babies can be split by sex (measure babies)")
		if "baby_outcomes" in rule and not babies:
			errors.append("baby_outcomes applies to measure babies only")
	elif source_type == SOURCE_MATERNITY_ADMISSION:
		if disaggregation != DISAGGREGATION_NONE:
			errors.append("maternity admission counts are not disaggregated")
		if "outcomes" in rule and rule.get("measure") not in OUTCOME_MEASURES:
			errors.append("outcomes applies to measures discharges and stay_days only")
	return errors


def _base(ctx: SourceContext, params: dict) -> list[str]:
	"""Facility, company, archived admission and merged mother conditions (module docstring)."""
	return [
		workload._not_archived("adm", MATERNITY_ADMISSION_DOCTYPE),
		f"IFNULL(pat.`{filters.MERGED_INTO_FIELD}`, '') = ''",
		*workload._facility_conditions(ctx, "adm.facility", params),
	]


def evaluate_delivery(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]:
	if workload._source_missing(
		ctx, (DELIVERY_RECORD_DOCTYPE, DELIVERY_BABY_DOCTYPE, MATERNITY_ADMISSION_DOCTYPE), SOURCE_DELIVERY
	):
		return {}
	out: dict[CellKey, int] = {}
	for element in elements:
		rule = workload._rule(ctx, element)
		if rule is None:
			continue
		params = {"m3_from": ctx.from_date, "m3_to": ctx.to_date}
		conds = [
			"dr.docstatus = 1",
			workload._not_archived("dr", DELIVERY_RECORD_DOCTYPE),
			f"{DELIVERY_DATE_SQL} BETWEEN %(m3_from)s AND %(m3_to)s",
			*_base(ctx, params),
		]
		workload._in(conds, params, "dr.delivery_mode", "m3_modes", rule.get("delivery_modes"))
		bba = rule.get("born_before_arrival") or "include"
		if bba == "exclude":
			conds.append("IFNULL(dr.born_before_arrival, 0) = 0")
		elif bba == "only":
			conds.append("IFNULL(dr.born_before_arrival, 0) = 1")
		if rule.get("measure") == "babies":
			workload._in(conds, params, "baby.outcome", "m3_outcomes", rule.get("baby_outcomes"))
			from_sql, measure = BABY_FROM, "COUNT(*)"
		else:
			from_sql, measure = DELIVERY_FROM, "COUNT(DISTINCT dr.name)"
		out.update(
			workload._aggregate(
				ctx,
				element,
				from_sql=from_sql,
				conditions=conds,
				params=params,
				event_date=DELIVERY_DATE_SQL,
				measure=measure,
				sex_sql=BABY_SEX_SQL,
			)
		)
	return out


def evaluate_maternity_admission(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]:
	if workload._source_missing(ctx, (MATERNITY_ADMISSION_DOCTYPE,), SOURCE_MATERNITY_ADMISSION):
		return {}
	out: dict[CellKey, int] = {}
	from_date, to_date = getdate(ctx.from_date), getdate(ctx.to_date)
	for element in elements:
		rule = workload._rule(ctx, element)
		if rule is None:
			continue
		measure = rule["measure"]
		params = {
			"m3_from": from_date,
			"m3_to": to_date,
			"m3_discharged": ADMISSION_DISCHARGED,
			"m3_period_end": add_days(to_date, 1),
			"m3_open_end": add_days(min(to_date, getdate(today())), 1),
		}
		conds = _base(ctx, params)
		event_date, sql = ADMIT_DATE_SQL, "COUNT(*)"
		if measure == "admissions":
			conds.append(f"{ADMIT_DATE_SQL} BETWEEN %(m3_from)s AND %(m3_to)s")
		elif measure in OUTCOME_MEASURES:
			conds += [
				"adm.status = %(m3_discharged)s",
				f"{DISCHARGE_DATE_SQL} BETWEEN %(m3_from)s AND %(m3_to)s",
			]
			workload._in(conds, params, "adm.discharge_outcome", "m3_outcomes", rule.get("outcomes"))
			event_date = DISCHARGE_DATE_SQL
			if measure == "stay_days":
				sql = STAY_DAYS_SQL
		else:  # bed_days: every stay that overlaps the period
			conds.append(f"{ADMIT_DATE_SQL} <= %(m3_to)s")
			conds.append(f"{STAY_END_SQL} > %(m3_from)s")
			sql = BED_DAYS_SQL
		out.update(
			workload._aggregate(
				ctx,
				element,
				from_sql=ADMISSION_FROM,
				conditions=conds,
				params=params,
				event_date=event_date,
				measure=sql,
			)
		)
	return out
