# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Immunisation, the KEPI schedule and the Due Immunisations list (MCH-B; docs/MCH_CONTRACT.md §7, CON-16).

**KEPI Vaccine Schedule** (master): one row per dose, seeded once from `mch/data/kepi_schedule.json`
(`seed_schedule` inserts missing ``dose_label`` rows and never edits one, D46). Provisional until OI-8.

**Immunisation** (submittable, one per visit, no cancel; `mch.common` lifecycle):

| Rule | |
|---|---|
| child | has a date of birth and is under 5 years (`GROWTH_MAX_AGE_DAYS`) on ``given_on`` |
| ``given_on`` | not in the future, not before the date of birth; ``age_at_visit`` = `common.age_label` |
| doses | at least one row; a vaccine once per record; the vaccine is an active KEPI row |
| Not given | needs ``reason_not_given`` |
| Given | ``given_by`` set by the server (the user who first saved the row as Given); refused when the vaccine is already Given on another submitted, not archived Immunisation of the child; refused when more than `EARLY_DAYS` (14) before the due age, or sooner than ``min_interval_days`` after the previous Given dose of the antigen (the message gives the date it can be given) |

The Patient row is locked first (lock order Patient -> Visit, as in registration and the maternity admit) and the
child's given doses are read with a locking read, so two drafts cannot both submit the same dose.

**Schedule** (`schedule`, and `schedule_rows` for many children at once, used by the Due Immunisations report):
``due_date = max(dob + due_age_days, previous dose of the antigen + min_interval_days)``. The previous dose is
its Given date; when it is not given yet, its own due date or ``on`` (whichever is later: it can be given
today at the earliest); a Missed previous dose does not hold the next one back.

| Status | When |
|---|---|
| Given | a submitted, not archived Given dose |
| Upcoming | ``on < due_date`` |
| Missed | after ``latest_date`` (``latest_age_days`` 0: never) |
| Due | ``due_date <= on <= due_date + DUE_GRACE_DAYS`` |
| Overdue | after that |
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, cstr, formatdate, getdate, today

from onerc_healthcare.clinical.utils import has_any_role
from onerc_healthcare.constants import (
	CHILD_MCH_ROLES,
	DOSE_GIVEN,
	DOSE_NOT_GIVEN,
	GROWTH_MAX_AGE_DAYS,
	IMMUNISATION_DOCTYPE,
	IMMUNISATION_DOSE_DOCTYPE,
	KEPI_SCHEDULE_DOCTYPE,
	MCH_READ_ROLES,
	PATIENT_DOB_ESTIMATED_FIELD,
	SCHEDULE_DUE,
	SCHEDULE_GIVEN,
	SCHEDULE_MISSED,
	SCHEDULE_OVERDUE,
	SCHEDULE_UPCOMING,
)
from onerc_healthcare.mch import common
from onerc_healthcare.mch.common import MCHRecordError

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "kepi_schedule.json")
# A dose stays Due this many days after its due date, then it is Overdue (contract §7).
DUE_GRACE_DAYS = 28
# A dose given more than this many days before its due age is refused (contract §7).
EARLY_DAYS = 14
ARCHIVED_FIELD = common.ARCHIVED_FIELD
REASON_MAX_LENGTH = 140
SCHEDULE_FIELDS = (
	"name",
	"antigen",
	"dose_number",
	"due_age_days",
	"latest_age_days",
	"min_interval_days",
	"sort_order",
	"is_active",
)
# Who may see a child's schedule (with the Patient read check): those who read Immunisation records.
SCHEDULE_READ_ROLES = (*CHILD_MCH_ROLES, *MCH_READ_ROLES)


# --------------------------------------------------------------------------------------- KEPI master


def schedule_row_validate(doc) -> None:
	"""KEPI Vaccine Schedule `validate`: ages and interval not negative, latest age 0 (none) or not before
	the due age, one row per (antigen, dose number)."""
	doc.antigen = cstr(doc.antigen).strip()
	doc.dose_label = cstr(doc.dose_label).strip()
	errors = []
	for field in ("due_age_days", "latest_age_days", "min_interval_days", "dose_number"):
		if cint(doc.get(field)) < 0:
			errors.append(_("{0} cannot be negative.").format(_(doc.meta.get_label(field))))
	if cint(doc.latest_age_days) and cint(doc.latest_age_days) < cint(doc.due_age_days):
		errors.append(
			_("The latest age ({0} days) is before the due age ({1} days). Use 0 for no upper limit.").format(
				cint(doc.latest_age_days), cint(doc.due_age_days)
			)
		)
	if errors:
		frappe.throw(errors, MCHRecordError, title=_("Check the schedule row"), as_list=True)
	other = frappe.db.get_value(
		KEPI_SCHEDULE_DOCTYPE,
		{"antigen": doc.antigen, "dose_number": cint(doc.dose_number), "name": ("!=", doc.name or "")},
		"name",
	)
	if other:
		frappe.throw(
			_("{0} dose {1} is already in the schedule ({2}).").format(
				doc.antigen, cint(doc.dose_number), other
			),
			MCHRecordError,
			title=_("Duplicate Dose"),
		)


