# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MCH form entry points (integrator, step 3): which MCH records the user can open or start from a visit or a
consultation, and the MCH summary on the Patient form. Read-only; the record controllers do every check."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, getdate, today

from onerc_healthcare.constants import (
	ANC_CONTACT_DOCTYPE,
	ANC_PREGNANCY_DOCTYPE,
	DEPT_MCH,
	ENCOUNTER_DOCTYPE,
	ENCOUNTER_VISIT_FIELD,
	GROWTH_MAX_AGE_DAYS,
	GROWTH_MONITORING_DOCTYPE,
	IMMUNISATION_DOCTYPE,
	PATIENT_MOTHER_FIELD,
	PNC_SUBJECT_BABY,
	PNC_SUBJECT_MOTHER,
	PNC_VISIT_DOCTYPE,
	PREGNANCY_OPEN,
	VISIT_DOCTYPE,
	VISIT_OPEN_STATUSES,
)

ARCHIVED_FIELD = "krcs_archived"
MIN_MOTHER_AGE_DAYS = 10 * 365  # ANC is offered to women from 10 years
PNC_BABY_MAX_AGE_DAYS = 365


def _patient(name: str) -> dict:
	row = frappe.db.get_value(
		"Patient",
		name,
		["name", "patient_name", "sex", "dob", "krcs_dob_estimated", PATIENT_MOTHER_FIELD],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("Patient {0} does not exist.").format(name))
	row.age_days = (getdate(today()) - getdate(row.dob)).days if row.dob else None
	return row


def _existing(doctype: str, visit: str, patient: str) -> str | None:
	filters = {"visit": visit, "patient": patient, "docstatus": ("<", 2)}
	if frappe.get_meta(doctype).has_field(ARCHIVED_FIELD):
		filters[ARCHIVED_FIELD] = 0
	names = frappe.get_all(doctype, filters=filters, pluck="name", limit=1, ignore_permissions=True)
	return names[0] if names else None


def open_pregnancy(patient: str) -> str | None:
	names = frappe.get_all(
		ANC_PREGNANCY_DOCTYPE,
		filters={"patient": patient, "status": PREGNANCY_OPEN, ARCHIVED_FIELD: 0},
		pluck="name",
		limit=1,
		ignore_permissions=True,
	)
	return names[0] if names else None


@frappe.whitelist()
def visit_actions(visit: str) -> dict:
	"""MCH buttons for an open visit: [{doctype, label, existing, action, defaults}] where action is "open" (a
	record of this visit exists and the user can read it), "new" (the user can create one; `defaults` prefill the
	new form) or "book" (ANC: book the pregnancy first; `defaults` are for a new ANC Pregnancy). Only records that fit the patient's sex and age are offered."""
	frappe.has_permission(VISIT_DOCTYPE, "read", doc=visit, throw=True)
	v = frappe.db.get_value(VISIT_DOCTYPE, visit, ["name", "patient", "status", "facility"], as_dict=True)
	out = {"visit": visit, "patient": v.patient, "facility": v.facility, "actions": []}
	if v.status not in VISIT_OPEN_STATUSES:
		return out
	p = _patient(v.patient)
	age = p.age_days
	mother = p.sex == "Female" and age is not None and age >= MIN_MOTHER_AGE_DAYS
	baby = age is not None and age < PNC_BABY_MAX_AGE_DAYS
	child = age is not None and age < GROWTH_MAX_AGE_DAYS
	wanted = []
	if mother:
		wanted.append((ANC_CONTACT_DOCTYPE, _("Antenatal Contact")))
	if mother or baby:
		wanted.append((PNC_VISIT_DOCTYPE, _("Postnatal Check")))
	if child:
		wanted += [
			(IMMUNISATION_DOCTYPE, _("Immunisation")),
			(GROWTH_MONITORING_DOCTYPE, _("Growth Monitoring")),
		]
	for doctype, label in wanted:
		existing = _existing(doctype, visit, v.patient)
		if existing:
			if frappe.has_permission(doctype, "read", doc=existing):
				out["actions"].append(
					{"doctype": doctype, "label": label, "existing": existing, "action": "open"}
				)
			continue
		if not frappe.has_permission(doctype, "create"):
			continue
		action, defaults = "new", {"patient": v.patient, "visit": visit}
		if doctype == PNC_VISIT_DOCTYPE:
			defaults["subject"] = PNC_SUBJECT_BABY if baby and not mother else PNC_SUBJECT_MOTHER
		if doctype == ANC_CONTACT_DOCTYPE and not open_pregnancy(v.patient):
			if not frappe.has_permission(ANC_PREGNANCY_DOCTYPE, "create"):
				continue
			action = "book"
			defaults = {"patient": v.patient, "facility": v.facility, "booking_visit": visit}
		out["actions"].append(
			{"doctype": doctype, "label": label, "existing": None, "action": action, "defaults": defaults}
		)
	return out


@frappe.whitelist()
def encounter_actions(encounter: str) -> dict:
	"""`visit_actions` for the visit of a draft MCH consultation (empty for other departments)."""
	frappe.has_permission(ENCOUNTER_DOCTYPE, "read", doc=encounter, throw=True)
	enc = frappe.db.get_value(
		ENCOUNTER_DOCTYPE, encounter, [ENCOUNTER_VISIT_FIELD, "krcs_department", "docstatus"], as_dict=True
	)
	if not enc or enc.docstatus != 0 or enc.krcs_department != DEPT_MCH or not enc.get(ENCOUNTER_VISIT_FIELD):
		return {"actions": []}
	return visit_actions(enc.get(ENCOUNTER_VISIT_FIELD))


@frappe.whitelist()
def patient_summary(patient: str) -> dict:
	"""MCH panel on the Patient form: the open pregnancy, whether the child panels apply, and record counts the
	user may read."""
	frappe.has_permission("Patient", "read", doc=patient, throw=True)
	p = _patient(patient)
	out = {
		"sex": p.sex,
		"age_days": p.age_days,
		"dob_estimated": cint(p.krcs_dob_estimated),
		"mother": p.get(PATIENT_MOTHER_FIELD),
		"is_child": p.age_days is not None and p.age_days < GROWTH_MAX_AGE_DAYS,
		"pregnancy": None,
		"counts": {},
	}
	if p.sex == "Female" and frappe.has_permission(ANC_PREGNANCY_DOCTYPE, "read"):
		name = open_pregnancy(patient)
		if name:
			out["pregnancy"] = frappe.db.get_value(
				ANC_PREGNANCY_DOCTYPE, name, ["name", "edd", "lmp", "booking_date"], as_dict=True
			)
	for doctype in (ANC_CONTACT_DOCTYPE, PNC_VISIT_DOCTYPE, IMMUNISATION_DOCTYPE, GROWTH_MONITORING_DOCTYPE):
		if frappe.has_permission(doctype, "read"):
			n = frappe.db.count(doctype, {"patient": patient, "docstatus": 1, ARCHIVED_FIELD: 0})
			if n:
				out["counts"][doctype] = n
	if out["pregnancy"]:  # DEFECT-MCH-7: the pregnancy's dates are patient data read through the API (D44)
		from onerc_healthcare.privacy import access_log

		access_log.log_access(
			access_log.ACTION_READ_API,
			ANC_PREGNANCY_DOCTYPE,
			out["pregnancy"].name,
			patient=patient,
			detail="patient_summary",
		)
	return out
