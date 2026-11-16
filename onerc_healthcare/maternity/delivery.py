# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Delivery Record, the Summary of Labour (M3; SDD §5.12.3, INP-16, OI-23, D60).

A Delivery Record is submittable and never cancelled or amended: a submitted record is a clinical record
(NFR-9). Corrections are appended to ``addendum`` with `add_addendum`, as for a completed consultation.

| Event | Rule |
|---|---|
| save (draft) | the admission is Admitted or In Labour and not archived; ``delivered_at`` is not in the future and not before ``admitted_at`` (except born before arrival); a mode of delivery; at least one baby; each baby has an outcome, a sex when live born, Apgar 0-10 and a weight of 0.3-6.5 kg when given; no other submitted record for the admission |
| submit | Midwife or Clinical Officer (`DELIVERY_SUBMIT_ROLES`); the admission is locked and checked again; then, in one transaction, `maternity.admission.record_delivery` (M1, admission → Delivered) and `register_newborns` |
| cancel | refused |
| update after submit | only ``addendum``, only through `add_addendum`, append-only |

Newborns (`register_newborns`): each live-born baby becomes a walk-in Patient ("Baby" + the mother's last
name, exact date of birth = the delivery date, sex, a facility-generated ID at the mother's registration
facility, population group and residence copied, ``krcs_mother`` and a Patient Relation "Mother"). Twins
share name, date of birth and often sex, so the duplicate check gets `NEWBORN_OVERRIDE_REASON`. A row that
already has its ``newborn_patient`` is skipped, so a second call creates nothing. Stillbirths get no Patient.
"""

from __future__ import annotations

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, flt, format_datetime, get_datetime, getdate, now_datetime

from onerc_healthcare.clinical.utils import require_roles
from onerc_healthcare.constants import (
	ADMISSION_ADMITTED,
	ADMISSION_IN_LABOUR,
	BABY_LIVE_BIRTH,
	BABY_OUTCOMES,
	DELIVERY_BABY_DOCTYPE,
	DELIVERY_MODES,
	DELIVERY_RECORD_DOCTYPE,
	MATERNITY_ADMISSION_DOCTYPE,
	MATERNITY_ROLES,
	NEWBORN_OVERRIDE_REASON,
	PATIENT_MOTHER_FIELD,
	PATIENT_WALK_IN_FIELD,
	ROLE_CLINICIAN,
	ROLE_MIDWIFE,
)
from onerc_healthcare.registration.identifiers import (
	OVERRIDE_REASON_FIELD,
	POPULATION_GROUP_FIELD,
	REGISTRATION_FACILITY_FIELD,
	RESIDENCE_FIELD,
)
from onerc_healthcare.visit.events import FUTURE_SKEW_MINUTES

# Who submits a Delivery Record (DocPerm submit); the Medical In-Charge reads and edits drafts only.
DELIVERY_SUBMIT_ROLES = (ROLE_MIDWIFE, ROLE_CLINICIAN)
# Admission statuses in which a delivery can be recorded
DELIVERY_ADMISSION_STATUSES = (ADMISSION_ADMITTED, ADMISSION_IN_LABOUR)

APGAR_MIN, APGAR_MAX = 0, 10
WEIGHT_MIN_KG, WEIGHT_MAX_KG = 0.3, 6.5
NEWBORN_FIRST_NAME = "Baby"
RELATION_MOTHER = "Mother"  # Marley's Patient Relation option
ADDENDUM_FIELD = "addendum"
ADDENDUM_MAX_LENGTH = 5000
ARCHIVED_FIELD = "krcs_archived"


class DeliveryRecordError(frappe.ValidationError):
	pass


# --------------------------------------------------------------------------------------- controller


def validate(doc) -> None:
	"""Delivery Record `validate`. After submit only the addendum may change (`before_update_after_submit`)."""
	if doc.docstatus == 2 or (doc.docstatus == 1 and doc._action != "submit"):
		return
	adm = _admission(doc.admission)
	doc.patient, doc.patient_name, doc.facility = adm.patient, adm.patient_name, adm.facility
	_check_admission(adm)
	_check_delivered_at(doc, adm)
	if doc.delivery_mode not in DELIVERY_MODES:
		frappe.throw(
			_("Choose the mode of delivery ({0}).").format(", ".join(_(m) for m in DELIVERY_MODES)),
			DeliveryRecordError,
			title=_("Missing Value"),
		)
	if not doc.delivered_by:
		doc.delivered_by = frappe.session.user
	_check_babies(doc)
	_check_no_other_delivery(doc)
	if doc.get(ADDENDUM_FIELD) and doc.docstatus == 0 and not doc.flags.krcs_addendum:
		frappe.throw(
			_("The addendum is for corrections after submit. Use Remarks while the record is a draft."),
			DeliveryRecordError,
		)


def before_submit(doc) -> None:
	require_roles(DELIVERY_SUBMIT_ROLES, _("submit a Delivery Record"))
	# Lock order (contract §5): Maternity Admission first. A locking read sees the latest committed status,
	# so a second submit waiting on the lock finds the admission Delivered and is refused.
	adm = _admission(doc.admission, for_update=True)
	_check_admission(adm)
	_check_no_other_delivery(doc, for_update=True)


def on_submit(doc) -> None:
	"""Contract §6.3, in this order and in the submit's transaction."""
	from onerc_healthcare.maternity.admission import record_delivery  # M1 (contract §7)

	record_delivery(doc.admission, doc.name)
	register_newborns(doc)