def seed_schedule() -> int:
	"""Insert the KEPI rows of mch/data/kepi_schedule.json that do not exist yet. Never edits a row (D46).
	Returns the number of rows inserted."""
	with open(DATA_FILE, encoding="utf-8") as f:
		rows = json.load(f)["rows"]
	inserted = 0
	for row in rows:
		if frappe.db.exists(KEPI_SCHEDULE_DOCTYPE, row["dose_label"]):
			continue
		# A site may have renamed a row and kept its (antigen, dose number): never add a duplicate.
		if frappe.db.exists(
			KEPI_SCHEDULE_DOCTYPE, {"antigen": row["antigen"], "dose_number": row["dose_number"]}
		):
			continue
		frappe.get_doc({"doctype": KEPI_SCHEDULE_DOCTYPE, "is_active": 1, **row}).insert(
			ignore_permissions=True
		)
		inserted += 1
	return inserted


def _schedule_rows(active_only: bool = True) -> list[frappe._dict]:
	filters = {"is_active": 1} if active_only else {}
	return frappe.get_all(
		KEPI_SCHEDULE_DOCTYPE,
		filters=filters,
		fields=list(SCHEDULE_FIELDS),
		order_by="sort_order asc, due_age_days asc, name asc",
	)


def _previous_rows(rows: list) -> dict[str, frappe._dict | None]:
	"""{row name: the row of the same antigen with the next lower dose number, or None}."""
	by_antigen: dict[str, list] = {}
	for row in rows:
		by_antigen.setdefault(row.antigen, []).append(row)
	out = {}
	for doses in by_antigen.values():
		doses.sort(key=lambda r: cint(r.dose_number))
		prev = None
		for row in doses:
			out[row.name] = prev
			prev = row
	return out


# --------------------------------------------------------------------------------------- Immunisation


def immunisation_validate(doc) -> None:
	if common.guard_draft(doc):
		return
	# Lock order Patient -> Visit, as in registration and the maternity admit (DEFECT-MCH-5).
	child = _child(doc.patient, for_update=True)
	common.check_visit(doc)
	common.guard_one_per_visit(doc)
	common.set_recorded_by(doc)
	_check_given_on(doc, child)
	_check_doses(doc, child)


def immunisation_before_submit(doc) -> None:
	common.before_submit(doc)


def immunisation_before_cancel(doc) -> None:
	common.refuse_cancel(doc)


def immunisation_before_update_after_submit(doc) -> None:
	common.guard_after_submit(doc)


def _child(patient: str, for_update: bool = False) -> frappe._dict:
	"""The Patient's dob, sex and estimated-dob flag (locked when `for_update`: lock order Patient, then
	Visit)."""
	child = frappe.db.get_value(
		"Patient",
		patient,
		["name", "dob", "sex", PATIENT_DOB_ESTIMATED_FIELD],
		as_dict=True,
		for_update=for_update,
	)
	if not child:
		frappe.throw(_("Patient {0} does not exist.").format(patient), MCHRecordError)
	if not child.dob:
		frappe.throw(
			_("Record the child's date of birth on the Patient first."),
			MCHRecordError,
			title=_("Missing Date of Birth"),
		)
	return child


def _check_given_on(doc, child) -> None:
	if not doc.given_on:
		frappe.throw(_("Enter the date the vaccines were given."), MCHRecordError, title=_("Missing Value"))
	given_on = getdate(doc.given_on)
	if given_on > getdate(today()):
		frappe.throw(
			_("The date ({0}) is in the future.").format(formatdate(given_on)),
			MCHRecordError,
			title=_("Check the date"),
		)
	days = common.age_in_days(child.dob, given_on)
	if days < 0:
		frappe.throw(
			_("The date ({0}) is before the child's date of birth ({1}).").format(
				formatdate(given_on), formatdate(child.dob)
			),
			MCHRecordError,
			title=_("Check the date"),
		)
	if days >= GROWTH_MAX_AGE_DAYS:
		frappe.throw(
			_("Immunisation records are for children under 5 years; the child was {0} on {1}.").format(
				common.age_label(days), formatdate(given_on)
			),
			MCHRecordError,
			title=_("Not a Child Under 5"),
		)
	doc.age_at_visit = common.age_label(days)


