# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""First attendance vs re-attendance (MOH 717; Phase 3 decision 2026-11-13). Owner: R3.

**Rule** (`set_initial_attendance`, called by the Patient Visit controller on insert, after the patient
checks and under the patient row lock, and when reception corrects the visit's patient):

1. A visit opened by an **internal referral** is never a new attendance: Re-attendance, basis
   "Calendar-year rule", with a note naming the source visit. The referral code inserts the new visit
   *before* `receive_referral` sets `referred_from`, so the visit is recognised by its pending internal
   referral: a Draft internal Referral of this patient, with no target visit yet, whose source visit at
   this facility was closed as Referred today (`referral.send` runs all of this in one transaction).
   `referred_from`, when already set, counts too.
2. Otherwise "New" when the patient has no other visit at the same facility with `visit_date` in the
   same calendar year, leaving out visits closed "Registered in error" or "Superseded by a new visit"
   (`REPORT_EXCLUDED_CLOSURE_REASONS`); else "Re-attendance". Basis "Calendar-year rule".
3. **Seen in SmartCare**: when the rule says New and reception ticked "Seen here before (SmartCare)",
   the visit becomes Re-attendance with basis "Reception override" and a note, if EMR Registration
   Settings allow reception overrides (the default). Otherwise the tick is kept and the visit stays New.

**Corrections** (POST, through `engine.annotate`, reason required, logged on the visit's status history):

| Endpoint | Nurse Receptionist | HIS Officer | Basis set |
|---|---|---|---|
| `correct_attendance` | while the visit is Registered, if overrides are allowed | any time | Reception override / HIS correction |
| `correct_referred_in` | while the visit is Registered | any time | (intake fields) |

Administrator counts as HIS Officer. Other roles are refused. `CORRECTIONS` lists these rules for audits.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import cint, getdate

from onerc_healthcare.constants import (
	ATTENDANCE_BASIS_HIS,
	ATTENDANCE_BASIS_RECEPTION,
	ATTENDANCE_BASIS_RULE,
	ATTENDANCE_NEW,
	ATTENDANCE_REVISIT,
	ATTENDANCE_TYPES,
	REFERRAL_DOCTYPE,
	REFERRAL_FACILITY_DOCTYPE,
	REPORT_EXCLUDED_CLOSURE_REASONS,
	ROLE_HIS,
	ROLE_RECEPTIONIST,
	VISIT_DOCTYPE,
	VISIT_STATUS_REFERRED,
	VISIT_STATUS_REGISTERED,
)

SETTINGS_DOCTYPE = "EMR Registration Settings"
YEAR_BASIS_CALENDAR = "Calendar year"
YEAR_BASES = (YEAR_BASIS_CALENDAR,)

ACTION_CORRECT_ATTENDANCE = "correct_attendance"
ACTION_CORRECT_REFERRED_IN = "correct_referred_in"

# Referral values written by clinical/referral.py (kept local: they are B1's module constants).
REFERRAL_TYPE_INTERNAL = "Internal"
REFERRAL_STATUS_DRAFT = "Draft"

NOTE_MAX_LENGTH = 1000


class AttendanceCorrectionError(frappe.ValidationError):
	"""The correction is not possible (unknown value, nothing changed)."""


@dataclass(frozen=True)
class Correction:
	"""Who may make a correction: `anytime_roles` at any status, `registered_roles` while Registered."""

	label: str
	anytime_roles: tuple[str, ...]
	registered_roles: tuple[str, ...]
	# Reception may correct only when EMR Registration Settings allow reception overrides.
	reception_needs_override_setting: bool = False


CORRECTIONS: dict[str, Correction] = {
	ACTION_CORRECT_ATTENDANCE: Correction(
		_("Correct Attendance"), (ROLE_HIS,), (ROLE_RECEPTIONIST,), reception_needs_override_setting=True
	),
	ACTION_CORRECT_REFERRED_IN: Correction(_("Correct Referred In"), (ROLE_HIS,), (ROLE_RECEPTIONIST,)),
}


# --------------------------------------------------------------------------- settings


def _single_raw(field: str):
	"""Raw stored value of an EMR Registration Settings field (None when never saved)."""
	row = frappe.db.sql(
		"select value from `tabSingles` where doctype = %s and field = %s", (SETTINGS_DOCTYPE, field)
	)
	return row[0][0] if row else None


def get_year_basis() -> str:
	value = _single_raw("attendance_year_basis")
	return value if value in YEAR_BASES else YEAR_BASIS_CALENDAR


def reception_override_allowed() -> bool:
	"""EMR Registration Settings "Allow Reception Attendance Override" (on until an admin turns it off)."""
	value = _single_raw("allow_reception_attendance_override")
	return True if value in (None, "") else bool(cint(value))


def year_bounds(visit_date) -> tuple:
	"""First and last day of the attendance year containing `visit_date` (calendar year for now)."""
	d = getdate(visit_date)
	return d.replace(month=1, day=1), d.replace(month=12, day=31)


# --------------------------------------------------------------------------- rule


def internal_referral_source(doc) -> str | None:
	"""The source visit when `doc` is opened by an internal referral, else None (see module docstring)."""
	if doc.get("referred_from"):
		return frappe.db.get_value(REFERRAL_DOCTYPE, doc.referred_from, "source_visit") or doc.referred_from
	if not doc.patient or not doc.facility or not frappe.db.table_exists(REFERRAL_DOCTYPE):
		return None
	rows = frappe.db.sql(
		f"""select sv.name from `tab{REFERRAL_DOCTYPE}` r
		join `tab{VISIT_DOCTYPE}` sv on sv.name = r.source_visit
		where r.patient = %(patient)s and r.referral_type = %(internal)s and r.status = %(draft)s
			and ifnull(r.target_visit, '') = '' and sv.name != %(name)s and sv.facility = %(facility)s
			and sv.status = %(referred)s and date(sv.closed_at) = %(visit_date)s
		order by sv.closed_at desc limit 1""",
		{
			"patient": doc.patient,
			"internal": REFERRAL_TYPE_INTERNAL,
			"draft": REFERRAL_STATUS_DRAFT,
			"name": doc.name or "",
			"facility": doc.facility,
			"referred": VISIT_STATUS_REFERRED,
			"visit_date": str(getdate(doc.visit_date)),
		},
	)
	return rows[0][0] if rows else None


def earlier_visit_this_year(doc) -> str | None:
	"""Another visit of the patient at this facility in the visit's attendance year that counts, or None."""
	start, end = year_bounds(doc.visit_date)
	rows = frappe.db.sql(
		f"""select name from `tab{VISIT_DOCTYPE}`
		where patient = %(patient)s and facility = %(facility)s and name != %(name)s
			and ifnull(closure_reason, '') not in %(excluded)s
			and visit_date between %(start)s and %(end)s
		order by visit_date asc, creation asc limit 1""",
		{
			"patient": doc.patient,
			"facility": doc.facility,
			"name": doc.name or "",
			"excluded": list(REPORT_EXCLUDED_CLOSURE_REASONS),
			"start": start,
			"end": end,
		},
	)
	return rows[0][0] if rows else None


def compute_attendance(doc) -> tuple[str, str, str | None]:
	"""(attendance_type, attendance_basis, attendance_note) for a visit being registered."""
	source = internal_referral_source(doc)
	if source:
		return (
			ATTENDANCE_REVISIT,
			ATTENDANCE_BASIS_RULE,
			_("Internal referral from visit {0}: not a new attendance.").format(source),
		)
	earlier = earlier_visit_this_year(doc)
	if earlier:
		return (
			ATTENDANCE_REVISIT,
			ATTENDANCE_BASIS_RULE,
			_("Earlier visit this year at this facility: {0}.").format(earlier),
		)
	if cint(doc.get("seen_in_smartcare")):
		if reception_override_allowed():
			return (
				ATTENDANCE_REVISIT,
				ATTENDANCE_BASIS_RECEPTION,
				_("Seen here before (SmartCare), ticked at registration."),
			)
		return (
			ATTENDANCE_NEW,
			ATTENDANCE_BASIS_RULE,
			_("Seen here before (SmartCare) was ticked, but reception overrides are turned off."),
		)
	return ATTENDANCE_NEW, ATTENDANCE_BASIS_RULE, None


def set_initial_attendance(doc) -> None:
	"""Set doc.attendance_type / attendance_basis (and attendance_note) on an unsaved or corrected visit.

	Must not save, commit or touch other records.
	"""
	if not doc.get("patient") or not doc.get("facility") or not doc.get("visit_date"):
		return
	doc.attendance_type, doc.attendance_basis, doc.attendance_note = compute_attendance(doc)


# --------------------------------------------------------------------------- corrections


def can_correct(action: str, visit_status: str, user: str | None = None) -> bool:
	"""Whether `user` may make correction `action` on a visit in `visit_status`."""
	rule = CORRECTIONS[action]
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	roles = set(frappe.get_roles(user))
	if roles & set(rule.anytime_roles):
		return True
	if visit_status != VISIT_STATUS_REGISTERED or not roles & set(rule.registered_roles):
		return False
	return not rule.reception_needs_override_setting or reception_override_allowed()


def _anytime(action: str, user: str) -> bool:
	return user == "Administrator" or bool(
		set(frappe.get_roles(user)) & set(CORRECTIONS[action].anytime_roles)
	)


def _check_correction(action: str, visit: str):
	"""Role, status and read checks. Returns the visit's current values."""
	rule = CORRECTIONS[action]
	if not frappe.db.exists(VISIT_DOCTYPE, visit):
		frappe.throw(_("Patient Visit {0} does not exist.").format(visit), frappe.DoesNotExistError)
	frappe.has_permission(VISIT_DOCTYPE, "read", doc=visit, throw=True)
	doc = frappe.get_doc(VISIT_DOCTYPE, visit)
	if not can_correct(action, doc.status):
		roles = set(frappe.get_roles())
		if roles & set(rule.registered_roles) and doc.status != VISIT_STATUS_REGISTERED:
			message = _("{0} is possible at reception only while the visit is Registered. Ask the {1}.")
			frappe.throw(
				message.format(frappe.bold(rule.label), _(ROLE_HIS)),
				frappe.PermissionError,
				title=_("Not Allowed"),
			)
		if roles & set(rule.registered_roles):
			frappe.throw(
				_(
					"Reception overrides of attendance are turned off in EMR Registration Settings. Ask the {0}."
				).format(_(ROLE_HIS)),
				frappe.PermissionError,
				title=_("Not Allowed"),
			)
		frappe.throw(
			_("You are not allowed to {0}. Required role: {1}.").format(
				rule.label, ", ".join(_(r) for r in (*rule.anytime_roles, *rule.registered_roles))
			),
			frappe.PermissionError,
		)
	return doc


def _reason(reason: str | None) -> str:
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(
			_("Enter a reason; it is recorded in the visit history."),
			AttendanceCorrectionError,
			title=_("Reason Required"),
		)
	return reason


@frappe.whitelist(methods=["POST"])
def correct_attendance(visit: str, attendance_type: str, reason: str) -> dict:
	"""Correct New / Re-attendance: Nurse Receptionist while Registered (Reception override), HIS Officer
	any time (HIS correction). Logged as `correct_attendance` with the reason."""
	reason = _reason(reason)
	doc = _check_correction(ACTION_CORRECT_ATTENDANCE, visit)
	if attendance_type not in ATTENDANCE_TYPES:
		frappe.throw(
			_("Attendance must be one of: {0}.").format(", ".join(_(t) for t in ATTENDANCE_TYPES)),
			AttendanceCorrectionError,
		)
	basis = (
		ATTENDANCE_BASIS_HIS
		if _anytime(ACTION_CORRECT_ATTENDANCE, frappe.session.user)
		else ATTENDANCE_BASIS_RECEPTION
	)
	if doc.attendance_type == attendance_type and doc.attendance_basis == basis:
		frappe.throw(
			_("Visit {0} is already recorded as {1} ({2}).").format(visit, _(attendance_type), _(basis)),
			AttendanceCorrectionError,
			title=_("No Change"),
		)
	from onerc_healthcare.visit import engine

	doc = engine.annotate(
		visit,
		ACTION_CORRECT_ATTENDANCE,
		_("Attendance {0} → {1}: {2}").format(_(doc.attendance_type or "-"), _(attendance_type), reason),
		{
			"attendance_type": attendance_type,
			"attendance_basis": basis,
			"attendance_note": reason[:NOTE_MAX_LENGTH],
		},
	)
	return {
		"name": doc.name,
		"attendance_type": doc.attendance_type,
		"attendance_basis": doc.attendance_basis,
	}


@frappe.whitelist(methods=["POST"])
def correct_referred_in(
	visit: str, referred_in: int, reason: str, referred_in_from: str | None = None
) -> dict:
	"""Correct "Referred in from another facility": Nurse Receptionist while Registered, HIS Officer any
	time. Logged as `correct_referred_in` with the reason."""
	reason = _reason(reason)
	doc = _check_correction(ACTION_CORRECT_REFERRED_IN, visit)
	referred_in = 1 if cint(referred_in) else 0
	referred_in_from = (referred_in_from or "").strip() or None
	if not referred_in:
		referred_in_from = None
	if referred_in_from and not frappe.db.exists(REFERRAL_FACILITY_DOCTYPE, referred_in_from):
		frappe.throw(
			_("Referral Facility {0} does not exist.").format(frappe.bold(referred_in_from)),
			AttendanceCorrectionError,
		)
	if cint(doc.referred_in) == referred_in and (doc.referred_in_from or None) == referred_in_from:
		frappe.throw(_("Nothing to correct."), AttendanceCorrectionError, title=_("No Change"))
	from onerc_healthcare.visit import engine

	before = (
		_("referred in from {0}").format(doc.referred_in_from or "-")
		if doc.referred_in
		else _("not referred in")
	)
	after = _("referred in from {0}").format(referred_in_from or "-") if referred_in else _("not referred in")
	doc = engine.annotate(
		visit,
		ACTION_CORRECT_REFERRED_IN,
		_("{0} → {1}: {2}").format(before, after, reason),
		{"referred_in": referred_in, "referred_in_from": referred_in_from},
	)
	return {"name": doc.name, "referred_in": doc.referred_in, "referred_in_from": doc.referred_in_from}
