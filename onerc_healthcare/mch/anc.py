# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""ANC Pregnancy and ANC Contact rules (MCH-A; docs/MCH_CONTRACT.md §5, BRD FR-14, SDD §5.3, D62).

Provisional until the MCH In-Charge confirms the fields (OI-8).

**ANC Pregnancy** (not submittable, track_changes). One open, non-archived pregnancy per mother, checked on
insert under a lock on the Patient row. The mother is a female patient. Dating:

- EDD basis LMP (the default): ``edd = LMP + 280`` (`maternity.admission.edd_from_lmp`), recomputed on
  every save; without an LMP a typed EDD is kept.
- Basis Ultrasound / Clinical estimate: the EDD is typed (required).
- The LMP is not in the future and not more than 45 weeks before the booking date; the EDD gives a
  gestation of 0 to 45 weeks at booking and is after the LMP.
- ``gestation`` is the gestation today (at ``closed_on`` once closed), refreshed on every save and on open.

Gravida and para follow the admission (`maternity.admission.validate_gravida_para`). ``status``,
``closed_*``, ``delivery_record`` and ``closure_note`` are server fields: they change only through
`close_on_delivery` (Delivery Record submit) and `close_pregnancy`. A Closed pregnancy is read-only (the
Medical In-Charge may still archive it, `mch.common.archive`). Archiving an open pregnancy frees the mother
for a new booking.

**ANC Contact** (submittable, one per visit, no cancel; `mch.common`). ``pregnancy`` is the mother's open
pregnancy, set by the server on insert. ``contact_number`` = the pregnancy's non-archived contacts before
this one + 1, set on insert under a lock on the pregnancy row (an archived contact frees its number for the
next contact; numbers already given are never renumbered). ``gestation`` is computed at ``contact_date``.
Plausible ranges are refused (0 = not recorded). Submit needs the pregnancy still Open and not archived.

Lock order (D41): Visit → ANC Pregnancy (contact); Patient → ANC Pregnancy (pregnancy insert); Maternity
Admission → ANC Pregnancy (delivery submit, `close_on_delivery`).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, today

from onerc_healthcare.clinical.utils import require_roles
from onerc_healthcare.constants import (
	ANC_CONTACT_DOCTYPE,
	ANC_PREGNANCY_DOCTYPE,
	DELIVERY_RECORD_DOCTYPE,
	MCH_ROLES,
	PREGNANCY_CLOSED,
	PREGNANCY_DELIVERED,
	PREGNANCY_ENDED,
	PREGNANCY_OPEN,
	PREGNANCY_TRANSFERRED,
	VISIT_DOCTYPE,
)
from onerc_healthcare.maternity import admission as maternity
from onerc_healthcare.mch import common
from onerc_healthcare.privacy import lifecycle

ARCHIVED_FIELD = common.ARCHIVED_FIELD
SERVER_FIELDS = ("status", "closed_reason", "closed_on", "closed_by", "delivery_record", "closure_note")
USER_CLOSE_REASONS = (PREGNANCY_ENDED, PREGNANCY_TRANSFERRED)
NOTE_MAX_LENGTH = common.REASON_MAX_LENGTH
MAX_GESTATION_DAYS = maternity.MAX_GESTATION_DAYS  # 45 weeks
FEMALE = "Female"

# (fieldname, low, high, unit): plausible, not normal, ranges (the visit/events.py VitalRange pattern)
PREGNANCY_RANGES = (
	("haemoglobin", 2, 20, "g/dL"),
	("height_cm", 100, 220, "cm"),  # provisional (D62): not in the contract table
	("muac_cm", 10, 50, "cm"),  # provisional (D62): not in the contract table
)
CONTACT_RANGES = (
	("weight_kg", 25, 200, "kg"),
	("bp_systolic", 50, 250, "mmHg"),
	("bp_diastolic", 30, 150, "mmHg"),
	("foetal_heart_rate", 60, 220, "beats/min"),
	("fundal_height_cm", 5, 50, "cm"),
	("haemoglobin", 2, 20, "g/dL"),
)
CLOSE_SAVEPOINT = "mch_close_on_delivery"