def _check_doses(doc, child) -> None:
	if not doc.get("doses"):
		frappe.throw(_("Add at least one vaccine dose."), MCHRecordError, title=_("Missing Value"))
	rows = {r.name: r for r in _schedule_rows(active_only=False)}
	previous = _previous_rows([r for r in rows.values() if cint(r.is_active)])
	before = doc.get_doc_before_save()
	given_by_before = {d.name: d.given_by for d in (before.get("doses") if before else [])}
	given_on = getdate(doc.given_on)
	others = _given_doses([child.name], exclude=doc.name, for_update=True).get(child.name, {})  # DEFECT-MCH-4
	in_record = {d.vaccine: d for d in doc.doses if d.status == DOSE_GIVEN}
	seen = set()
	for d in doc.doses:
		sched = rows.get(d.vaccine)
		if not sched:
			frappe.throw(
				_("Row {0}: choose a vaccine dose from the KEPI schedule.").format(d.idx),
				MCHRecordError,
				title=_("Missing Value"),
			)
		if d.vaccine in seen:
			frappe.throw(
				_("{0} is entered twice. Record each vaccine dose once.").format(d.vaccine),
				MCHRecordError,
				title=_("Duplicate Dose"),
			)
		seen.add(d.vaccine)
		if not cint(sched.is_active):
			frappe.throw(
				_("{0} is no longer active in the KEPI schedule.").format(d.vaccine),
				MCHRecordError,
				title=_("Inactive Vaccine"),
			)
		d.antigen = sched.antigen
		if d.status not in (DOSE_GIVEN, DOSE_NOT_GIVEN):
			frappe.throw(
				_("Row {0}: the status is Given or Not given.").format(d.idx),
				MCHRecordError,
				title=_("Missing Value"),
			)
		if d.status == DOSE_NOT_GIVEN:
			d.reason_not_given = cstr(d.reason_not_given).strip()
			if not d.reason_not_given:
				frappe.throw(
					_("{0} was not given: enter the reason.").format(d.vaccine),
					MCHRecordError,
					title=_("Missing Value"),
				)
			d.given_by = None
			continue
		d.given_by = given_by_before.get(d.name) or frappe.session.user
		if d.vaccine in others:
			on, name = others[d.vaccine]
			frappe.throw(
				_("{0} was already given on {1} ({2}).").format(d.vaccine, formatdate(on), name),
				MCHRecordError,
				title=_("Already Given"),
			)
		_check_timing(d, sched, previous.get(sched.name), child, given_on, others, in_record)


def _check_timing(d, sched, prev, child, given_on, others: dict, in_record: dict) -> None:
	dob = getdate(child.dob)
	due = dob + timedelta(days=cint(sched.due_age_days))
	earliest = due - timedelta(days=EARLY_DAYS)
	reason = None
	prev_on = None
	if prev is not None and cint(sched.min_interval_days):
		if prev.name in others:
			prev_on = getdate(others[prev.name][0])
		elif prev.name in in_record:
			prev_on = given_on
	if prev_on is not None:
		after_prev = prev_on + timedelta(days=cint(sched.min_interval_days))
		if after_prev > earliest:
			earliest = after_prev
			reason = _("{0} days after {1} (given on {2})").format(
				cint(sched.min_interval_days), prev.name, formatdate(prev_on)
			)
	if given_on >= earliest:
		return
	if reason:
		message = _("{0} is too early: it is given at least {1}, from {2}.").format(
			d.vaccine, reason, formatdate(earliest)
		)
	else:
		message = _("{0} is too early: it is due on {1} and can be given from {2}.").format(
			d.vaccine, formatdate(due), formatdate(earliest)
		)
	frappe.throw(message, MCHRecordError, title=_("Too Early"))


def not_archived_sql(alias: str) -> str:
	if frappe.get_meta(IMMUNISATION_DOCTYPE).has_field(ARCHIVED_FIELD):
		return f"IFNULL({alias}.`{ARCHIVED_FIELD}`, 0) = 0"
	return "1 = 1"


