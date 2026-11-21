# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""R4: workload sources for MOH 717, the MOH 711 rehabilitation and social work section, and later forms.

Every evaluator has the W0 signature ``evaluate(ctx, elements) -> {CellKey: int}`` (`reporting.registry`).
It is read-only, runs one aggregate query per element, never depends on the user's permissions and
applies the base filters of `reporting.filters` (facility, company, archived visits, merged patients,
"Registered in error" / "Superseded" closures and the ``krcs_report_exclusions`` hook). Data problems go to
``ctx.add_exception`` with record IDs only. An element whose rule does not parse is skipped and listed as an
"Invalid rule" exception (so one bad element never blocks a run); `validate_rule` gives the same messages for
the definition form and the seed tests.

Shared conventions
------------------

- **Age** is in completed months on the event date of each source (below), through `reporting.ages`.
  A missing date of birth falls in ``ages.UNKNOWN_BAND`` and the element gets one "Age unknown" exception.
- **Sex** cells are "Male" and "Female"; every other Patient sex (or none) is "Other".
- **Department** of a visit (``departments`` filters) is the department the visit was last *routed* to: the
  newest status-log row of a ``route``, ``reroute`` or ``receive_referral`` action. A visit that was never
  routed (left at reception or triage) falls back to ``current_department`` (Reception / Triage), so it
  matches no clinical department, only elements without a department filter. ``current_department`` at the
  close is not used: a visit returned to triage and closed there would leave its clinic's count.
- **Outreach** (``outreach``: "include" / "exclude" / "only") filters on ``Patient Visit.is_outreach``. The
  default is "exclude" for attendance (outreach attendance is its own line) and "include" elsewhere.
- Lists of names (departments, templates, service types, categories...) are matched exactly; an empty or
  missing list means "no filter".

Rule formats (JSON object per element; keys not listed are refused)
------------------------------------------------------------------

**Visit Attendance** (`evaluate_attendance`; event date ``visit_date``; one count per visit)

- ``attendance_type``: "New" | "Re-attendance"; omitted: both. Visits with no attendance type yet are
  listed as "Attendance type missing" exceptions (up to 50 IDs) and counted only by untyped elements.
- ``departments``: [Medical Department]; ``outreach``: default "exclude".
- ``internal_referrals``: "include" (default) | "exclude" | "only". An internal referral opens a second
  visit (``referred_from`` set) in the receiving clinic: include it for clinic workload, exclude it for a
  facility total so the patient is not counted twice.
- ``exclude_closure_reasons``: [closure reason], e.g. ["Left before being seen"] (not seen, so not an
  attendance). The base filters already drop "Registered in error" and "Superseded by a new visit".
- ``unit``: "visits" (default) | "patients" (distinct patients).

**Laboratory Tests** (`evaluate_lab_tests`; event date: the day the test was first approved)