def before_cancel(doc) -> None:
	frappe.throw(
		_("A Delivery Record cannot be cancelled. Use Add Addendum to record a correction."),
		frappe.PermissionError,
		title=_("Not Allowed"),
	)


def before_update_after_submit(doc) -> None:
	"""The addendum only grows, and only through `add_addendum`."""
	before = doc.get_doc_before_save()
	old = (before.get(ADDENDUM_FIELD) if before else None) or ""
	new = doc.get(ADDENDUM_FIELD) or ""
	if old == new:
		return
	if not doc.flags.krcs_addendum or not new.startswith(old):
		frappe.throw(
			_("The addendum is append-only. Use Add Addendum."), DeliveryRecordError, title=_("Not Allowed")
		)


# --------------------------------------------------------------------------------------- validation


def _admission(name: str | None, for_update: bool = False):
	if not name:
		frappe.throw(_("Choose the Maternity Admission."), DeliveryRecordError, title=_("Missing Value"))
	adm = frappe.db.get_value(
		MATERNITY_ADMISSION_DOCTYPE,
		name,
		["name", "patient", "patient_name", "facility", "status", "admitted_at", ARCHIVED_FIELD],
		as_dict=True,
		for_update=for_update,
	)
	if not adm:
		frappe.throw(_("Maternity Admission {0} does not exist.").format(name), DeliveryRecordError)
	return adm


def _check_admission(adm) -> None:
	if cint(adm.get(ARCHIVED_FIELD)):
		frappe.throw(
			_("Maternity Admission {0} is archived; a delivery cannot be recorded on it.").format(adm.name),
			DeliveryRecordError,
		)
	if adm.status not in DELIVERY_ADMISSION_STATUSES:
		frappe.throw(
			_("A delivery is recorded while the admission is Admitted or In Labour. {0} is {1}.").format(
				adm.name, _(adm.status)
			),
			DeliveryRecordError,
			title=_("Not Allowed"),
		)


def _check_delivered_at(doc, adm) -> None:
	if not doc.delivered_at:
		frappe.throw(_("Enter the date and time of delivery."), DeliveryRecordError, title=_("Missing Value"))
	delivered = get_datetime(doc.delivered_at)
	if delivered > now_datetime() + timedelta(minutes=FUTURE_SKEW_MINUTES):
		frappe.throw(
			_("The delivery time ({0}) is in the future.").format(format_datetime(delivered)),
			DeliveryRecordError,
			title=_("Check the date"),
		)
	# A baby born before arrival was delivered before the mother was admitted.
	if adm.admitted_at and not cint(doc.born_before_arrival) and delivered < get_datetime(adm.admitted_at):
		frappe.throw(
			_(
				"The delivery time ({0}) is before the admission ({1}). Tick Born Before Arrival if the baby "
				"was born before the mother arrived."
			).format(format_datetime(delivered), format_datetime(adm.admitted_at)),
			DeliveryRecordError,
			title=_("Check the date"),
		)


