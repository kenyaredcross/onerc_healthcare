# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Outreach package (OUT-06, FR-23, D58; protocol §3). O1 owns this file.

Both endpoints accept an online sign-in (cookie) or a device grant (`outreach.auth`), and both require the user
to be on the session team. With a device grant, the grant's session must be the session asked for. The session
must be open (Planned, Active or Returned).

**`get_reference(session, version=None)`** (GET): the reference data the app works from offline. `version` is
the SHA-1 of the canonical JSON of `data`, so an unchanged client copy gets `{"version", "unchanged": true}`.
- `icd11`: `[name, code, display, is_leaf]` of every ICD-11 Code Value outside chapter X (extension codes
  cannot be a diagnosis, `clinical.diagnosis`).
- `departments`: the session's departments, never Mental Health (`OUTREACH_EXCLUDED_DEPARTMENTS`) nor a
  non-clinical department (`outreach.session.NOT_OFFERED_DEPARTMENTS`).
- `lab_templates`: the orderable tests: point-of-care templates, panels (`has_component`), the catalogue's single
  tests, and any other sample-collection template that is not only a panel component. `point_of_care` is the
  `krcs_point_of_care` flag.
- The other lists are enabled masters (medications, dosages, durations, dosage forms, identifier types, the
  operation's residence tree, triage levels by priority, community activity types, genders) and the outcomes an
  outreach visit may close with (Discharged home, Died).
- `dosage_units` `{Prescription Dosage: doses per day}` (the sum of its Dosage Strength rows) and `period_days`
  `{Prescription Duration: days}` (Marley's `get_days`), protocol §6 F3: the app computes a line's prescribed
  quantity as `dosage_units[dosage] x period_days[period]` (at least 1), as the server does with the daily
  interval (DEFECT-RX-1), and never dispenses more.

Closed sessions (protocol §6 F1): both endpoints refuse them ("no new work"), with a cookie or a device grant.
`patient_in_catchment` is also the session-patient check of `outreach.apply` (§6 F5).

**`get_patients(session, page=0, page_size=500)`** (GET): active patients (not Disabled, not merged, not
archived) whose `krcs_residence` lies in one of the catchment area's residence subtrees (`lft`/`rgt`), ordered by
name (stable paging), at most `package_max_patients`. Per patient: demographics, identifiers, the last
`summary_visits` visits with their submitted diagnoses, and the active prescriptions.
- **Mental Health is never sent.** A visit is skipped when its department, or the department of any of its
  encounters (`krcs_department`, else `medical_department`, else the visit's department, the same order as
  `privacy.access_log.is_sensitive`), is in `OUTREACH_EXCLUDED_DEPARTMENTS`, or when it has a Service Delivered
  of a Visit Service Type marked sensitive. The last N visits are taken *after* this filter, so the gap never
  shows. Prescriptions follow the same encounter rule. Every encounter that is sent is checked once more with
  `is_sensitive` (no query: the values are already loaded).
- Visits closed as Registered in error / Superseded, archived visits and encounters, and draft or cancelled
  encounters are left out.
- A prescription is active while `encounter_date + period` has not passed, the Medication Request was not
  stopped, cancelled, ended, entered in error or closed as Not Dispensed. A line without a known period counts
  for `UNKNOWN_PERIOD_DAYS` days.
- Every patient sent is written to the Access Log once per page sent (action "Outreach package",
  `sensitive=0`, the session and page in the details), in one bulk insert.
- The first download moves a Planned session to Active (`outreach.session.activate_on_download`).
- The queries are batched per page (one query each for patients, identifiers, visits, diagnoses and
  prescriptions), never per patient. A GET is rolled back by Frappe, so the request is flagged to commit.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_months, cint, flt, getdate, now_datetime, today

from onerc_healthcare.constants import (
	ACCESS_ACTION_OUTREACH_PACKAGE,
	ACCESS_LOG_DOCTYPE,
	CATCHMENT_LOCATION_DOCTYPE,
	COMMUNITY_ACTIVITY_TYPE_DOCTYPE,
	ENCOUNTER_DIAGNOSIS_DOCTYPE,
	ENCOUNTER_DOCTYPE,
	IDENTIFIER_FACILITY,
	OUTCOME_DIED,
	OUTCOME_DISCHARGED,
	OUTREACH_EXCLUDED_DEPARTMENTS,
	PATIENT_DOB_ESTIMATED_FIELD,
	PATIENT_IDENTIFIER_DOCTYPE,
	PATIENT_IDENTIFIER_TYPE_DOCTYPE,
	PATIENT_IDENTIFIERS_FIELD,
	POINT_OF_CARE_FIELD,
	REPORT_EXCLUDED_CLOSURE_REASONS,
	SERVICE_DELIVERED_DOCTYPE,
	SERVICE_TYPE_DOCTYPE,
	VISIT_DOCTYPE,
)
from onerc_healthcare.outreach.devices import assert_request_grant
from onerc_healthcare.outreach.session import (
	activate_on_download,
	assert_member,
	assert_open,
	session_departments,
)

DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 1000
UNKNOWN_PERIOD_DAYS = 30
# Prescriptions older than this are never active (the longest seeded duration is 3 months).
PRESCRIPTION_LOOKBACK_DAYS = 400
OUTREACH_OUTCOMES = (OUTCOME_DISCHARGED, OUTCOME_DIED)
ARCHIVED_FIELD = "krcs_archived"
MERGED_INTO_FIELD = "krcs_merged_into"
RESIDENCE_DOCTYPE = "Residence Location"
# Medication Request states that end a prescription (pharmacy.dispensing)
ENDED_MR_STATUSES = (
	"stopped-Medication Request Status",
	"cancelled-Medication Request Status",
	"entered-in-error-Medication Request Status",
	"ended-Medication Request Status",
)
ENDED_DISPENSE_STATUSES = ("Not Dispensed",)
ENDED_HOLD_STATUSES = ("Stopped",)


# ------------------------------------------------------------------------------------------------- access


def assert_package_access(session: str) -> frappe._dict:
	"""The open session row, for a team member; a device grant must be for this session."""
	if frappe.session.user in (None, "", "Guest"):
		frappe.throw(_("Sign in to continue."), frappe.AuthenticationError)
	assert_request_grant(session)
	assert_member(session)
	return assert_open(session)


# ---------------------------------------------------------------------------------------------- reference


def content_version(data) -> str:
	return hashlib.sha1(
		json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()
	).hexdigest()


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=120, seconds=60 * 60, ip_based=False, user_based=True)
def get_reference(session: str, version: str | None = None) -> dict:
	"""Reference data for offline work (protocol §3)."""
	assert_package_access(session)
	data = build_reference(session)
	current = content_version(data)
	if version and version == current:
		return {"version": current, "unchanged": True}
	return {"version": current, "unchanged": False, "data": data}


def build_reference(session: str) -> dict:
	return {
		"icd11": icd11_rows(),
		"medications": medications(),
		"dosages": sorted(frappe.get_all("Prescription Dosage", pluck="name")),
		"dosage_units": dosage_units(),
		"periods": durations(),
		"period_days": period_days(),
		"dosage_forms": sorted(frappe.get_all("Dosage Form", pluck="name")),
		"lab_templates": lab_templates(),
		"departments": session_departments(session),
		"identifier_types": identifier_types(),
		"residences": residences(),
		"triage_levels": frappe.get_all("Triage Level", pluck="name", order_by="priority asc, name asc"),
		"community_activity_types": frappe.get_all(
			COMMUNITY_ACTIVITY_TYPE_DOCTYPE, filters={"enabled": 1}, pluck="name", order_by="name asc"
		),
		"outcomes": list(OUTREACH_OUTCOMES),
		"genders": sorted(frappe.get_all("Gender", pluck="name")),
	}


def icd11_rows() -> list[list]:
	from onerc_healthcare.clinical.diagnosis import EXCLUDED_CHAPTER
	from onerc_healthcare.coding.setup import ICD11_CODE_SYSTEM

	rows = frappe.db.sql(
		"""
		select name, code_value, display, krcs_is_leaf
		from `tabCode Value`
		where code_system = %s and ifnull(krcs_chapter, '') != %s
		order by code_value
		""",
		(ICD11_CODE_SYSTEM, EXCLUDED_CHAPTER),
	)
	return [[name, code, display or "", cint(leaf)] for name, code, display, leaf in rows]


def medications() -> list[dict]:
	rows = frappe.get_all(
		"Medication",
		filters={"disabled": 0},
		fields=[
			"name",
			"generic_name",
			"strength",
			"strength_uom",
			"dosage_form",
			"default_prescription_dosage",
			"default_prescription_duration",
		],
		order_by="name asc",
	)
	return [
		{
			"name": r.name,
			"generic_name": r.generic_name,
			"strength": (f"{flt(r.strength):g} {r.strength_uom or ''}".strip() if flt(r.strength) else None),
			"dosage_form": r.dosage_form,
			"default_dosage": r.default_prescription_dosage,
			"default_period": r.default_prescription_duration,
		}
		for r in rows
	]


def dosage_units() -> dict[str, float]:
	"""{Prescription Dosage: doses per day}: the sum of its Dosage Strength `strength` rows (Marley's
	`DrugPrescription.get_quantity`)."""
	out = {name: 0.0 for name in frappe.get_all("Prescription Dosage", pluck="name")}
	for row in frappe.get_all(
		"Dosage Strength",
		filters={"parenttype": "Prescription Dosage", "parentfield": "dosage_strength"},
		fields=["parent", "strength"],
	):
		if row.parent in out:
			out[row.parent] += flt(row.strength)
	return {name: _number(value) for name, value in sorted(out.items())}


def period_days() -> dict[str, float]:
	"""{Prescription Duration: days} (Marley's `PrescriptionDuration.get_days`: Hour / 24, Week x 7, Month x 30)."""
	out = {}
	for row in frappe.get_all("Prescription Duration", fields=["name", "number", "period"], order_by="name"):
		duration = frappe.get_doc(
			{"doctype": "Prescription Duration", "number": row.number, "period": row.period}
		)
		out[row.name] = _number(flt(duration.get_days()))
	return out


def _number(value: float):
	"""An int when whole (so the JSON and the version hash stay stable), else the float."""
	return int(value) if float(value).is_integer() else value


def durations() -> list[str]:
	order = {"Hour": 0, "Day": 1, "Week": 2, "Month": 3}
	rows = frappe.get_all("Prescription Duration", fields=["name", "number", "period"])
	rows.sort(key=lambda r: (order.get(r.period, 9), cint(r.number), r.name))
	return [r.name for r in rows]


def lab_templates() -> list[dict]:
	from onerc_healthcare.setup.operations import load_data

	try:
		catalogue_tests = {row["name"] for row in load_data("lab_catalogue.json")["single_tests"]}
	except (OSError, KeyError):
		catalogue_tests = set()
	components = set(
		frappe.get_all(
			"Observation Component",
			filters={"parenttype": "Observation Template"},
			pluck="observation_template",
		)
	)
	meta = frappe.get_meta("Observation Template")
	poc = meta.has_field(POINT_OF_CARE_FIELD)
	fields = [
		"name",
		"abbr",
		"permitted_data_type",
		"options",
		"permitted_unit",
		"has_component",
		"sample_collection_required",
	] + ([POINT_OF_CARE_FIELD] if poc else [])
	out = []
	for r in frappe.get_all("Observation Template", fields=fields, order_by="name asc"):
		is_poc = cint(r.get(POINT_OF_CARE_FIELD)) if poc else 0
		if not (
			is_poc
			or cint(r.has_component)
			or r.name in catalogue_tests
			or (cint(r.sample_collection_required) and r.name not in components)
		):
			continue
		options = [o for o in (r.options or "").split("\n") if o.strip()]
		out.append(
			{
				"name": r.name,
				"abbr": r.abbr,
				"point_of_care": is_poc,
				"data_type": r.permitted_data_type or None,
				"options": options or None,
				"unit": r.permitted_unit or None,
			}
		)
	return out


def identifier_types() -> list[dict]:
	rows = frappe.get_all(
		PATIENT_IDENTIFIER_TYPE_DOCTYPE,
		filters={"enabled": 1},
		fields=["name", "is_primary", "value_pattern"],
		order_by="is_primary desc, name asc",
	)
	return [
		{
			"name": r.name,
			"is_primary": cint(r.is_primary),
			"pattern": r.value_pattern or None,
			"facility_generated": int(r.name == IDENTIFIER_FACILITY),
		}
		for r in rows
	]


def residences() -> list[dict]:
	from onerc_healthcare.registration.residence import get_residence_root_name

	filters = {"enabled": 1}
	root = get_residence_root_name()
	bounds = frappe.db.get_value(RESIDENCE_DOCTYPE, root, ["lft", "rgt"], as_dict=True) if root else None
	if bounds:
		filters.update({"lft": [">=", bounds.lft], "rgt": ["<=", bounds.rgt]})
	rows = frappe.get_all(
		RESIDENCE_DOCTYPE,
		filters=filters,
		fields=["name", "location_name", "parent_residence_location", "is_group", "level"],
		order_by="lft asc",
	)
	return [
		{
			"name": r.name,
			"location_name": r.location_name,
			"parent": r.parent_residence_location,
			"is_group": cint(r.is_group),
			"level": r.level,
		}
		for r in rows
	]


# ----------------------------------------------------------------------------------------------- patients


def catchment_bounds(catchment_area: str) -> list[tuple[int, int]]:
	"""(lft, rgt) of each residence location listed on the catchment area."""
	rows = frappe.db.sql(
		f"""
		select r.lft, r.rgt
		from `tab{CATCHMENT_LOCATION_DOCTYPE}` c
		inner join `tab{RESIDENCE_DOCTYPE}` r on r.name = c.residence_location
		where c.parent = %s and c.parenttype = 'Catchment Area'
		order by r.lft
		""",
		(catchment_area,),
	)
	return [(cint(lft), cint(rgt)) for lft, rgt in rows]


def patient_in_catchment(patient: str, catchment_area: str | None) -> bool:
	"""`patient`'s `krcs_residence` lies in one of the catchment area's residence subtrees (protocol §6 F5)."""
	if not patient or not catchment_area:
		return False
	residence = frappe.db.get_value("Patient", patient, "krcs_residence")
	lft = frappe.db.get_value(RESIDENCE_DOCTYPE, residence, "lft") if residence else None
	if lft is None:
		return False
	return any(low <= cint(lft) <= high for low, high in catchment_bounds(catchment_area))


def _patient_conditions(bounds: list[tuple[int, int]]) -> tuple[str, dict]:
	meta = frappe.get_meta("Patient")
	values: dict = {}
	ranges = []
	for i, (lft, rgt) in enumerate(bounds):
		values[f"lft{i}"], values[f"rgt{i}"] = lft, rgt
		ranges.append(f"(r.lft >= %(lft{i})s and r.lft <= %(rgt{i})s)")
	conditions = [f"({' or '.join(ranges)})" if ranges else "1=0", "ifnull(p.status, '') != 'Disabled'"]
	if meta.has_field(MERGED_INTO_FIELD):
		conditions.append(f"ifnull(p.{MERGED_INTO_FIELD}, '') = ''")
	if meta.has_field(ARCHIVED_FIELD):
		conditions.append(f"ifnull(p.{ARCHIVED_FIELD}, 0) = 0")
	return " and ".join(conditions), values


PATIENT_FROM = f"`tabPatient` p inner join `tab{RESIDENCE_DOCTYPE}` r on r.name = p.krcs_residence"


def count_patients(bounds) -> int:
	where, values = _patient_conditions(bounds)
	return cint(frappe.db.sql(f"select count(*) from {PATIENT_FROM} where {where}", values)[0][0])


def patient_rows(bounds, start: int, limit: int) -> list[frappe._dict]:
	where, values = _patient_conditions(bounds)
	meta = frappe.get_meta("Patient")
	estimated = f"p.{PATIENT_DOB_ESTIMATED_FIELD}" if meta.has_field(PATIENT_DOB_ESTIMATED_FIELD) else "0"
	values.update({"start": cint(start), "limit": cint(limit)})
	return frappe.db.sql(
		f"""
		select p.name, p.modified, p.first_name, p.middle_name, p.last_name, p.sex, p.dob,
			{estimated} as dob_estimated, p.mobile, p.krcs_residence as residence, p.allergies
		from {PATIENT_FROM}
		where {where}
		order by p.name
		limit %(limit)s offset %(start)s
		""",
		values,
		as_dict=True,
	)


def _excluded_departments() -> tuple[str, ...]:
	return tuple(OUTREACH_EXCLUDED_DEPARTMENTS) or ("",)


def _encounter_department_sql(enc: str, visit: str) -> str:
	"""SQL of an encounter's department, in `privacy.access_log._department_of` order."""
	return (
		f"coalesce(nullif({enc}.krcs_department, ''), nullif({enc}.medical_department, ''),"
		f" {visit}.current_department, '')"
	)


def _has_field(doctype: str, fieldname: str) -> bool:
	return frappe.get_meta(doctype).has_field(fieldname)


def _no_sensitive_service_sql(visit: str) -> str:
	"""SQL condition: visit `visit` has no Service Delivered of a Visit Service Type marked sensitive."""
	if not _has_field(SERVICE_TYPE_DOCTYPE, "sensitive"):
		return ""
	return f"""
			and not exists (
				select 1 from `tab{SERVICE_DELIVERED_DOCTYPE}` s
				inner join `tab{SERVICE_TYPE_DOCTYPE}` st on st.name = s.service_type
				where s.visit = {visit}.name and st.sensitive = 1
			)"""


def recent_visits(patients: list[str], count: int) -> dict[str, list[dict]]:
	"""{patient: [visit summaries, newest first]} — at most `count` per patient, Mental Health never."""
	if not patients or count <= 0:
		return {}
	archived_v = f"and ifnull(v.{ARCHIVED_FIELD}, 0) = 0" if _has_field(VISIT_DOCTYPE, ARCHIVED_FIELD) else ""
	sensitive_service = _no_sensitive_service_sql("v")
	visits = frappe.db.sql(
		f"""
		select name, patient, visit_date, facility, current_department
		from (
			select v.name, v.patient, v.visit_date, v.facility, v.current_department,
				row_number() over (
					partition by v.patient order by v.visit_date desc, v.arrived_at desc, v.name desc
				) as rn
			from `tab{VISIT_DOCTYPE}` v
			where v.patient in %(patients)s
				{archived_v}
				and ifnull(v.closure_reason, '') not in %(closures)s
				and ifnull(v.current_department, '') not in %(excluded)s
				and not exists (
					select 1 from `tab{ENCOUNTER_DOCTYPE}` e
					where e.krcs_visit = v.name
						and {_encounter_department_sql("e", "v")} in %(excluded)s
				)
				{sensitive_service}
		) ranked
		where rn <= %(count)s
		order by patient, rn
		""",
		{
			"patients": tuple(patients),
			"closures": tuple(REPORT_EXCLUDED_CLOSURE_REASONS),
			"excluded": _excluded_departments(),
			"count": cint(count),
		},
		as_dict=True,
	)
	diagnoses = _visit_diagnoses([v.name for v in visits], patients)
	out: dict[str, list[dict]] = {}
	for v in visits:
		out.setdefault(v.patient, []).append(
			{
				"date": str(v.visit_date) if v.visit_date else None,
				"facility": v.facility,
				"department": v.current_department,
				"diagnoses": diagnoses.get(v.name, []),
			}
		)
	return out


def _visit_diagnoses(visits: list[str], patients: list[str]) -> dict[str, list[dict]]:
	"""{visit: [{code, title, case_type}]} from the visits' submitted, unarchived encounters."""
	if not visits:
		return {}
	from onerc_healthcare.clinical.diagnosis import DIAGNOSES_FIELD
	from onerc_healthcare.privacy.access_log import is_sensitive

	archived_e = (
		f"and ifnull(e.{ARCHIVED_FIELD}, 0) = 0" if _has_field(ENCOUNTER_DOCTYPE, ARCHIVED_FIELD) else ""
	)
	rows = frappe.db.sql(
		f"""
		select e.krcs_visit as visit, e.name as encounter, e.krcs_department, e.medical_department,
			d.code_value as code, d.title, d.case_type
		from `tab{ENCOUNTER_DOCTYPE}` e
		inner join `tab{ENCOUNTER_DIAGNOSIS_DOCTYPE}` d
			on d.parent = e.name and d.parenttype = %(enc_dt)s and d.parentfield = %(field)s
		where e.patient in %(patients)s and e.krcs_visit in %(visits)s and e.docstatus = 1 {archived_e}
		order by e.krcs_visit, e.encounter_date, e.name, d.is_primary desc, d.idx
		""",
		{
			"patients": tuple(patients),
			"visits": tuple(visits),
			"enc_dt": ENCOUNTER_DOCTYPE,
			"field": DIAGNOSES_FIELD,
		},
		as_dict=True,
	)
	out: dict[str, list[dict]] = {}
	checked: dict[str, bool] = {}
	for r in rows:
		if r.encounter not in checked:
			checked[r.encounter] = is_sensitive(
				ENCOUNTER_DOCTYPE,
				{
					"krcs_department": r.krcs_department,
					"medical_department": r.medical_department,
					"krcs_visit": r.visit,
				},
			)
		if checked[r.encounter]:
			continue  # never reached: the visit query already removed Mental Health (belt and braces)
		out.setdefault(r.visit, []).append({"code": r.code, "title": r.title, "case_type": r.case_type})
	return out


def _prescription_end(start: date, period: str | None, durations: dict) -> date:
	"""First day the prescription no longer runs: the encounter date is day 1 of `period`."""
	row = durations.get(period) if period else None
	number = cint(row.number) if row else 0
	unit = row.period if row else None
	if number <= 0 or unit not in ("Hour", "Day", "Week", "Month"):
		return start + timedelta(days=UNKNOWN_PERIOD_DAYS)
	if unit == "Month":
		return getdate(add_months(start, number))
	if unit == "Week":
		return start + timedelta(weeks=number)
	if unit == "Hour":
		return start + timedelta(days=-(-number // 24))
	return start + timedelta(days=number)


def active_prescriptions(patients: list[str]) -> dict[str, list[dict]]:
	"""{patient: [{medication, dosage, period, date}]} still running today, never from Mental Health."""
	if not patients:
		return {}
	archived_e = (
		f"and ifnull(e.{ARCHIVED_FIELD}, 0) = 0" if _has_field(ENCOUNTER_DOCTYPE, ARCHIVED_FIELD) else ""
	)
	mr_meta = frappe.get_meta("Medication Request")
	dispense = "mr.krcs_dispense_status" if mr_meta.has_field("krcs_dispense_status") else "null"
	hold = "mr.krcs_hold_status" if mr_meta.has_field("krcs_hold_status") else "null"
	since = getdate(today()) - timedelta(days=PRESCRIPTION_LOOKBACK_DAYS)
	rows = frappe.db.sql(
		f"""
		select e.patient, e.name as encounter, e.encounter_date, e.krcs_department, e.medical_department,
			e.krcs_visit, dp.medication, dp.drug_name, dp.drug_code, dp.dosage, dp.period,
			mr.status as mr_status, {dispense} as dispense_status, {hold} as hold_status
		from `tab{ENCOUNTER_DOCTYPE}` e
		inner join `tabDrug Prescription` dp
			on dp.parent = e.name and dp.parenttype = %(enc_dt)s and dp.parentfield = 'drug_prescription'
		left join `tab{VISIT_DOCTYPE}` v on v.name = e.krcs_visit
		left join `tabMedication Request` mr on mr.name = dp.medication_request
		where e.patient in %(patients)s and e.docstatus = 1 and e.encounter_date >= %(since)s
			{archived_e}
			and {_encounter_department_sql("e", "v")} not in %(excluded)s
			and ifnull(v.current_department, '') not in %(excluded)s
			{_no_sensitive_service_sql("v")}
		order by e.patient, e.encounter_date desc, e.name, dp.idx
		""",
		{
			"patients": tuple(patients),
			"enc_dt": ENCOUNTER_DOCTYPE,
			"since": since,
			"excluded": _excluded_departments(),
		},
		as_dict=True,
	)
	if not rows:
		return {}
	from onerc_healthcare.privacy.access_log import is_sensitive

	durations = {
		r.name: r for r in frappe.get_all("Prescription Duration", fields=["name", "number", "period"])
	}
	now = getdate(today())
	out: dict[str, list[dict]] = {}
	for r in rows:
		if (
			r.mr_status in ENDED_MR_STATUSES
			or r.dispense_status in ENDED_DISPENSE_STATUSES
			or r.hold_status in ENDED_HOLD_STATUSES
		):
			continue
		if not r.encounter_date or _prescription_end(getdate(r.encounter_date), r.period, durations) <= now:
			continue
		if is_sensitive(
			ENCOUNTER_DOCTYPE,
			{
				"krcs_department": r.krcs_department,
				"medical_department": r.medical_department,
				"krcs_visit": r.krcs_visit,
			},
		):
			continue  # never reached (filtered in SQL); belt and braces
		out.setdefault(r.patient, []).append(
			{
				"medication": r.medication or r.drug_name or r.drug_code,
				"dosage": r.dosage,
				"period": r.period,
				"date": str(r.encounter_date),
			}
		)
	return out


def identifiers(patients: list[str]) -> dict[str, list[dict]]:
	if not patients:
		return {}
	rows = frappe.get_all(
		PATIENT_IDENTIFIER_DOCTYPE,
		filters={
			"parent": ["in", patients],
			"parenttype": "Patient",
			"parentfield": PATIENT_IDENTIFIERS_FIELD,
		},
		fields=["parent", "identifier_type", "identifier_value"],
		order_by="parent asc, idx asc",
	)
	out: dict[str, list[dict]] = {}
	for r in rows:
		out.setdefault(r.parent, []).append(
			{"identifier_type": r.identifier_type, "identifier_value": r.identifier_value}
		)
	return out


def build_patients(rows: list, summary_visits: int) -> list[dict]:
	names = [r.name for r in rows]
	ids = identifiers(names)
	visits = recent_visits(names, summary_visits)
	prescriptions = active_prescriptions(names)
	return [
		{
			"name": r.name,
			"modified": str(r.modified),
			"first_name": r.first_name,
			"middle_name": r.middle_name or None,
			"last_name": r.last_name or None,
			"sex": r.sex,
			"dob": str(r.dob) if r.dob else None,
			"dob_estimated": cint(r.dob_estimated),
			"mobile": r.mobile or None,
			"residence": r.residence,
			"allergies": r.allergies or None,
			"identifiers": ids.get(r.name, []),
			"recent_visits": visits.get(r.name, []),
			"active_prescriptions": prescriptions.get(r.name, []),
		}
		for r in rows
	]


def log_package(session: str, patients: list[str], page: int) -> int:
	"""One Access Log Entry ("Outreach package", not sensitive) per patient sent, in one bulk insert."""
	if not patients:
		return 0
	from onerc_healthcare.privacy.access_log import build_entry

	user = frappe.session.user
	now = now_datetime()
	details = f"Outreach session {session}, package page {page}"
	fields = [
		"name",
		"creation",
		"modified",
		"owner",
		"modified_by",
		"docstatus",
		"idx",
		"timestamp",
		"user",
		"action",
		"sensitive",
		"reference_doctype",
		"reference_name",
		"patient",
		"ip",
		"details",
	]
	values = []
	for patient in patients:
		entry = build_entry(
			ACCESS_ACTION_OUTREACH_PACKAGE,
			reference_doctype="Patient",
			reference_name=patient,
			patient=patient,
			sensitive=False,
			details=details,
			user=user,
			timestamp=now,
		)
		values.append(
			(
				frappe.generate_hash(length=10),
				now,
				now,
				user,
				user,
				0,
				0,
				entry.timestamp,
				entry.user,
				entry.action,
				entry.sensitive,
				entry.reference_doctype,
				entry.reference_name,
				entry.patient,
				entry.ip,
				entry.details,
			)
		)
	frappe.db.bulk_insert(ACCESS_LOG_DOCTYPE, fields, values)
	return len(values)


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=600, seconds=60 * 60, ip_based=False, user_based=True)
def get_patients(session: str, page: int = 0, page_size: int = DEFAULT_PAGE_SIZE) -> dict:
	"""One page of the session's patient package (protocol §3)."""
	from onerc_healthcare.emr_outreach.doctype.emr_outreach_settings.emr_outreach_settings import get_settings

	row = assert_package_access(session)
	settings = get_settings()
	page = max(cint(page), 0)
	page_size = min(max(cint(page_size) or DEFAULT_PAGE_SIZE, 1), MAX_PAGE_SIZE)
	bounds = catchment_bounds(row.catchment_area)
	total = min(count_patients(bounds), cint(settings.package_max_patients))
	start = page * page_size
	limit = max(0, min(page_size, total - start))
	rows = patient_rows(bounds, start, limit) if limit else []
	patients = build_patients(rows, cint(settings.summary_visits))
	log_package(session, [p["name"] for p in patients], page)
	activate_on_download(session)
	frappe.local.flags.commit = True  # Frappe rolls back a GET: keep the access log and the activation
	return {"page": page, "has_more": start + limit < total, "total": total, "patients": patients}
