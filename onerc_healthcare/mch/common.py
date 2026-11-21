# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Shared rules of the per-visit MCH records (W0, integrator-owned; MCH-A and MCH-B call these and never
edit them). See docs/MCH_CONTRACT.md §4.

Every record in `MCH_RECORD_DOCTYPES` (ANC Contact, PNC Visit, Immunisation, Growth Monitoring):
- is made during an **open visit of the same patient** (`check_visit`, on insert only; a draft may be
  submitted after the visit closes). PNC Visit may instead name the mother's open Maternity Admission;
- is **one per visit** (`guard_one_per_visit`), checked under a lock on the visit (lock order D41: Visit first);
- gets `facility` from the visit, `encounter` from the visit's consultation (server-set, never typed: a
  Triage Nurse cannot read encounters, so the browser must not validate that link), `recorded_by` = the
  user who inserted it;
- is **submittable with no cancel or amend** (NFR-9); after submit only the addendum grows (`add_addendum`),
  and only the Medical In-Charge archives (`archive`, `krcs_archived`).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, date_diff, format_datetime, getdate, now_datetime

from onerc_healthcare.clinical.utils import has_any_role, require_roles
from onerc_healthcare.constants import (
	ANC_PREGNANCY_DOCTYPE,
	CHILD_MCH_ROLES,
	GROWTH_MONITORING_DOCTYPE,
	IMMUNISATION_DOCTYPE,
	MCH_RECORD_DOCTYPES,
	MCH_ROLES,
	ROLE_MIC,
	VISIT_DOCTYPE,
	VISIT_OPEN_STATUSES,
)

ADDENDUM_FIELD = "addendum"
ARCHIVED_FIELD = "krcs_archived"
ADDENDUM_MAX_LENGTH = 5000
REASON_MAX_LENGTH = 1000
ARCHIVE_ROLES = (ROLE_MIC,)


class MCHRecordError(frappe.ValidationError):
	pass


# --------------------------------------------------------------------------------------- visit


def check_visit(doc, required: bool = True) -> dict | None:
	"""Load `doc.visit` (locked) and check it belongs to `doc.patient` and, for a new record, is open.

	Sets `doc.facility` from the visit and `doc.encounter` from the visit's consultation (when the record has
	those fields and they are empty). Returns the visit row ({name, patient, facility, status, encounter,
	current_department}) or None when there is no visit and `required` is False."""
	if not doc.get("visit"):
		if required:
			frappe.throw(_("Choose the patient's visit."), MCHRecordError, title=_("Missing Value"))
		return None
	visit = frappe.db.get_value(
		VISIT_DOCTYPE,
		doc.visit,
		["name", "patient", "facility", "status", "encounter", "current_department"],
		as_dict=True,
		for_update=True,
	)
	if not visit:
		frappe.throw(_("Patient Visit {0} does not exist.").format(doc.visit), MCHRecordError)
	if doc.get("patient") and visit.patient != doc.patient:
		frappe.throw(
			_("Visit {0} belongs to another patient.").format(visit.name),
			MCHRecordError,
			title=_("Wrong Visit"),
		)
	if doc.is_new() and visit.status not in VISIT_OPEN_STATUSES:
		frappe.throw(
			_("Visit {0} is {1}. MCH records are made during an open visit.").format(
				visit.name, _(visit.status)
			),
			MCHRecordError,
			title=_("Visit Closed"),
		)
	if doc.meta.has_field("facility") and not doc.get("facility"):
		doc.facility = visit.facility
	if doc.meta.has_field("encounter") and not doc.get("encounter") and visit.encounter:
		doc.encounter = visit.encounter
	return visit