def _check_babies(doc) -> None:
	rows = doc.get("babies") or []
	if not rows:
		frappe.throw(
			_("Add one row per baby in the Babies table."), DeliveryRecordError, title=_("Missing Value")
		)
	for row in rows:
		label = _("Baby {0}").format(row.idx)
		if row.outcome not in BABY_OUTCOMES:
			frappe.throw(
				_("{0}: choose the outcome ({1}).").format(label, ", ".join(_(o) for o in BABY_OUTCOMES)),
				DeliveryRecordError,
				title=_("Missing Value"),
			)
		if row.outcome == BABY_LIVE_BIRTH and not row.sex:
			frappe.throw(
				_("{0}: enter the sex of a live-born baby (it is registered as a patient).").format(label),
				DeliveryRecordError,
				title=_("Missing Value"),
			)
		for field in ("apgar_1", "apgar_5"):
			value = row.get(field)
			if value not in (None, "") and not APGAR_MIN <= cint(value) <= APGAR_MAX:
				frappe.throw(
					_("{0}: {1} must be between {2} and {3}.").format(
						label, _(row.meta.get_label(field)), APGAR_MIN, APGAR_MAX
					),
					DeliveryRecordError,
					title=_("Check the value"),
				)
		weight = flt(row.weight_kg)
		if weight and not WEIGHT_MIN_KG <= weight <= WEIGHT_MAX_KG:
			frappe.throw(
				_("{0}: the weight must be between {1} and {2} kg.").format(
					label, WEIGHT_MIN_KG, WEIGHT_MAX_KG
				),
				DeliveryRecordError,
				title=_("Check the value"),
			)
		if doc.docstatus == 0 or doc._action == "submit":
			row.newborn_patient = None  # set by the server at submit only


def _check_no_other_delivery(doc, for_update: bool = False) -> None:
	lock = " FOR UPDATE" if for_update else ""
	other = frappe.db.sql(
		f"SELECT name FROM `tab{DELIVERY_RECORD_DOCTYPE}`"
		f" WHERE admission = %s AND docstatus = 1 AND name != %s LIMIT 1{lock}",
		(doc.admission, doc.name or ""),
	)
	if other:
		frappe.throw(
			_("Maternity Admission {0} already has a submitted Delivery Record ({1}).").format(
				doc.admission, other[0][0]
			),
			DeliveryRecordError,
			title=_("Already Recorded"),
		)


# --------------------------------------------------------------------------------------- submit steps


def register_newborns(doc) -> list[str]:
	"""Register each live-born baby of submitted Delivery Record `doc` as a Patient (contract §6.3).

	Returns the newborn Patient names in baby-row order (live births only). Idempotent: a row whose
	``newborn_patient`` is already set (in memory or in the database) is skipped.
	"""
	require_roles(DELIVERY_SUBMIT_ROLES, _("register newborns"))
	if doc.docstatus != 1:
		frappe.throw(_("Newborns are registered when the Delivery Record is submitted."), DeliveryRecordError)
	mother = frappe.db.get_value(
		"Patient",
		doc.patient,
		[
			"name",
			"first_name",
			"last_name",
			REGISTRATION_FACILITY_FIELD,
			POPULATION_GROUP_FIELD,
			RESIDENCE_FIELD,
		],
		as_dict=True,
	)
	if not mother:
		frappe.throw(_("The mother {0} does not exist.").format(doc.patient), DeliveryRecordError)
	facility = _registration_facility(mother, doc.facility)
	names: list[str] = []
	for row in doc.get("babies") or []:
		if row.outcome != BABY_LIVE_BIRTH:
			continue
		existing = row.newborn_patient or frappe.db.get_value(
			DELIVERY_BABY_DOCTYPE, row.name, "newborn_patient"
		)
		if existing:
			row.newborn_patient = existing
			names.append(existing)
			continue
		patient = build_newborn(doc, row, mother, facility)
		# DocPerm: Patient create is the Receptionist's and the Midwife's; a Clinical Officer has none. The
		# role gate above (DELIVERY_SUBMIT_ROLES) and the Delivery Record submit right are the checks.
		patient.insert(ignore_permissions=True)
		row.db_set("newborn_patient", patient.name, update_modified=False)
		names.append(patient.name)
	return names