class ImplausibleANCValueError(common.MCHRecordError):
	pass


# --------------------------------------------------------------------------------------- shared


def _archived_sql(alias: str = "") -> str:
	prefix = f"{alias}." if alias else ""
	if frappe.db.has_column(ANC_PREGNANCY_DOCTYPE, ARCHIVED_FIELD):
		return f"IFNULL({prefix}`{ARCHIVED_FIELD}`, 0) = 0"
	return "1 = 1"


def _fmt_date(value) -> str:
	return frappe.format(value, {"fieldtype": "Date"})


def check_female(patient: str | None, what: str) -> None:
	"""Refuse a male (or not female) patient for a mother's record."""
	sex = frappe.db.get_value("Patient", patient, "sex") if patient else None
	if sex != FEMALE:
		frappe.throw(
			_("{0} is for a female patient; {1} is recorded as {2}.").format(
				_(what), patient, _(sex or "sex not recorded")
			),
			common.MCHRecordError,
			title=_("Wrong Patient"),
		)


def check_ranges(doc, ranges, bp_fields: tuple[str, str] | None = None) -> None:
	"""Refuse values outside `ranges` (0 or empty = not recorded); BP both or neither, systolic above
	diastolic. All problems in one message."""
	errors = []
	label = doc.meta.get_label
	for fieldname, low, high, unit in ranges:
		value = flt(doc.get(fieldname))
		if value and not low <= value <= high:
			errors.append(
				_("{0} of {1} {2} is not plausible. Expected between {3} and {4} {2}.").format(
					frappe.bold(_(label(fieldname))), value, unit, low, high
				)
			)
	if bp_fields:
		systolic, diastolic = cint(doc.get(bp_fields[0])), cint(doc.get(bp_fields[1]))
		if bool(systolic) != bool(diastolic):
			errors.append(_("Enter both systolic and diastolic blood pressure, or neither."))
		elif systolic and systolic <= diastolic:
			errors.append(
				_("Systolic blood pressure ({0}) must be higher than diastolic ({1}).").format(
					systolic, diastolic
				)
			)
	if errors:
		frappe.throw(errors, ImplausibleANCValueError, title=_("Check the values"), as_list=True)


def _not_future(doc, fieldname: str) -> None:
	if doc.get(fieldname) and getdate(doc.get(fieldname)) > getdate(today()):
		frappe.throw(
			_("{0} cannot be in the future.").format(_(doc.meta.get_label(fieldname))),
			ImplausibleANCValueError,
			title=_("Check the dates"),
		)


def gestation_label(preg, on=None) -> str | None:
	"""'24+3 weeks' on `on` (default today) from the EDD (or the LMP when there is no EDD)."""
	days = maternity.admission_gestation_days(preg, getdate(on or today()))
	if days is None or days < 0:
		return None
	weeks, rest = divmod(days, 7)
	return _("{0}+{1} weeks").format(weeks, rest)


def _gestation_on(doc):
	"""Gestation date of a pregnancy: today while open, the closing date once closed."""
	if doc.get("status") == PREGNANCY_CLOSED and doc.get("closed_on"):
		return doc.closed_on
	return today()


def open_pregnancy(patient: str, for_update: bool = False) -> str | None:
	"""Name of the patient's open, non-archived ANC Pregnancy, or None. `for_update`: a locking read (take
	it after the Visit / Patient / Admission lock, D41)."""
	if not patient:
		return None
	rows = frappe.db.sql(
		f"""SELECT name FROM `tab{ANC_PREGNANCY_DOCTYPE}`
		WHERE patient = %(patient)s AND status = %(open)s AND {_archived_sql()}
		ORDER BY creation DESC LIMIT 1{" FOR UPDATE" if for_update else ""}""",
		{"patient": patient, "open": PREGNANCY_OPEN},
	)
	return rows[0][0] if rows else None