A *test* is an ordered test: a root Observation (no ``parent_observation``; panels up to two levels deep
are folded onto their root), counted once when at least one Observation of its tree is approved
(``docstatus`` 1). A full haemogram is one test, not ten. Its date is the earliest approval
(``time_of_approval``) in the tree; a rejected (cancelled) Observation is not a test. ``time_of_approval``
is stamped at submit (`lab.events.stamp_approval_time`; older rows by the
``backfill_observation_approval_time`` patch), so a later write such as a note does not move the test
to another month; ``modified`` is only the fallback for a row that still has no approval time.
The test belongs to the encounter of its Service Request (``order_group``), else of its reference (the
encounter or the encounter's Sample Collection), and through the encounter to the visit, which carries the
base filters. The encounter may still be a draft (results arrive during the consultation) but not cancelled
or archived.

- ``templates``: [Observation Template] of the root (the ordered test or panel); omitted: every template.
- ``categories``: [Observation Template ``observation_category``]; default ["Laboratory"].
- ``departments``; ``outreach`` (default "include").
- ``unit``: "tests" (default) | "patients".

**Prescriptions** (`evaluate_prescriptions`; event date ``encounter_date``)

Submitted Medication Requests (``docstatus`` 1, not archived) of encounters that pass
``filters.encounter_conditions`` (submitted encounter dated in the period: prescriptions are released when
the consultation completes).

- ``unit``: "prescriptions" (default; distinct encounters with at least one line) | "items" (Medication
  Request lines) | "patients".
- ``departments``; ``outreach`` (default "include").

**Dispensing** (`evaluate_dispensing`; event date ``dispensed_at``)

Submitted Dispensing Records (``docstatus`` 1; cancelled records are docstatus 2 and drop out), not
archived, of visits passing the base filters, whose encounter is submitted and not archived (D49;
DEFECT-RECON-1).

- ``outcome``: "Dispensed" (default; partial quantities included) | "Not Dispensed".
- ``unit``: "items" (default; distinct Medication Requests) | "prescriptions" (distinct encounters) |
  "records" | "patients".
- ``departments``; ``outreach`` (default "include").

**Referrals** (`evaluate_referrals`)

- ``direction`` (required): "out" | "in".
- "out": Referral records (not archived) whose ``status`` is in ``statuses`` (default Sent, Received,
  Completed, Lost to Follow-up: sent or later), dated by ``sent_at``, of source visits passing the base
  filters. ``referral_type``: "External" | "Internal"; omitted: both.
- "in": visits with ``referred_in`` ticked at reception, dated by ``visit_date``. ``referral_type`` and
  ``statuses`` are refused.
- ``departments`` (source visit for "out", receiving visit for "in"); ``outreach`` (default "include").

**Visit Outcome** (`evaluate_outcomes`; event date: ``outcome_at``, else ``closed_at``, else ``visit_date``)

- ``outcomes`` (required): [Patient Visit outcome], e.g. ["Died"] or ["Left without completing"].
- ``departments``; ``outreach`` (default "include").

**Service Delivered** (`evaluate_services`; event date ``service_date``)

Active records only (Voided never count) of visits passing the base filters. An Active record without a
visit cannot be attributed and is listed as a "Service without visit" exception.

- ``service_types``: [Visit Service Type]; ``categories``: [Visit Service Type ``category``];
  ``moh_lines``: [Visit Service Type ``moh_line``]. Several keys combine with AND.
- ``unit``: "services" (default) | "patients" | "visits".
- ``departments``; ``outreach`` (default "include").

**Community Activity** (`evaluate_community`; event date ``activity_date``)

Submitted Community Activity Records (``docstatus`` 1; drafts and cancelled records never count). They
have no patient and no visit, so the visit filters cannot apply: the facility filter takes the record's
``facility`` and every unit under it, the company filter takes the facility's company, and the
``krcs_report_exclusions`` hook (written for the visit aliases) is not applied.

- ``activity_types``: [Community Activity Type]; ``categories``: [its ``category``]; ``moh_lines``: [its
  ``moh_line``]. Several keys combine with AND.
- ``measure``: "sessions" (default; number of records) | "participants" (sum of ``participants_total``,
  at least male + female). Sex disaggregation is allowed for participants only: Male and Female from the
  split, "Other" for the remainder. Age disaggregation is refused (no ages are recorded).

**Delivery** and **Maternity Admission** (Phase 5M, M3): evaluated in `reporting.maternity`, whose docstring
holds their rule formats; `evaluate_delivery` and `evaluate_maternity_admission` below are thin wrappers so
the definition form checks their rules like every workload source.

R3's DocTypes (Service Delivered, Visit Service Type, Community Activity Record / Type) are guarded with
``frappe.db.table_exists``: before they exist those evaluators return nothing and add one "Source not
available" exception.
"""

from __future__ import annotations

import json

import frappe

from onerc_healthcare.clinical import referral
from onerc_healthcare.constants import (
	ATTENDANCE_TYPES,
	BABY_OUTCOMES,
	COMMUNITY_ACTIVITY_DOCTYPE,
	COMMUNITY_ACTIVITY_TYPE_DOCTYPE,
	DELIVERY_MODES,
	DISPENSING_DOCTYPE,
	ENCOUNTER_DOCTYPE,
	MATERNITY_DISCHARGE_OUTCOMES,
	MCH_RECORD_DOCTYPES,
	PNC_SUBJECT_BABY,
	PNC_SUBJECT_MOTHER,
	REFERRAL_DOCTYPE,
	SERVICE_DELIVERED_DOCTYPE,
	SERVICE_TYPE_DOCTYPE,
	VISIT_CLOSURE_REASONS,
	VISIT_DOCTYPE,
	VISIT_OUTCOMES,
)
from onerc_healthcare.pharmacy import dispensing
from onerc_healthcare.reporting import ages, filters
from onerc_healthcare.reporting.registry import (
	DISAGGREGATION_AGE,
	DISAGGREGATION_AGE_SEX,
	DISAGGREGATION_SEX,
	CellKey,
	ElementSpec,
	SourceContext,
	cell_key,
)
from onerc_healthcare.visit.engine import ACTION_RECEIVE_REFERRAL, ACTION_REROUTE, ACTION_ROUTE

SOURCE_ATTENDANCE = "Visit Attendance"
SOURCE_LAB = "Laboratory Tests"
SOURCE_PRESCRIPTIONS = "Prescriptions"
SOURCE_DISPENSING = "Dispensing"
SOURCE_REFERRALS = "Referrals"
SOURCE_OUTCOME = "Visit Outcome"
SOURCE_SERVICES = "Service Delivered"
SOURCE_COMMUNITY = "Community Activity"
SOURCE_DELIVERY = "Delivery"  # Phase 5M (M3): reporting/maternity.py
SOURCE_MATERNITY_ADMISSION = "Maternity Admission"  # Phase 5M (M3): reporting/maternity.py
SOURCE_MCH = "MCH Records"  # MCH (MCH-A): reporting/mch.py, MOH 717 B07-B09

SEX_MALE = "Male"
SEX_FEMALE = "Female"
SEX_OTHER = "Other"
SEX_SQL = "(CASE WHEN pat.sex IN ('Male', 'Female') THEN pat.sex ELSE 'Other' END)"

# Status-log actions that send a visit to a clinical department (visit engine action names).
ROUTING_ACTIONS = (ACTION_ROUTE, ACTION_REROUTE, ACTION_RECEIVE_REFERRAL)
ROUTED_DEPARTMENT_SQL = (
	"COALESCE((SELECT l.department FROM `tabPatient Visit Status Log` l"
	" WHERE l.parent = v.name AND l.parenttype = 'Patient Visit'"
	f" AND l.action IN ({', '.join(f"'{a}'" for a in ROUTING_ACTIONS)})"  # constants, no quotes inside
	" AND IFNULL(l.department, '') != ''"
	" ORDER BY l.idx DESC LIMIT 1), v.current_department)"
)

# Referral statuses that mean "sent or later" (clinical.referral lifecycle).
REFERRAL_SENT_OR_LATER = (
	referral.STATUS_SENT,
	referral.STATUS_RECEIVED,
	referral.STATUS_COMPLETED,
	referral.STATUS_LOST,
)
REFERRAL_TYPES = (referral.TYPE_INTERNAL, referral.TYPE_EXTERNAL)
DISPENSING_OUTCOMES = (dispensing.OUTCOME_DISPENSED, dispensing.OUTCOME_NOT_DISPENSED)
LAB_DEFAULT_CATEGORIES = ("Laboratory",)
SERVICE_STATUS_ACTIVE = "Active"

MISSING_ATTENDANCE_LIMIT = 50
OUTREACH_CHOICES = ("include", "exclude", "only")

# --------------------------------------------------------------------------------------------------
# Rule schemas
# --------------------------------------------------------------------------------------------------


def _choice(*values):
	def check(key, value):
		if value not in values:
			return f"{key} must be one of {', '.join(map(str, values))}"

	return check


def _names(*allowed):
	def check(key, value):
		if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
			return f"{key} must be a list of names"
		if allowed:
			bad = [v for v in value if v not in allowed]
			if bad:
				return f"{key}: unknown value(s) {', '.join(bad)}"

	return check


_VISIT_SCOPE = {"departments": _names(), "outreach": _choice(*OUTREACH_CHOICES)}

RULE_SCHEMAS: dict[str, dict] = {
	SOURCE_ATTENDANCE: {
		**_VISIT_SCOPE,
		"attendance_type": _choice(*ATTENDANCE_TYPES),
		"internal_referrals": _choice("include", "exclude", "only"),
		"exclude_closure_reasons": _names(*VISIT_CLOSURE_REASONS),
		"unit": _choice("visits", "patients"),
	},
	SOURCE_LAB: {
		**_VISIT_SCOPE,
		"templates": _names(),
		"categories": _names(),
		"unit": _choice("tests", "patients"),
	},
	SOURCE_PRESCRIPTIONS: {**_VISIT_SCOPE, "unit": _choice("prescriptions", "items", "patients")},
	SOURCE_DISPENSING: {
		**_VISIT_SCOPE,
		"outcome": _choice(*DISPENSING_OUTCOMES),
		"unit": _choice("items", "prescriptions", "records", "patients"),
	},
	SOURCE_REFERRALS: {
		**_VISIT_SCOPE,
		"direction": _choice("in", "out"),
		"referral_type": _choice(*REFERRAL_TYPES),
		"statuses": _names(*referral.STATUSES),
	},
	SOURCE_OUTCOME: {**_VISIT_SCOPE, "outcomes": _names(*VISIT_OUTCOMES)},
	SOURCE_SERVICES: {
		**_VISIT_SCOPE,
		"service_types": _names(),
		"categories": _names(),
		"moh_lines": _names(),
		"unit": _choice("services", "patients", "visits"),
	},
	SOURCE_COMMUNITY: {
		"activity_types": _names(),
		"categories": _names(),
		"moh_lines": _names(),
		"measure": _choice("sessions", "participants"),
	},
	SOURCE_DELIVERY: {
		"measure": _choice("deliveries", "babies"),
		"delivery_modes": _names(*DELIVERY_MODES),
		"born_before_arrival": _choice("include", "exclude", "only"),
		"baby_outcomes": _names(*BABY_OUTCOMES),
	},
	SOURCE_MATERNITY_ADMISSION: {
		"measure": _choice("admissions", "discharges", "bed_days", "stay_days"),
		"outcomes": _names(*MATERNITY_DISCHARGE_OUTCOMES),
	},
	SOURCE_MCH: {
		"record_types": _names(*MCH_RECORD_DOCTYPES),
		"unit": _choice("records", "visits", "patients"),
		"pnc_subjects": _names(PNC_SUBJECT_MOTHER, PNC_SUBJECT_BABY),
	},
}
REQUIRED_KEYS = {
	SOURCE_REFERRALS: ("direction",),
	SOURCE_OUTCOME: ("outcomes",),
	SOURCE_MATERNITY_ADMISSION: ("measure",),
	SOURCE_MCH: ("record_types",),
}


def validate_rule(source_type: str, rule, disaggregation: str | None = None) -> list[str]:
	"""Error messages for an element rule of an R4 source (empty when valid). `rule` is a dict or JSON."""
	schema = RULE_SCHEMAS.get(source_type)
	if schema is None:
		return [f"{source_type} is not a workload source"]
	if rule in (None, ""):
		rule = {}
	if isinstance(rule, str):
		try:
			rule = json.loads(rule)
		except ValueError:
			return ["the rule is not valid JSON"]
	if not isinstance(rule, dict):
		return ["the rule must be a JSON object"]
	errors = [f"unknown key {key}" for key in rule if key not in schema]
	for key, check in schema.items():
		if key in rule and rule[key] is not None:
			msg = check(key, rule[key])
			if msg:
				errors.append(msg)
	errors += [f"{key} is required" for key in REQUIRED_KEYS.get(source_type, ()) if not rule.get(key)]
	if source_type == SOURCE_OUTCOME and rule.get("outcomes") == []:
		errors.append("outcomes must not be empty")
	if source_type == SOURCE_REFERRALS and rule.get("direction") == "in":
		errors += [
			f"{key} applies to referrals out only" for key in ("referral_type", "statuses") if key in rule
		]
	if source_type == SOURCE_COMMUNITY and disaggregation:
		if disaggregation in (DISAGGREGATION_AGE, DISAGGREGATION_AGE_SEX):
			errors.append("community activities record no ages: use no age disaggregation")
		if disaggregation == DISAGGREGATION_SEX and rule.get("measure", "sessions") != "participants":
			errors.append("only participants can be split by sex")
	if source_type in (SOURCE_DELIVERY, SOURCE_MATERNITY_ADMISSION):
		from onerc_healthcare.reporting import maternity

		errors += maternity.rule_errors(source_type, rule, disaggregation)
	if source_type == SOURCE_MCH:
		from onerc_healthcare.reporting import mch

		errors += mch.rule_errors(rule, disaggregation)
	return errors


def _rule(ctx: SourceContext, element: ElementSpec) -> dict | None:
	errors = validate_rule(element.source_type, element.rule, element.disaggregation)
	if errors:
		ctx.add_exception("Invalid rule", "; ".join(errors), element=element.code)
		return None
	rule = element.rule or {}
	return json.loads(rule) if isinstance(rule, str) else dict(rule)


# --------------------------------------------------------------------------------------------------
# SQL helpers
# --------------------------------------------------------------------------------------------------


def _dims(element: ElementSpec) -> tuple[bool, bool]:
	by_age = element.disaggregation in (DISAGGREGATION_AGE, DISAGGREGATION_AGE_SEX)
	by_sex = element.disaggregation in (DISAGGREGATION_SEX, DISAGGREGATION_AGE_SEX)
	return by_age, by_sex


def _not_archived(alias: str, doctype: str) -> str:
	if frappe.db.has_column(doctype, filters.ARCHIVED_FIELD):
		return f"IFNULL({alias}.`{filters.ARCHIVED_FIELD}`, 0) = 0"
	return "1 = 1"


def _visit_scope(rule: dict, params: dict, outreach_default: str = "include") -> list[str]:
	"""Department and outreach conditions on ``v``."""
	conds = []
	if rule.get("departments"):
		conds.append(f"{ROUTED_DEPARTMENT_SQL} IN %(r4_departments)s")
		params["r4_departments"] = list(rule["departments"])
	outreach = rule.get("outreach") or outreach_default
	if outreach == "exclude":
		conds.append("IFNULL(v.is_outreach, 0) = 0")
	elif outreach == "only":
		conds.append("IFNULL(v.is_outreach, 0) = 1")
	return conds


def _in(conds: list[str], params: dict, column: str, key: str, values) -> None:
	if values:
		conds.append(f"{column} IN %({key})s")
		params[key] = list(values)


def _aggregate(
	ctx: SourceContext,
	element: ElementSpec,
	*,
	from_sql: str,
	conditions: list[str],
	params: dict,
	event_date: str,
	measure: str = "COUNT(*)",
	dob_col: str = "pat.dob",
	sex_sql: str = SEX_SQL,
) -> dict[CellKey, int]:
	"""One grouped count for `element`: by age band and / or sex as the element is disaggregated."""
	by_age, by_sex = _dims(element)
	band = ages.band_case_sql(dob_col, event_date, ctx.age_bands) if by_age else "''"
	sex = sex_sql if by_sex else "''"
	rows = frappe.db.sql(
		f"SELECT {band} AS band, {sex} AS sex, {measure} AS n FROM {from_sql}"
		f" WHERE {' AND '.join(f'({c})' for c in conditions)} GROUP BY 1, 2",
		params,
	)
	out: dict[CellKey, int] = {}
	unknown = 0
	for band_label, sex_label, n in rows:
		n = int(n or 0)
		if not n:
			continue
		key = cell_key(element, band_label, sex_label)
		out[key] = out.get(key, 0) + n
		if by_age and band_label == ages.UNKNOWN_BAND:
			unknown += n
	if unknown:
		ctx.add_exception(
			"Age unknown",
			f"{unknown} counted under '{ages.UNKNOWN_BAND}': date of birth missing or after the event date",
			element=element.code,
		)
	return out


def _source_missing(ctx: SourceContext, doctypes: tuple[str, ...], source: str) -> bool:
	missing = [d for d in doctypes if not frappe.db.table_exists(d)]
	if missing:
		ctx.add_exception(
			"Source not available",
			f"{source}: {', '.join(missing)} does not exist on this site yet; its elements are empty",
		)
	return bool(missing)


# --------------------------------------------------------------------------------------------------
# Evaluators
# --------------------------------------------------------------------------------------------------


def evaluate_attendance(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]:
	where, base = filters.visit_conditions(ctx, by_visit_date=True)
	out: dict[CellKey, int] = {}
	typed = False
	for element in elements:
		rule = _rule(ctx, element)
		if rule is None:
			continue
		params = dict(base)
		conds = [where, *_visit_scope(rule, params, outreach_default="exclude")]
		if rule.get("attendance_type"):
			typed = True
			conds.append("v.attendance_type = %(r4_attendance_type)s")
			params["r4_attendance_type"] = rule["attendance_type"]
		internal = rule.get("internal_referrals") or "include"
		if internal == "exclude":
			conds.append("IFNULL(v.referred_from, '') = ''")
		elif internal == "only":
			conds.append("IFNULL(v.referred_from, '') != ''")
		if rule.get("exclude_closure_reasons"):
			conds.append("IFNULL(v.closure_reason, '') NOT IN %(r4_closures)s")
			params["r4_closures"] = list(rule["exclude_closure_reasons"])
		measure = "COUNT(DISTINCT v.patient)" if rule.get("unit") == "patients" else "COUNT(*)"
		out.update(
			_aggregate(
				ctx,
				element,
				from_sql=filters.VISIT_FROM,
				conditions=conds,
				params=params,
				event_date="v.visit_date",
				measure=measure,
			)
		)
	if typed:
		_report_missing_attendance(ctx, where, base)
	return out


def _report_missing_attendance(ctx: SourceContext, where: str, params: dict) -> None:
	names = frappe.db.sql_list(
		f"SELECT v.name FROM {filters.VISIT_FROM} WHERE {where} AND IFNULL(v.attendance_type, '') = ''"
		f" ORDER BY v.name LIMIT {MISSING_ATTENDANCE_LIMIT + 1}",
		params,
	)
	for name in names[:MISSING_ATTENDANCE_LIMIT]:
		ctx.add_exception(
			"Attendance type missing",
			"Visit has no New / Re-attendance type; counted only in untyped totals",
			VISIT_DOCTYPE,
			name,
		)
	if len(names) > MISSING_ATTENDANCE_LIMIT:
		ctx.add_exception(
			"Attendance type missing",
			f"More than {MISSING_ATTENDANCE_LIMIT} visits have no attendance type; only the first are listed",
		)


# Observation tree folded to its root (panels up to two levels deep), the root's encounter, its visit.
_LAB_ENCOUNTER_SQL = (
	"COALESCE("
	"CASE WHEN sr.source_doc = 'Patient Encounter' THEN sr.order_group END,"
	" CASE WHEN root.reference_doctype = 'Patient Encounter' THEN root.reference_docname END,"
	" CASE WHEN sc.reference_doc = 'Patient Encounter' THEN sc.reference_name END)"
)
_LAB_FROM = (
	"`tabObservation` obs"
	" LEFT JOIN `tabObservation` p1 ON p1.name = obs.parent_observation"
	" LEFT JOIN `tabObservation` p2 ON p2.name = p1.parent_observation"
	" JOIN `tabObservation` root ON root.name = COALESCE(p2.name, p1.name, obs.name)"
	" LEFT JOIN `tabObservation Template` ot ON ot.name = root.observation_template"
	" LEFT JOIN `tabService Request` sr ON sr.name = root.service_request"
	" LEFT JOIN `tabSample Collection` sc"
	" ON root.reference_doctype = 'Sample Collection' AND sc.name = root.reference_docname"
	f" JOIN `tabPatient Encounter` enc ON enc.name = {_LAB_ENCOUNTER_SQL}"
	" JOIN `tabPatient Visit` v ON v.name = enc.krcs_visit"
	" JOIN `tabPatient` pat ON pat.name = v.patient"
)


def evaluate_lab_tests(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]:
	where, base = filters.visit_conditions(ctx, by_visit_date=False)
	out: dict[CellKey, int] = {}
	for element in elements:
		rule = _rule(ctx, element)
		if rule is None:
			continue
		params = dict(base)
		conds = [
			where,
			"obs.docstatus = 1",
			# Approvals after the period never change whether a test's first approval is in it.
			"DATE(COALESCE(obs.time_of_approval, obs.modified)) <= %(krcs_to)s",
			_not_archived("obs", "Observation"),
			"root.docstatus < 2",
			"enc.docstatus < 2",
			_not_archived("enc", ENCOUNTER_DOCTYPE),
			*_visit_scope(rule, params),
		]
		_in(conds, params, "root.observation_template", "r4_templates", rule.get("templates"))
		categories = rule.get("categories")
		_in(conds, params, "ot.observation_category", "r4_categories", categories or LAB_DEFAULT_CATEGORIES)
		inner = (
			"SELECT root.name AS test, MAX(v.patient) AS patient, MAX(pat.dob) AS dob, MAX(pat.sex) AS sex,"
			" MIN(DATE(COALESCE(obs.time_of_approval, obs.modified))) AS done_on"
			f" FROM {_LAB_FROM} WHERE {' AND '.join(f'({c})' for c in conds)} GROUP BY root.name"
		)
		measure = "COUNT(DISTINCT t.patient)" if rule.get("unit") == "patients" else "COUNT(*)"
		out.update(
			_aggregate(
				ctx,
				element,
				from_sql=f"({inner}) t",
				conditions=["t.done_on BETWEEN %(krcs_from)s AND %(krcs_to)s"],
				params=params,
				event_date="t.done_on",
				measure=measure,
				dob_col="t.dob",
				sex_sql="(CASE WHEN t.sex IN ('Male', 'Female') THEN t.sex ELSE 'Other' END)",
			)
		)
	return out


def evaluate_prescriptions(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]:
	where, base = filters.encounter_conditions(ctx)
	from_sql = f"{filters.ENCOUNTER_FROM} JOIN `tabMedication Request` mr ON mr.order_group = enc.name"
	units = {"items": "COUNT(*)", "patients": "COUNT(DISTINCT v.patient)"}
	out: dict[CellKey, int] = {}
	for element in elements:
		rule = _rule(ctx, element)
		if rule is None:
			continue
		params = dict(base)
		conds = [
			where,
			"mr.docstatus = 1",
			_not_archived("mr", "Medication Request"),
			*_visit_scope(rule, params),
		]
		out.update(
			_aggregate(
				ctx,
				element,
				from_sql=from_sql,
				conditions=conds,
				params=params,
				event_date="enc.encounter_date",
				measure=units.get(rule.get("unit"), "COUNT(DISTINCT enc.name)"),
			)
		)
	return out


def evaluate_dispensing(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]:
	where, base = filters.visit_conditions(ctx, by_visit_date=False)
	from_sql = (
		f"`tab{DISPENSING_DOCTYPE}` d JOIN `tabPatient Visit` v ON v.name = d.visit"
		" JOIN `tabPatient` pat ON pat.name = v.patient"
		" LEFT JOIN `tabPatient Encounter` enc ON enc.name = d.encounter"
	)
	encounter_ok = (
		f"(d.encounter IS NULL OR (enc.docstatus = 1 AND {_not_archived('enc', 'Patient Encounter')}))"
	)
	units = {
		"records": "COUNT(*)",
		"prescriptions": "COUNT(DISTINCT d.encounter)",
		"patients": "COUNT(DISTINCT v.patient)",
	}
	out: dict[CellKey, int] = {}
	for element in elements:
		rule = _rule(ctx, element)
		if rule is None:
			continue
		params = dict(base)
		params["r4_outcome"] = rule.get("outcome") or dispensing.OUTCOME_DISPENSED
		conds = [
			where,
			"d.docstatus = 1",
			_not_archived("d", DISPENSING_DOCTYPE),
			encounter_ok,
			"d.outcome = %(r4_outcome)s",
			"DATE(d.dispensed_at) BETWEEN %(krcs_from)s AND %(krcs_to)s",
			*_visit_scope(rule, params),
		]
		out.update(
			_aggregate(
				ctx,
				element,
				from_sql=from_sql,
				conditions=conds,
				params=params,
				event_date="DATE(d.dispensed_at)",
				measure=units.get(rule.get("unit"), "COUNT(DISTINCT d.medication_request)"),
			)
		)
	return out


def evaluate_referrals(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]:
	out: dict[CellKey, int] = {}
	for element in elements:
		rule = _rule(ctx, element)
		if rule is None:
			continue
		if rule["direction"] == "in":
			where, params = filters.visit_conditions(ctx, by_visit_date=True)
			conds = [where, "IFNULL(v.referred_in, 0) = 1", *_visit_scope(rule, params)]
			from_sql, event_date = filters.VISIT_FROM, "v.visit_date"
		else:
			where, params = filters.visit_conditions(ctx, by_visit_date=False)
			conds = [
				where,
				_not_archived("r", REFERRAL_DOCTYPE),
				"r.status IN %(r4_statuses)s",
				"DATE(r.sent_at) BETWEEN %(krcs_from)s AND %(krcs_to)s",
				*_visit_scope(rule, params),
			]
			params["r4_statuses"] = list(rule.get("statuses") or REFERRAL_SENT_OR_LATER)
			if rule.get("referral_type"):
				conds.append("r.referral_type = %(r4_referral_type)s")
				params["r4_referral_type"] = rule["referral_type"]
			from_sql = (
				f"`tab{REFERRAL_DOCTYPE}` r JOIN `tabPatient Visit` v ON v.name = r.source_visit"
				" JOIN `tabPatient` pat ON pat.name = v.patient"
			)
			event_date = "DATE(r.sent_at)"
		out.update(
			_aggregate(
				ctx, element, from_sql=from_sql, conditions=conds, params=params, event_date=event_date
			)
		)
	return out


OUTCOME_DATE_SQL = "DATE(COALESCE(v.outcome_at, v.closed_at, v.visit_date))"


def evaluate_outcomes(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]:
	where, base = filters.visit_conditions(ctx, by_visit_date=False)
	out: dict[CellKey, int] = {}
	for element in elements:
		rule = _rule(ctx, element)
		if rule is None:
			continue
		params = dict(base)
		params["r4_outcomes"] = list(rule["outcomes"])
		conds = [
			where,
			"v.outcome IN %(r4_outcomes)s",
			f"{OUTCOME_DATE_SQL} BETWEEN %(krcs_from)s AND %(krcs_to)s",
			*_visit_scope(rule, params),
		]
		out.update(
			_aggregate(
				ctx,
				element,
				from_sql=filters.VISIT_FROM,
				conditions=conds,
				params=params,
				event_date=OUTCOME_DATE_SQL,
			)
		)
	return out


def evaluate_services(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]:
	if _source_missing(ctx, (SERVICE_DELIVERED_DOCTYPE, SERVICE_TYPE_DOCTYPE), SOURCE_SERVICES):
		return {}
	where, base = filters.visit_conditions(ctx, by_visit_date=False)
	from_sql = (
		f"`tab{SERVICE_DELIVERED_DOCTYPE}` sd JOIN `tabPatient Visit` v ON v.name = sd.visit"
		" JOIN `tabPatient` pat ON pat.name = v.patient"
		f" LEFT JOIN `tab{SERVICE_TYPE_DOCTYPE}` st ON st.name = sd.service_type"
	)
	units = {"patients": "COUNT(DISTINCT v.patient)", "visits": "COUNT(DISTINCT v.name)"}
	out: dict[CellKey, int] = {}
	for element in elements:
		rule = _rule(ctx, element)
		if rule is None:
			continue
		params = dict(base)
		params["r4_active"] = SERVICE_STATUS_ACTIVE
		conds = [
			where,
			"sd.status = %(r4_active)s",
			_not_archived("sd", SERVICE_DELIVERED_DOCTYPE),
			"sd.service_date BETWEEN %(krcs_from)s AND %(krcs_to)s",
			*_visit_scope(rule, params),
		]
		_in(conds, params, "sd.service_type", "r4_service_types", rule.get("service_types"))
		_in(conds, params, "st.category", "r4_categories", rule.get("categories"))
		_in(conds, params, "st.moh_line", "r4_moh_lines", rule.get("moh_lines"))
		out.update(
			_aggregate(
				ctx,
				element,
				from_sql=from_sql,
				conditions=conds,
				params=params,
				event_date="sd.service_date",
				measure=units.get(rule.get("unit"), "COUNT(*)"),
			)
		)
	if elements:
		_report_services_without_visit(ctx)
	return out


def _facility_conditions(ctx: SourceContext, column: str, params: dict) -> list[str]:
	"""Facility (the unit and every unit under it) and company conditions for a Healthcare Service Unit link."""
	conds = []
	if ctx.facility:
		conds.append(
			f"{column} IN (SELECT c.name FROM `tabHealthcare Service Unit` c"
			" JOIN `tabHealthcare Service Unit` g ON c.lft >= g.lft AND c.rgt <= g.rgt"
			" WHERE g.name = %(r4_facility)s)"
		)
		params["r4_facility"] = ctx.facility
	if ctx.company:
		conds.append(
			f"{column} IN (SELECT name FROM `tabHealthcare Service Unit` WHERE company = %(r4_company)s)"
		)
		params["r4_company"] = ctx.company
	return conds


def _report_services_without_visit(ctx: SourceContext) -> None:
	params = {"r4_from": ctx.from_date, "r4_to": ctx.to_date, "r4_active": SERVICE_STATUS_ACTIVE}
	conds = [
		"IFNULL(sd.visit, '') = ''",
		"sd.status = %(r4_active)s",
		"sd.service_date BETWEEN %(r4_from)s AND %(r4_to)s",
		_not_archived("sd", SERVICE_DELIVERED_DOCTYPE),
		*_facility_conditions(ctx, "sd.facility", params),
	]
	names = frappe.db.sql_list(
		f"SELECT sd.name FROM `tab{SERVICE_DELIVERED_DOCTYPE}` sd WHERE {' AND '.join(conds)}"
		f" ORDER BY sd.name LIMIT {MISSING_ATTENDANCE_LIMIT}",
		params,
	)
	for name in names:
		ctx.add_exception(
			"Service without visit",
			"Active service has no visit, so it cannot be attributed; it is not counted",
			SERVICE_DELIVERED_DOCTYPE,
			name,
		)


def evaluate_community(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]:
	if _source_missing(ctx, (COMMUNITY_ACTIVITY_DOCTYPE, COMMUNITY_ACTIVITY_TYPE_DOCTYPE), SOURCE_COMMUNITY):
		return {}
	from_sql = (
		f"`tab{COMMUNITY_ACTIVITY_DOCTYPE}` car"
		f" LEFT JOIN `tab{COMMUNITY_ACTIVITY_TYPE_DOCTYPE}` cat ON cat.name = car.activity_type"
	)
	out: dict[CellKey, int] = {}
	for element in elements:
		rule = _rule(ctx, element)
		if rule is None:
			continue
		params = {"r4_from": ctx.from_date, "r4_to": ctx.to_date}
		conds = [
			"car.docstatus = 1",
			_not_archived("car", COMMUNITY_ACTIVITY_DOCTYPE),
			"car.activity_date BETWEEN %(r4_from)s AND %(r4_to)s",
			*_facility_conditions(ctx, "car.facility", params),
		]
		_in(conds, params, "car.activity_type", "r4_activity_types", rule.get("activity_types"))
		_in(conds, params, "cat.category", "r4_categories", rule.get("categories"))
		_in(conds, params, "cat.moh_line", "r4_moh_lines", rule.get("moh_lines"))
		male = "IFNULL(car.participants_male, 0)"
		female = "IFNULL(car.participants_female, 0)"
		total = f"GREATEST(IFNULL(car.participants_total, 0), {male} + {female})"
		row = frappe.db.sql(
			f"SELECT COUNT(*), SUM({male}), SUM({female}), SUM({total}) FROM {from_sql}"
			f" WHERE {' AND '.join(f'({c})' for c in conds)}",
			params,
		)[0]
		sessions, males, females, participants = (int(x or 0) for x in row)
		by_sex = _dims(element)[1]  # age disaggregation is refused by validate_rule
		if rule.get("measure") != "participants":
			cells = {"": sessions}
		elif by_sex:
			cells = {SEX_MALE: males, SEX_FEMALE: females, SEX_OTHER: participants - males - females}
		else:
			cells = {"": participants}
		for sex, n in cells.items():
			if n:
				key = cell_key(element, None, sex)
				out[key] = out.get(key, 0) + n
	return out


def evaluate_delivery(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]:
	"""Phase 5M (M3): `reporting.maternity.evaluate_delivery` (MOH 717 K01-K07)."""
	from onerc_healthcare.reporting import maternity

	return maternity.evaluate_delivery(ctx, elements)


def evaluate_maternity_admission(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]:
	"""Phase 5M (M3): `reporting.maternity.evaluate_maternity_admission` (MOH 717 J01-J04, K08)."""
	from onerc_healthcare.reporting import maternity

	return maternity.evaluate_maternity_admission(ctx, elements)


def evaluate_mch(ctx: SourceContext, elements: list[ElementSpec]) -> dict[CellKey, int]:
	"""MCH (MCH-A): `reporting.mch.evaluate` (MOH 717 B07 CWC, B08 ANC, B09 PNC attendances)."""
	from onerc_healthcare.reporting import mch

	return mch.evaluate(ctx, elements)


def install() -> None:
	"""Nothing to seed: the MOH 717 / 711 / blocked definitions are JSON files in ``reporting/data`` that
	R1's ``reporting.definitions.sync_definitions()`` loads. Kept for the W0 install chain."""