def build_newborn(doc, row, mother, facility: str | None):
	"""A new (unsaved) Patient for live-born baby `row` (pattern of `outreach.apply.build_patient`)."""
	patient = frappe.new_doc("Patient")
	patient.first_name = NEWBORN_FIRST_NAME
	patient.last_name = mother.last_name or mother.first_name
	patient.sex = row.sex
	patient.dob = getdate(doc.delivered_at)
	patient.set(PATIENT_WALK_IN_FIELD, 1)
	if facility:
		patient.set(REGISTRATION_FACILITY_FIELD, facility)
	patient.set(POPULATION_GROUP_FIELD, mother.get(POPULATION_GROUP_FIELD))
	patient.set(RESIDENCE_FIELD, mother.get(RESIDENCE_FIELD))
	patient.set(PATIENT_MOTHER_FIELD, mother.name)
	patient.append("patient_relation", {"patient": mother.name, "relation": RELATION_MOTHER})
	patient.set(OVERRIDE_REASON_FIELD, NEWBORN_OVERRIDE_REASON)
	return patient


def _registration_facility(mother, admission_facility: str | None) -> str | None:
	"""The mother's registration facility, else the admission's facility, else None (the Patient default)."""
	from onerc_healthcare.registration.walk_in import is_registration_facility

	for facility in (mother.get(REGISTRATION_FACILITY_FIELD), admission_facility):
		if is_registration_facility(facility):
			return facility
	return None


# --------------------------------------------------------------------------------------- endpoints


@frappe.whitelist(methods=["POST"])
def add_addendum(delivery: str, text: str) -> dict:
	"""Append a dated, signed correction to a submitted Delivery Record (no cancel or amend, NFR-9)."""
	require_roles(MATERNITY_ROLES, _("add an addendum to a Delivery Record"))
	frappe.has_permission(DELIVERY_RECORD_DOCTYPE, "write", doc=delivery, throw=True)
	text = (text or "").strip()
	if not text:
		frappe.throw(_("Enter the addendum text."), DeliveryRecordError)
	if len(text) > ADDENDUM_MAX_LENGTH:
		frappe.throw(
			_("The addendum is limited to {0} characters.").format(ADDENDUM_MAX_LENGTH), DeliveryRecordError
		)
	frappe.db.get_value(DELIVERY_RECORD_DOCTYPE, delivery, "name", for_update=True)
	doc = frappe.get_doc(DELIVERY_RECORD_DOCTYPE, delivery)
	if doc.docstatus != 1:
		frappe.throw(
			_("An addendum is added to a submitted Delivery Record. Edit the draft directly instead."),
			DeliveryRecordError,
		)
	entry = f"[{format_datetime(now_datetime())}, {frappe.utils.get_fullname(frappe.session.user)}]\n{text}"
	current = doc.get(ADDENDUM_FIELD) or ""
	doc.set(ADDENDUM_FIELD, f"{current}\n\n{entry}" if current else entry)
	doc.flags.krcs_addendum = True
	# The Medical In-Charge writes but has no submit right, which update-after-submit asks for; the role
	# gate and the write permission above are the checks.
	doc.flags.ignore_permissions = True
	try:
		doc.save()
	finally:
		doc.flags.krcs_addendum = False
	return {"name": doc.name, ADDENDUM_FIELD: doc.get(ADDENDUM_FIELD)}