# --------------------------------------------------------------------------------------- ANC Pregnancy


def pregnancy_validate(doc) -> None:
	if common.guard_draft(doc):
		return  # archive-only change (Medical In-Charge, mch.common.archive)
	if doc.flags.krcs_close:
		return  # the server closing it (_close): nothing else changes
	before = None if doc.is_new() else doc.get_doc_before_save()
	if doc.is_new():
		require_roles(MCH_ROLES, _("book a pregnancy"))
		if not doc.flags.from_factory:
			doc.status = PREGNANCY_OPEN
			for fieldname in SERVER_FIELDS[1:]:
				doc.set(fieldname, None)
	elif before is not None:
		if before.get("status") == PREGNANCY_CLOSED:
			frappe.throw(
				_("Pregnancy {0} is closed ({1}) and cannot be edited.").format(
					doc.name, _(before.get("closed_reason") or PREGNANCY_CLOSED)
				),
				common.MCHRecordError,
				title=_("Closed"),
			)
		lifecycle.guard_fields(doc, before, SERVER_FIELDS)
	check_female(doc.patient, ANC_PREGNANCY_DOCTYPE)
	_check_booking_visit(doc)
	_not_future(doc, "booking_date")
	_set_dates(doc)
	maternity.validate_gravida_para(doc, before)
	check_ranges(doc, PREGNANCY_RANGES)
	if doc.is_new() and doc.status == PREGNANCY_OPEN:
		_check_one_open(doc)
	doc.gestation = gestation_label(doc, _gestation_on(doc))
	common.set_recorded_by(doc)


def pregnancy_on_update(doc) -> None:
	"""Nothing to propagate: contacts read the pregnancy when they are saved."""


def pregnancy_onload(doc) -> None:
	"""Show today's gestation on open (the stored value is the one of the last save)."""
	if not doc.is_new():
		doc.gestation = gestation_label(doc, _gestation_on(doc))


def _check_booking_visit(doc) -> None:
	if not doc.get("booking_visit"):
		return
	visit = frappe.db.get_value(VISIT_DOCTYPE, doc.booking_visit, ["patient", "facility"], as_dict=True)
	if not visit:
		frappe.throw(_("Patient Visit {0} does not exist.").format(doc.booking_visit), common.MCHRecordError)
	if visit.patient != doc.patient:
		frappe.throw(
			_("Visit {0} belongs to another patient.").format(doc.booking_visit),
			common.MCHRecordError,
			title=_("Wrong Visit"),
		)
	if not doc.get("facility"):
		doc.facility = visit.facility


