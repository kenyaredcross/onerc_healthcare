# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient Encounter and Healthcare Practitioner doc_events (wired in hooks.py). Owned by B1.

Encounter guards (FR-10, FR-11, SDD §5.3):

- An encounter belongs to exactly one open Patient Visit of the same patient (`krcs_visit`), and a visit
  has at most one active (draft or submitted) encounter. A new encounter needs a visit whose
  consultation has started (In Consultation, or Awaiting Laboratory / Pharmacy).
- No draft saves once the visit is closed.
- Submitting goes only through `clinical.consultation` (`flags.krcs_via_consultation`), so the desk
  Submit button cannot bypass the diagnosis, lab and visit rules.
- No cancel (Phase 2 decision): Marley's cancel orphans the Medication Requests and its amend duplicates
  every order. Corrections are made with the addendum.
- Procedure, therapy and Marley's free-text diagnosis tables are not used by the EMR (hidden; C-5).
"""

from __future__ import annotations

import frappe
from frappe import _

from onerc_healthcare.clinical import diagnosis
from onerc_healthcare.clinical.practitioner import get_or_create, strip_user_permission
from onerc_healthcare.clinical.utils import CONSULTING_ROLES, active_encounter_for_visit, has_any_role
from onerc_healthcare.constants import (
	DEFAULT_APPOINTMENT_TYPE,
	ENCOUNTER_VISIT_FIELD,
	VISIT_CLOSED_STATUSES,
	VISIT_DOCTYPE,
	VISIT_STATUS_AWAITING_LAB,
	VISIT_STATUS_AWAITING_PHARMACY,
	VISIT_STATUS_IN_CONSULTATION,
)

ADDENDUM_FIELD = "krcs_addendum"
# A new encounter needs a visit whose consultation has started (normally In Consultation).
CONSULTING_STATUSES = (
	VISIT_STATUS_IN_CONSULTATION,
	VISIT_STATUS_AWAITING_LAB,
	VISIT_STATUS_AWAITING_PHARMACY,
)
# Marley tables the EMR does not use: their orders would bypass the consultation flow.
UNUSED_TABLES = {"procedure_prescription": "Procedures", "therapies": "Therapies"}


class EncounterGuardError(frappe.ValidationError):
	"""The encounter change is not allowed (visit link, closed visit, submit or cancel outside the flow)."""


def encounter_before_validate(doc, method=None) -> None:
	doc.submit_orders_on_save = 0
	if not doc.appointment_type:
		doc.appointment_type = DEFAULT_APPOINTMENT_TYPE
	if doc.is_new() and not doc.practitioner and has_any_role(CONSULTING_ROLES):
		doc.practitioner = get_or_create()
	visit = doc.get(ENCOUNTER_VISIT_FIELD)
	if visit:
		if not doc.get("krcs_department"):
			doc.krcs_department = frappe.db.get_value(VISIT_DOCTYPE, visit, "current_department")
		if not doc.company:
			doc.company = frappe.db.get_value(VISIT_DOCTYPE, visit, "company")
	if not doc.medical_department and doc.get("krcs_department"):
		doc.medical_department = doc.krcs_department


def encounter_validate(doc, method=None) -> None:
	visit = doc.get(ENCOUNTER_VISIT_FIELD)
	if not visit:
		frappe.throw(
			_("A consultation must belong to a Patient Visit. Start it from the visit (Start Consultation)."),
			EncounterGuardError,
			title=_("Visit Required"),
		)
	row = frappe.db.get_value(VISIT_DOCTYPE, visit, ["status", "patient"], as_dict=True)
	if not row:
		frappe.throw(_("Patient Visit {0} does not exist.").format(visit), EncounterGuardError)
	if row.patient != doc.patient:
		frappe.throw(
			_("Patient Visit {0} belongs to another patient.").format(visit),
			EncounterGuardError,
			title=_("Wrong Patient"),
		)
	if row.status in VISIT_CLOSED_STATUSES:
		frappe.throw(
			_("Visit {0} is closed ({1}); the consultation can no longer be changed.").format(
				visit, _(row.status)
			),
			EncounterGuardError,
			title=_("Visit Closed"),
		)
	if doc.is_new() and row.status not in CONSULTING_STATUSES:
		frappe.throw(
			_("Visit {0} is {1}. Start the consultation from the visit first.").format(visit, _(row.status)),
			EncounterGuardError,
			title=_("Not Possible Now"),
		)
	if not doc.is_new() and doc.has_value_changed(ENCOUNTER_VISIT_FIELD):
		frappe.throw(_("The visit of a consultation cannot be changed."), EncounterGuardError)
	other = active_encounter_for_visit(visit, exclude=None if doc.is_new() else doc.name)
	if other:
		frappe.throw(
			_("Visit {0} already has consultation {1}.").format(visit, frappe.bold(other)),
			EncounterGuardError,
			title=_("Consultation Exists"),
		)
	for table, label in UNUSED_TABLES.items():
		if doc.get(table):
			frappe.throw(
				_("{0} are not ordered from the consultation.").format(_(label)), EncounterGuardError
			)
	diagnosis.validate_diagnoses(doc)
	diagnosis.rebuild_codification(doc)


def encounter_before_submit(doc, method=None) -> None:
	if not doc.flags.krcs_via_consultation:
		frappe.throw(
			_("Use Complete Consultation (or send a Referral) to complete this consultation."),
			EncounterGuardError,
			title=_("Not Allowed"),
		)
	diagnosis.assert_completable(doc)


def encounter_before_cancel(doc, method=None) -> None:
	frappe.throw(
		_("A consultation cannot be cancelled. Add an addendum to correct it."),
		EncounterGuardError,
		title=_("Not Allowed"),
	)


def encounter_before_update_after_submit(doc, method=None) -> None:
	"""The addendum only grows, and only through `consultation.add_addendum` (integrator hook)."""
	before = doc.get_doc_before_save()
	if not before:
		return
	old = before.get(ADDENDUM_FIELD) or ""
	new = doc.get(ADDENDUM_FIELD) or ""
	if old == new:
		return
	if not doc.flags.krcs_addendum or not new.startswith(old):
		frappe.throw(
			_("The addendum is append-only. Use Add Addendum."), EncounterGuardError, title=_("Not Allowed")
		)


def practitioner_on_update(doc, method=None) -> None:
	"""Remove the User Permission Marley adds for the practitioner's user (D22)."""
	if doc.get("user_id"):
		strip_user_permission(doc.name, doc.user_id)