def guard_one_per_visit(doc, keys: tuple[str, ...] = ("visit",)) -> None:
	"""Refuse a second record of `doc.doctype` with the same `keys` values (drafts and submitted records count;
	archived ones do not). Call after `check_visit` so the visit row is locked."""
	filters = {k: doc.get(k) for k in keys}
	if any(v in (None, "") for v in filters.values()):
		return
	filters["docstatus"] = ("<", 2)
	filters["name"] = ("!=", doc.name or "")
	if frappe.get_meta(doc.doctype).has_field(ARCHIVED_FIELD):
		filters[ARCHIVED_FIELD] = 0
	other = frappe.get_all(doc.doctype, filters=filters, pluck="name", limit=1)
	if other:
		frappe.throw(
			_("{0} {1} is already recorded for this visit. Open it instead.").format(
				_(doc.doctype), other[0]
			),
			MCHRecordError,
			title=_("Already Recorded"),
		)


def set_recorded_by(doc) -> None:
	if doc.is_new() or not doc.get("recorded_by"):
		doc.recorded_by = frappe.session.user


def age_in_days(dob, on) -> int | None:
	if not dob or not on:
		return None
	return date_diff(getdate(on), getdate(dob))


def age_label(days: int | None) -> str | None:
	"""'10 days', '7 weeks', '14 months', '3 years 2 months'."""
	if days is None or days < 0:
		return None
	if days < 28:
		return _("{0} days").format(days)
	if days < 91:
		return _("{0} weeks").format(days // 7)
	months = int(days / 30.4375)
	if months < 24:
		return _("{0} months").format(months)
	return _("{0} years {1} months").format(months // 12, months % 12)


# --------------------------------------------------------------------------------------- lifecycle


def guard_draft(doc) -> bool:
	"""Common `validate` rules for a draft. Returns True when the save is an archive-only change (nothing else
	to check). The addendum is for submitted records only; `krcs_archived` changes only through `archive`."""
	if doc.get(ADDENDUM_FIELD) and doc.docstatus == 0 and not doc.flags.krcs_addendum:
		frappe.throw(
			_("The addendum is for corrections after submit. Use Notes while the record is a draft."),
			MCHRecordError,
		)
	if doc.is_new():
		if cint(doc.get(ARCHIVED_FIELD)):
			frappe.throw(_("A new record cannot be archived."), frappe.PermissionError)
		return False
	before = doc.get_doc_before_save()
	if before is None:
		return False
	if cint(doc.get(ARCHIVED_FIELD)) != cint(before.get(ARCHIVED_FIELD)):
		if not doc.flags.krcs_archive:
			frappe.throw(
				_("Only the Medical In-Charge archives an MCH record (Archive)."),
				frappe.PermissionError,
				title=_("Not Allowed"),
			)
		return True
	if cint(doc.get(ARCHIVED_FIELD)):
		frappe.throw(_("{0} is archived and cannot be edited.").format(doc.name), MCHRecordError)
	return False


def before_submit(doc) -> None:
	if cint(doc.get(ARCHIVED_FIELD)):
		frappe.throw(
			_("{0} is archived and cannot be submitted.").format(doc.name),
			MCHRecordError,
			title=_("Archived"),
		)


def refuse_cancel(doc) -> None:
	frappe.throw(
		_("A submitted {0} cannot be cancelled. Use Add Addendum to record a correction.").format(
			_(doc.doctype)
		),
		frappe.PermissionError,
		title=_("Not Allowed"),
	)


def guard_after_submit(doc) -> None:
	"""After submit only the addendum grows (through `add_addendum`) and `krcs_archived` is set (through
	`archive`). Anything else is refused by Frappe (fields are not allow_on_submit)."""
	before = doc.get_doc_before_save()
	if cint(doc.get(ARCHIVED_FIELD)) != cint(before.get(ARCHIVED_FIELD) if before else 0):
		if not doc.flags.krcs_archive:
			frappe.throw(
				_("Only the Medical In-Charge archives an MCH record (Archive)."),
				frappe.PermissionError,
				title=_("Not Allowed"),
			)
	old = (before.get(ADDENDUM_FIELD) if before else None) or ""
	new = doc.get(ADDENDUM_FIELD) or ""
	if old != new and (not doc.flags.krcs_addendum or not new.startswith(old)):
		frappe.throw(
			_("The addendum is append-only. Use Add Addendum."), MCHRecordError, title=_("Not Allowed")
		)


def _check_doctype(doctype: str, archivable: bool = False) -> None:
	allowed = (*MCH_RECORD_DOCTYPES, ANC_PREGNANCY_DOCTYPE) if archivable else MCH_RECORD_DOCTYPES
	if doctype not in allowed:
		frappe.throw(_("{0} is not an MCH record.").format(doctype), MCHRecordError)


def _append(doc, text: str) -> None:
	entry = f"[{format_datetime(now_datetime())}, {frappe.utils.get_fullname(frappe.session.user)}]\n{text}"
	current = doc.get(ADDENDUM_FIELD) or ""
	doc.set(ADDENDUM_FIELD, f"{current}\n\n{entry}" if current else entry)


@frappe.whitelist(methods=["POST"])
def add_addendum(doctype: str, name: str, text: str) -> dict:
	"""Append a dated, signed correction to a submitted MCH record (no cancel or amend, NFR-9)."""
	_check_doctype(doctype)
	roles = CHILD_MCH_ROLES if doctype in (IMMUNISATION_DOCTYPE, GROWTH_MONITORING_DOCTYPE) else MCH_ROLES
	require_roles(roles, _("add an addendum"))
	frappe.has_permission(doctype, "write", doc=name, throw=True)
	text = (text or "").strip()
	if not text:
		frappe.throw(_("Enter the addendum text."), MCHRecordError)
	if len(text) > ADDENDUM_MAX_LENGTH:
		frappe.throw(
			_("The addendum is limited to {0} characters.").format(ADDENDUM_MAX_LENGTH), MCHRecordError
		)
	frappe.db.get_value(doctype, name, "name", for_update=True)
	doc = frappe.get_doc(doctype, name)
	if doc.docstatus != 1:
		frappe.throw(_("An addendum is added to a submitted record. Edit the draft instead."), MCHRecordError)
	_append(doc, text)
	doc.flags.krcs_addendum = True
	# update-after-submit asks for the submit right, which a writer may lack; the role gate and the write
	# permission above are the checks.
	doc.flags.ignore_permissions = True
	try:
		doc.save()
	finally:
		doc.flags.krcs_addendum = False
	return {"name": doc.name, ADDENDUM_FIELD: doc.get(ADDENDUM_FIELD)}


@frappe.whitelist(methods=["POST"])
def archive(doctype: str, name: str, reason: str) -> dict:
	"""Medical In-Charge: archive an MCH record made in error (wrong patient or visit). Never deleted; the reason
	is appended to the addendum of a submitted record and kept in the Version history and comments otherwise.
	Also archives an ANC Pregnancy (MCH-A frees nothing: a new pregnancy can then be booked)."""
	_check_doctype(doctype, archivable=True)
	if not has_any_role(ARCHIVE_ROLES):
		frappe.throw(_("Only the Medical In-Charge can archive MCH records."), frappe.PermissionError)
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Enter the reason for archiving."), MCHRecordError)
	if len(reason) > REASON_MAX_LENGTH:
		frappe.throw(_("The reason is limited to {0} characters.").format(REASON_MAX_LENGTH), MCHRecordError)
	frappe.db.get_value(doctype, name, "name", for_update=True)
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")
	if cint(doc.get(ARCHIVED_FIELD)):
		frappe.throw(_("{0} is already archived.").format(name), MCHRecordError)
	doc.set(ARCHIVED_FIELD, 1)
	if doc.docstatus == 1:
		_append(doc, _("Archived: {0}").format(reason))
		doc.flags.krcs_addendum = True
	doc.add_comment("Info", _("Archived: {0}").format(reason))
	doc.flags.krcs_archive = True
	doc.flags.ignore_permissions = True
	try:
		doc.save()
	finally:
		doc.flags.krcs_archive = False
		doc.flags.krcs_addendum = False
	return {"name": doc.name, ARCHIVED_FIELD: 1}