def _set_dates(doc) -> None:
	"""EDD by its basis; LMP not in the future and at most 45 weeks before booking (module docstring)."""
	basis = doc.get("edd_basis") or maternity.EDD_BASIS_LMP
	if basis not in maternity.EDD_BASES:
		frappe.throw(
			_("The EDD basis must be one of {0}.").format(", ".join(_(b) for b in maternity.EDD_BASES)),
			ImplausibleANCValueError,
		)
	doc.edd_basis = basis
	booking = getdate(doc.booking_date or today())
	lmp = getdate(doc.lmp) if doc.lmp else None
	if lmp:
		if lmp > getdate(today()) or lmp > booking:
			frappe.throw(
				_("The LMP ({0}) cannot be in the future or after the booking date.").format(_fmt_date(lmp)),
				ImplausibleANCValueError,
				title=_("Check the dates"),
			)
		if (booking - lmp).days > MAX_GESTATION_DAYS:
			frappe.throw(
				_("The LMP ({0}) is more than {1} weeks before the booking date. Check the date.").format(
					_fmt_date(lmp), MAX_GESTATION_DAYS // 7
				),
				ImplausibleANCValueError,
				title=_("Check the dates"),
			)
	if basis == maternity.EDD_BASIS_LMP and lmp:
		doc.edd = maternity.edd_from_lmp(lmp)
	elif basis != maternity.EDD_BASIS_LMP and not doc.edd:
		frappe.throw(
			_("Enter the EDD given by the {0}, or set the EDD basis to LMP.").format(_(basis).lower()),
			ImplausibleANCValueError,
			title=_("Missing Value"),
		)
	if not doc.edd:
		return
	edd = getdate(doc.edd)
	if lmp and edd <= lmp:
		frappe.throw(
			_("The EDD must be after the LMP."), ImplausibleANCValueError, title=_("Check the dates")
		)
	days = maternity.gestation_days(None, edd, booking)
	if not 0 <= days <= MAX_GESTATION_DAYS:
		frappe.throw(
			_(
				"The EDD {0} gives a gestation of {1} weeks at booking; expected 0 to {2}. Check the dates."
			).format(_fmt_date(edd), days // 7, MAX_GESTATION_DAYS // 7),
			ImplausibleANCValueError,
			title=_("Check the dates"),
		)


def _check_one_open(doc) -> None:
	"""One open, non-archived pregnancy per mother, under a lock on the Patient row (then a locking read)."""
	frappe.db.sql("SELECT name FROM `tabPatient` WHERE name = %s FOR UPDATE", doc.patient)
	other = open_pregnancy(doc.patient, for_update=True)
	if other and other != doc.name:
		frappe.throw(
			_(
				"{0} already has an open pregnancy ({1}). Record the contacts on it, or close it first"
				" (Close Pregnancy)."
			).format(doc.patient, other),
			common.MCHRecordError,
			title=_("Open Pregnancy Exists"),
		)


def _close(doc, reason: str, on, delivery_record: str | None = None, note: str | None = None) -> None:
	"""Close `doc` (an open, non-archived, locked ANC Pregnancy) as `reason`. Server path only."""
	doc.status = PREGNANCY_CLOSED
	doc.closed_reason = reason
	doc.closed_on = getdate(on)
	doc.closed_by = frappe.session.user
	doc.delivery_record = delivery_record
	doc.closure_note = note
	doc.gestation = gestation_label(doc, doc.closed_on)
	doc.flags.krcs_close = True
	doc.flags.ignore_permissions = True
	try:
		doc.save()
	finally:
		doc.flags.krcs_close = False


def close_on_delivery(doc, method=None) -> None:
	"""Delivery Record on_submit (hooks.py doc_events): close the mother's open pregnancy as Delivered on the
	delivery date. Idempotent (no open pregnancy: nothing happens). A pregnancy booked after the delivery date
	is a later one and stays open. A Delivery Record that supersedes another (a correction) only re-points a
	pregnancy closed by the superseded record.

	Runs in the submit's transaction and never fails the delivery: any error is rolled back to a savepoint
	and written to the Error Log."""
	messages = len(getattr(frappe.local, "message_log", None) or [])
	frappe.db.savepoint(CLOSE_SAVEPOINT)
	try:
		_close_on_delivery(doc)
	except Exception:
		frappe.db.rollback(save_point=CLOSE_SAVEPOINT)
		if getattr(frappe.local, "message_log", None):
			del frappe.local.message_log[messages:]  # the delivery succeeded: no error dialog
		try:
			frappe.log_error(
				title=_("MCH: closing the ANC pregnancy on delivery failed"),
				reference_doctype=DELIVERY_RECORD_DOCTYPE,
				reference_name=doc.name,
			)
		except Exception:
			pass
	else:
		frappe.db.release_savepoint(CLOSE_SAVEPOINT)


def _close_on_delivery(doc) -> None:
	mother = doc.get("patient")
	if not mother and doc.get("admission"):
		mother = frappe.db.get_value("Maternity Admission", doc.admission, "patient")
	if not mother or not frappe.db.table_exists(ANC_PREGNANCY_DOCTYPE):
		return
	delivered = getdate(doc.delivered_at or today())
	superseded = doc.get("supersedes")
	if superseded:
		# A correction: the pregnancy was closed by the superseded record (or never booked). Re-point it and
		# never close another (a later) pregnancy.
		for name in frappe.db.sql(
			f"""SELECT name FROM `tab{ANC_PREGNANCY_DOCTYPE}`
			WHERE delivery_record = %s AND status = %s FOR UPDATE""",
			(superseded, PREGNANCY_CLOSED),
			pluck=True,
		):
			frappe.db.set_value(
				ANC_PREGNANCY_DOCTYPE,
				name,
				{"delivery_record": doc.name, "closed_on": delivered},
			)
		return
	name = open_pregnancy(mother, for_update=True)
	if not name:
		return
	preg = frappe.get_doc(ANC_PREGNANCY_DOCTYPE, name)
	if preg.booking_date and getdate(preg.booking_date) > delivered:
		return  # booked after this delivery: a later pregnancy
	_close(preg, PREGNANCY_DELIVERED, delivered, delivery_record=doc.name)


@frappe.whitelist(methods=["POST"])
def close_pregnancy(pregnancy: str, reason: str, note: str | None = None) -> dict:
	"""Close an open pregnancy as Pregnancy ended (miscarriage or abortion) or Transferred out (MCH_ROLES).
	A delivery closes it by itself (Delivery Record submit)."""
	require_roles(MCH_ROLES, _("close a pregnancy"))
	if reason not in USER_CLOSE_REASONS:
		frappe.throw(
			_(
				"Choose the reason: {0}. A delivery closes the pregnancy when its Delivery Record is submitted."
			).format(", ".join(_(r) for r in USER_CLOSE_REASONS)),
			common.MCHRecordError,
		)
	note = (note or "").strip() or None
	if note and len(note) > NOTE_MAX_LENGTH:
		frappe.throw(
			_("The note is limited to {0} characters.").format(NOTE_MAX_LENGTH), common.MCHRecordError
		)
	frappe.has_permission(ANC_PREGNANCY_DOCTYPE, "write", doc=pregnancy, throw=True)
	row = frappe.db.get_value(
		ANC_PREGNANCY_DOCTYPE, pregnancy, ["name", "status", "patient"], as_dict=True, for_update=True
	)
	if not row:
		frappe.throw(_("ANC Pregnancy {0} does not exist.").format(pregnancy), common.MCHRecordError)
	doc = frappe.get_doc(ANC_PREGNANCY_DOCTYPE, pregnancy)
	if cint(doc.get(ARCHIVED_FIELD)):
		frappe.throw(_("{0} is archived.").format(pregnancy), common.MCHRecordError, title=_("Archived"))
	if doc.status != PREGNANCY_OPEN:
		frappe.throw(_("{0} is already closed.").format(pregnancy), common.MCHRecordError, title=_("Closed"))
	_close(doc, reason, today(), note=note)
	return {"name": doc.name, "status": doc.status, "closed_reason": doc.closed_reason}


@frappe.whitelist()
def admit_prefill(patient: str) -> dict:
	"""LMP, EDD basis, EDD, gravida, para and blood group/rhesus of the open pregnancy, for the Maternity admit
	dialog ({} when none or the user cannot read it)."""
	name = open_pregnancy(patient)
	if not name or not frappe.has_permission(ANC_PREGNANCY_DOCTYPE, "read", doc=name):
		return {}
	values = frappe.db.get_value(
		ANC_PREGNANCY_DOCTYPE,
		name,
		["lmp", "edd_basis", "edd", "gravida", "para", "blood_group", "rhesus"],
		as_dict=True,
	)
	from onerc_healthcare.privacy import access_log

	access_log.log_access(
		access_log.ACTION_READ_API, ANC_PREGNANCY_DOCTYPE, name, patient=patient, detail="admit_prefill"
	)
	return {
		"lmp": values.lmp,
		"edd_basis": values.edd_basis,
		"edd": values.edd,
		"gravida": cint(values.gravida) or None,
		"para": values.para,
		"blood_group": values.blood_group,
		"rhesus": values.rhesus,
		"pregnancy": name,
	}


# --------------------------------------------------------------------------------------- ANC Contact


def contact_validate(doc) -> None:
	if common.guard_draft(doc):
		return
	common.check_visit(doc)
	common.guard_one_per_visit(doc)
	common.set_recorded_by(doc)
	if doc.is_new():
		require_roles(MCH_ROLES, _("record an ANC contact"))
	check_female(doc.patient, ANC_CONTACT_DOCTYPE)
	preg = _contact_pregnancy(doc)
	_not_future(doc, "contact_date")
	contact_date = getdate(doc.contact_date or today())
	if preg.booking_date and contact_date < getdate(preg.booking_date):
		frappe.throw(
			_("The contact date cannot be before the pregnancy was booked ({0}).").format(
				_fmt_date(preg.booking_date)
			),
			ImplausibleANCValueError,
			title=_("Check the dates"),
		)
	if preg.status == PREGNANCY_CLOSED and preg.closed_on and contact_date > getdate(preg.closed_on):
		frappe.throw(
			_("The contact date cannot be after the pregnancy was closed ({0}).").format(
				_fmt_date(preg.closed_on)
			),
			ImplausibleANCValueError,
			title=_("Check the dates"),
		)
	doc.gestation = gestation_label(preg, contact_date)
	check_ranges(doc, CONTACT_RANGES, ("bp_systolic", "bp_diastolic"))


def _pregnancy_row(name: str, for_update: bool = False):
	return frappe.db.get_value(
		ANC_PREGNANCY_DOCTYPE,
		name,
		["name", "patient", "status", "booking_date", "closed_on", "lmp", "edd", ARCHIVED_FIELD],
		as_dict=True,
		for_update=for_update,
	)


def _contact_pregnancy(doc):
	"""Insert: the mother's open pregnancy (locked) and the contact number. Later saves keep both."""
	if not doc.is_new():
		before = doc.get_doc_before_save()
		if before is not None:
			doc.pregnancy, doc.contact_number = before.pregnancy, before.contact_number
	if not doc.is_new() and doc.get("pregnancy"):
		return _pregnancy_row(doc.pregnancy)
	name = open_pregnancy(doc.patient, for_update=True)
	if not name:
		frappe.throw(
			_("{0} has no open pregnancy. Book the pregnancy first (ANC Pregnancy).").format(doc.patient),
			common.MCHRecordError,
			title=_("No Open Pregnancy"),
		)
	doc.pregnancy = name
	(count,) = frappe.db.sql(
		f"""SELECT COUNT(*) FROM `tab{ANC_CONTACT_DOCTYPE}`
		WHERE pregnancy = %(preg)s AND docstatus < 2 AND name != %(name)s AND {_contact_archived_sql()}
		FOR UPDATE""",
		{"preg": name, "name": doc.name or ""},
	)[0]
	doc.contact_number = cint(count) + 1
	return _pregnancy_row(name)


def _contact_archived_sql() -> str:
	if frappe.db.has_column(ANC_CONTACT_DOCTYPE, ARCHIVED_FIELD):
		return f"IFNULL(`{ARCHIVED_FIELD}`, 0) = 0"
	return "1 = 1"


def contact_before_submit(doc) -> None:
	common.before_submit(doc)
	preg = _pregnancy_row(doc.pregnancy, for_update=True) if doc.get("pregnancy") else None
	if not preg or preg.status != PREGNANCY_OPEN or cint(preg.get(ARCHIVED_FIELD)):
		frappe.throw(
			_(
				"Pregnancy {0} is no longer open (closed or archived); this contact cannot be submitted."
			).format(doc.get("pregnancy") or ""),
			common.MCHRecordError,
			title=_("Pregnancy Closed"),
		)


def contact_before_cancel(doc) -> None:
	common.refuse_cancel(doc)


def contact_before_update_after_submit(doc) -> None:
	common.guard_after_submit(doc)