def _given_doses(
	patients: list[str], exclude: str | None = None, for_update: bool = False
) -> dict[str, dict[str, tuple]]:
	"""{patient: {vaccine: (given_on, immunisation)}}: the earliest Given dose of each vaccine on a
	submitted, not archived Immunisation (one query for every patient)."""
	if not patients:
		return {}
	rows = frappe.db.sql(
		f"""
		SELECT imm.patient, dose.vaccine, imm.given_on, imm.name
		FROM `tab{IMMUNISATION_DOSE_DOCTYPE}` dose
		JOIN `tab{IMMUNISATION_DOCTYPE}` imm ON imm.name = dose.parent
		WHERE dose.parenttype = %(parenttype)s AND dose.parentfield = 'doses'
			AND dose.status = %(given)s AND imm.docstatus = 1 AND {not_archived_sql("imm")}
			AND imm.patient IN %(patients)s AND imm.name != %(exclude)s
		ORDER BY imm.given_on ASC, imm.creation ASC
		{"FOR UPDATE" if for_update else ""}
		""",
		{
			"parenttype": IMMUNISATION_DOCTYPE,
			"given": DOSE_GIVEN,
			"patients": tuple(patients),
			"exclude": exclude or "",
		},
		as_dict=True,
	)
	out: dict[str, dict[str, tuple]] = {}
	for r in rows:
		out.setdefault(r.patient, {}).setdefault(r.vaccine, (getdate(r.given_on), r.name))
	return out


# --------------------------------------------------------------------------------------- schedule


def compute_schedule(rows: list, dob, given: dict[str, tuple], on, dob_estimated: int = 0) -> list[dict]:
	"""A child's schedule on `on` from the active KEPI `rows` (sorted for display) and `given`
	({vaccine: (given_on, immunisation)}). Pure: no database access (used per child by the report)."""
	dob, on = getdate(dob), getdate(on)
	previous = _previous_rows(rows)
	order = sorted(rows, key=lambda r: (r.antigen, cint(r.dose_number)))
	done: dict[str, dict] = {}
	for row in order:
		due = dob + timedelta(days=cint(row.due_age_days))
		prev = previous.get(row.name)
		if prev is not None and cint(row.min_interval_days):
			p = done[prev.name]
			if p["given_on"]:
				anchor = p["given_on"]
			elif p["status"] == SCHEDULE_MISSED:
				anchor = None
			else:
				anchor = max(p["due_date"], on)
			if anchor:
				due = max(due, anchor + timedelta(days=cint(row.min_interval_days)))
		latest = dob + timedelta(days=cint(row.latest_age_days)) if cint(row.latest_age_days) else None
		given_on, immunisation = given.get(row.name) or (None, None)
		if given_on:
			status = SCHEDULE_GIVEN
		elif on < due:
			status = SCHEDULE_UPCOMING
		elif latest and on > latest:
			status = SCHEDULE_MISSED
		elif on <= due + timedelta(days=DUE_GRACE_DAYS):
			status = SCHEDULE_DUE
		else:
			status = SCHEDULE_OVERDUE
		done[row.name] = {
			"vaccine": row.name,
			"antigen": row.antigen,
			"dose_number": cint(row.dose_number),
			"due_date": due,
			"latest_date": latest,
			"status": status,
			"given_on": given_on,
			"immunisation": immunisation,
			"dob_estimated": cint(dob_estimated),
		}
	return [done[row.name] for row in rows]


def schedule_rows(patients: list[dict], on) -> dict[str, list[dict]]:
	"""{patient: schedule} for many children ({name, dob, krcs_dob_estimated}) with two queries in all."""
	rows = _schedule_rows()
	given = _given_doses([p["name"] for p in patients])
	return {
		p["name"]: compute_schedule(
			rows, p["dob"], given.get(p["name"], {}), on, p.get(PATIENT_DOB_ESTIMATED_FIELD)
		)
		for p in patients
		if p.get("dob")
	}


@frappe.whitelist(methods=["GET"])
def schedule(patient: str, on: str | None = None) -> list[dict]:
	"""The child's schedule on date `on` (default today): one dict per active KEPI dose
	{vaccine, antigen, dose_number, due_date, latest_date, status (SCHEDULE_*), given_on, immunisation,
	dob_estimated}. The caller reads the Patient and Immunisation records. [] without a date of birth."""
	if not has_any_role(SCHEDULE_READ_ROLES):
		frappe.throw(_("You are not allowed to see the immunisation schedule."), frappe.PermissionError)
	frappe.has_permission("Patient", "read", doc=patient, throw=True)
	child = frappe.db.get_value(
		"Patient", patient, ["name", "dob", PATIENT_DOB_ESTIMATED_FIELD], as_dict=True
	)
	if not child or not child.dob:
		return []
	rows = schedule_rows([child], getdate(on or today())).get(child.name, [])
	if rows:  # DEFECT-MCH-7: the panel read of the child's immunisation history is access-logged (D44)
		from onerc_healthcare.privacy import access_log

		access_log.log_access(
			access_log.ACTION_READ_API, IMMUNISATION_DOCTYPE, None, patient=patient, detail="schedule"
		)
	return rows
