# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Consultation endpoints (FR-10 to FR-12, BR-3, SDD §5.3).

Flow (decisions 2026-11-13):

1. `start_consultation(visit)`: Routed → In Consultation (engine) and one draft Patient Encounter per
   visit, pre-filled from triage; calling it again returns the same encounter.
2. `send_lab_orders(encounter)`: lab rows are sent explicitly during the consultation. Marley creates the
   Service Requests; `lab.orders.ensure_observations` (B2) adds the Observations Marley skips for the 2nd
   and later orders; the visit gets `request_lab`.
3. `complete_consultation(encounter, reason)`: diagnosis rule, no unsent lab rows, then submit. Marley's
   `on_submit` releases the prescriptions (Medication Requests) → `prescribe`; then the engine's
   `complete_consultation` (a reason is required while results are pending).
4. `cancel_lab_order(service_request, reason)`: only before any sample or result exists.

The encounter is never cancelled or amended (Phase 2 decision); corrections go in the addendum
(`add_addendum`). Submitting is only possible through this module (`flags.krcs_via_consultation`).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, format_datetime, now_datetime, nowdate, nowtime

from onerc_healthcare.clinical import diagnosis, practitioner
from onerc_healthcare.clinical.utils import (
	CONSULTING_ROLES,
	LAB_WITHDRAWN_FIELD,
	active_encounter_for_visit,
	lock_visit_and_encounter,
	require_roles,
	unsent_lab_rows,
)
from onerc_healthcare.constants import (
	DEFAULT_APPOINTMENT_TYPE,
	ENCOUNTER_DOCTYPE,
	ENCOUNTER_VISIT_FIELD,
	VISIT_CLOSED_STATUSES,
	VISIT_DOCTYPE,
	VISIT_STATUS_IN_CONSULTATION,
	VISIT_STATUS_ROUTED,
)
from onerc_healthcare.visit import engine, tasks

ENSURE_OBSERVATIONS = "onerc_healthcare.lab.orders.ensure_observations"
ADDENDUM_FIELD = "krcs_addendum"
MAX_REASON_LENGTH = engine.MAX_COMMENT_LENGTH


class ConsultationError(frappe.ValidationError):
	"""The consultation action is not possible in the encounter's or visit's current state."""


# --------------------------------------------------------------------------- start


@frappe.whitelist(methods=["POST"])
def start_consultation(visit: str) -> dict:
	"""Start (or reopen) the consultation of `visit`. Returns {"encounter", "created", "visit_status"}."""
	require_roles(CONSULTING_ROLES, _("start a consultation"))
	frappe.has_permission(VISIT_DOCTYPE, "read", doc=visit, throw=True)
	visit_doc = frappe.get_doc(VISIT_DOCTYPE, visit, for_update=True)

	if visit_doc.status == VISIT_STATUS_ROUTED:
		visit_doc = engine.transition(visit, engine.ACTION_START_CONSULTATION)
		from onerc_healthcare.clinical.referral import receive_for_visit

		receive_for_visit(visit_doc)

	# Locking reads after the visit lock (M7): a concurrent call that waited on the visit sees the
	# encounter the first call inserted, instead of its REPEATABLE READ snapshot.
	existing = None
	if visit_doc.get("encounter") and frappe.db.get_value(
		ENCOUNTER_DOCTYPE, {"name": visit_doc.encounter, "docstatus": ["<", 2]}, "name", for_update=True
	):
		existing = visit_doc.encounter
	existing = existing or active_encounter_for_visit(visit, for_update=True)
	if existing:
		if visit_doc.get("encounter") != existing:
			engine.link_encounter(visit, existing)
		return {"encounter": existing, "created": False, "visit_status": visit_doc.status}

	if visit_doc.status != VISIT_STATUS_IN_CONSULTATION:
		frappe.throw(
			_(
				"Visit {0} is {1}. A consultation can be started only for a visit routed to a department."
			).format(visit, frappe.bold(_(visit_doc.status))),
			ConsultationError,
			title=_("Not Possible Now"),
		)

	frappe.has_permission(ENCOUNTER_DOCTYPE, "create", throw=True)
	department = visit_doc.current_department
	doc = frappe.get_doc(
		{
			"doctype": ENCOUNTER_DOCTYPE,
			"patient": visit_doc.patient,
			"practitioner": practitioner.get_or_create(),
			"appointment_type": DEFAULT_APPOINTMENT_TYPE,
			"company": visit_doc.company,
			"encounter_date": nowdate(),
			"encounter_time": nowtime(),
			"medical_department": department,
			ENCOUNTER_VISIT_FIELD: visit,
			"krcs_department": department,
			"krcs_presenting_complaint": _triage_complaint(visit_doc),
		}
	)
	doc.insert()
	engine.link_encounter(visit, doc.name)
	return {"encounter": doc.name, "created": True, "visit_status": visit_doc.status}


def _triage_complaint(visit_doc) -> str | None:
	if not visit_doc.get("triage_vital_signs"):
		return None
	return frappe.db.get_value("Vital Signs", visit_doc.triage_vital_signs, "krcs_chief_complaint")


# --------------------------------------------------------------------------- lab orders


@frappe.whitelist(methods=["POST"])
def send_lab_orders(encounter: str) -> dict:
	"""Send the encounter's unsent lab rows to the laboratory (FR-12/FR-15). Returns the new orders."""
	require_roles(CONSULTING_ROLES, _("send laboratory orders"))
	frappe.has_permission(ENCOUNTER_DOCTYPE, "write", doc=encounter, throw=True)
	visit_doc, doc = lock_visit_and_encounter(encounter)
	_assert_draft(doc)
	visit = _open_visit(doc, visit_doc)
	if not unsent_lab_rows(doc):
		frappe.throw(_("There are no new laboratory tests to send."), ConsultationError)

	before = {row.service_request for row in doc.lab_test_prescription if row.service_request}
	doc.make_service_request()
	doc.save()
	new_orders = [
		row.service_request
		for row in doc.lab_test_prescription
		if row.service_request and row.service_request not in before
	]
	ensure = _ensure_observations_fn()
	for service_request in new_orders:
		if ensure:
			ensure(service_request)
	tasks.raise_event(visit, engine.ACTION_REQUEST_LAB)
	return {
		"service_requests": new_orders,
		"observations_ensured": bool(ensure),
		"visit_status": frappe.db.get_value(VISIT_DOCTYPE, visit, "status"),
	}


def _ensure_observations_fn():
	"""B2's `lab.orders.ensure_observations`, or None while it is not available yet."""
	try:
		return frappe.get_attr(ENSURE_OBSERVATIONS)
	except (ImportError, AttributeError):
		return None


@frappe.whitelist(methods=["POST"])
def cancel_lab_order(service_request: str, reason: str) -> dict:
	"""Withdraw a sent lab order before any sample or result exists (B2 updates the lab task)."""
	require_roles(CONSULTING_ROLES, _("cancel a laboratory order"))
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Give the reason for cancelling the laboratory order."), ConsultationError)
	if len(reason) > MAX_REASON_LENGTH:
		frappe.throw(
			_("The reason is limited to {0} characters.").format(MAX_REASON_LENGTH), ConsultationError
		)
	source = frappe.db.get_value(
		"Service Request", service_request, ["source_doc", "order_group"], as_dict=True
	)
	if not source or source.source_doc != ENCOUNTER_DOCTYPE or not source.order_group:
		frappe.throw(_("{0} is not a consultation order.").format(service_request), ConsultationError)
	frappe.has_permission(ENCOUNTER_DOCTYPE, "write", doc=source.order_group, throw=True)
	lock_visit_and_encounter(source.order_group)
	sr = frappe.get_doc("Service Request", service_request, for_update=True)
	if sr.docstatus != 1:
		frappe.throw(_("Laboratory order {0} is not active.").format(service_request), ConsultationError)
	if lab_work_started(service_request):
		frappe.throw(
			_(
				"The laboratory has already collected a sample or entered a result for {0}; it can no longer"
				" be cancelled."
			).format(service_request),
			ConsultationError,
			title=_("Not Possible Now"),
		)
	sr.add_comment("Comment", _("Cancelled by the clinician: {0}").format(reason))
	sr.flags.ignore_permissions = True
	sr.cancel()
	return {"service_request": sr.name, "docstatus": sr.docstatus}


def lab_work_started(service_request: str) -> bool:
	"""The laboratory has started on the order: a submitted Observation or Sample Collection, or a sample
	row marked Collected on a draft Sample Collection."""
	if frappe.db.exists("Observation", {"service_request": service_request, "docstatus": 1}):
		return True
	if frappe.db.exists("Sample Collection", {"service_request": service_request, "docstatus": 1}):
		return True
	# A sample row of the order counts once its collection is submitted, or once the row itself is marked
	# Collected on a still-draft Sample Collection.
	return bool(
		frappe.db.sql(
			"""select 1 from `tabObservation Sample Collection` osc
			join `tabSample Collection` sc on sc.name = osc.parent
			where osc.service_request = %s
				and (sc.docstatus = 1 or (sc.docstatus = 0 and osc.status = 'Collected'))
			limit 1""",
			(service_request,),
		)
	)


# --------------------------------------------------------------------------- complete


@frappe.whitelist(methods=["POST"])
def complete_consultation(
	encounter: str, reason: str | None = None, no_diagnosis_reason: str | None = None
) -> dict:
	"""Complete the consultation: submit the encounter, release prescriptions, move the visit on.

	`reason` is required while laboratory results are pending; `no_diagnosis_reason` when there is no
	diagnosis (it is stored on the encounter).
	"""
	require_roles(CONSULTING_ROLES, _("complete a consultation"))
	frappe.has_permission(ENCOUNTER_DOCTYPE, "write", doc=encounter, throw=True)
	visit_doc, doc = lock_visit_and_encounter(encounter)
	_assert_draft(doc)
	visit = _open_visit(doc, visit_doc)
	reason = (reason or "").strip() or None

	# Pre-check the engine rule so nothing is submitted when the visit cannot be completed.
	rule = engine.TRANSITIONS[engine.ACTION_COMPLETE_CONSULTATION]
	if visit_doc.status not in rule.from_statuses:
		frappe.throw(
			_("Visit {0} is {1}; the consultation cannot be completed now.").format(
				visit, frappe.bold(_(visit_doc.status))
			),
			ConsultationError,
			title=_("Not Possible Now"),
		)
	if cint(visit_doc.lab_open) and not reason:
		frappe.throw(
			_("Laboratory results are still pending. Give a reason to complete the consultation now."),
			ConsultationError,
			title=_("Reason Required"),
		)

	if no_diagnosis_reason and not doc.get(diagnosis.DIAGNOSES_FIELD):
		doc.set(diagnosis.NO_DIAGNOSIS_REASON_FIELD, no_diagnosis_reason.strip())
	medication_requests = submit_encounter(doc)
	if visit_doc.get("encounter") != doc.name:
		engine.link_encounter(visit, doc.name)
	if medication_requests:
		tasks.raise_event(visit, engine.ACTION_PRESCRIBE)
	visit_doc = engine.transition(visit, engine.ACTION_COMPLETE_CONSULTATION, comment=reason)
	return {
		"encounter": doc.name,
		"medication_requests": medication_requests,
		"visit_status": visit_doc.status,
	}


def submit_encounter(doc) -> list[str]:
	"""Check the completion rules and submit (Marley releases the orders). Returns new Medication Requests.

	Used by `complete_consultation` and by the Referral `send` action.
	"""
	diagnosis.assert_completable(doc)
	unsent = unsent_lab_rows(doc)
	if unsent:
		frappe.throw(
			_("Send or remove the unsent laboratory tests first: {0}.").format(
				", ".join(frappe.bold(r.observation_template or r.lab_test_code) for r in unsent)
			),
			ConsultationError,
			title=_("Unsent Laboratory Tests"),
		)
	before = {row.medication_request for row in doc.get("drug_prescription") or [] if row.medication_request}
	doc.flags.krcs_via_consultation = True
	try:
		doc.submit()
	finally:
		doc.flags.krcs_via_consultation = False
	return [
		row.medication_request
		for row in doc.get("drug_prescription") or []
		if row.medication_request and row.medication_request not in before
	]


# --------------------------------------------------------------------------- addendum


@frappe.whitelist(methods=["POST"])
def add_addendum(encounter: str, text: str) -> dict:
	"""Append a dated, signed correction to a completed consultation (no cancel/amend in Phase 2)."""
	require_roles(CONSULTING_ROLES, _("add an addendum"))
	frappe.has_permission(ENCOUNTER_DOCTYPE, "write", doc=encounter, throw=True)
	text = (text or "").strip()
	if not text:
		frappe.throw(_("Enter the addendum text."), ConsultationError)
	if len(text) > 5000:
		frappe.throw(_("The addendum is limited to 5000 characters."), ConsultationError)
	_visit_doc, doc = lock_visit_and_encounter(encounter)
	if doc.docstatus != 1:
		frappe.throw(
			_("An addendum is added to a completed consultation. Edit the draft directly instead."),
			ConsultationError,
		)
	user = frappe.utils.get_fullname(frappe.session.user)
	entry = f"[{format_datetime(now_datetime())}, {user}]\n{text}"
	current = doc.get(ADDENDUM_FIELD) or ""
	doc.set(ADDENDUM_FIELD, f"{current}\n\n{entry}" if current else entry)
	doc.flags.krcs_addendum = True
	try:
		doc.save()
	finally:
		doc.flags.krcs_addendum = False
	return {"encounter": doc.name, ADDENDUM_FIELD: doc.get(ADDENDUM_FIELD)}


# --------------------------------------------------------------------------- visit closed


def on_visit_closed(visit_doc) -> None:
	"""Close Visit handler (`tasks.VISIT_CLOSED_HANDLERS`): withdraw the draft encounter's unsent lab rows.

	The rows are kept and flagged `krcs_withdrawn` (L4: clinical content is never deleted); they are
	never sent afterwards.

	The draft encounter itself is left as it is (no cancel in Phase 2); it can no longer be saved or
	completed because its visit is closed.
	"""
	encounter = active_encounter_for_visit(visit_doc.name)
	if not encounter or frappe.db.get_value(ENCOUNTER_DOCTYPE, encounter, "docstatus") != 0:
		return
	doc = frappe.get_doc(ENCOUNTER_DOCTYPE, encounter)
	unsent = unsent_lab_rows(doc)
	if not unsent:
		return
	for row in unsent:
		frappe.db.set_value("Lab Prescription", row.name, LAB_WITHDRAWN_FIELD, 1, update_modified=False)
	doc.add_comment(
		"Comment",
		_("Visit {0} was closed; unsent laboratory tests withdrawn: {1}").format(
			visit_doc.name, ", ".join(r.observation_template or r.lab_test_code for r in unsent)
		),
	)


# --------------------------------------------------------------------------- helpers


def _assert_draft(doc) -> None:
	if doc.docstatus != 0:
		frappe.throw(
			_("Consultation {0} is already completed. Use Add Addendum for corrections.").format(doc.name),
			ConsultationError,
			title=_("Already Completed"),
		)


def _open_visit(doc, visit_doc) -> str:
	"""The encounter's visit name; refuses a closed visit. `visit_doc` is the locked visit (M7)."""
	visit = doc.get(ENCOUNTER_VISIT_FIELD)
	if not visit or not visit_doc:
		frappe.throw(_("Consultation {0} has no visit.").format(doc.name), ConsultationError)
	status = visit_doc.status
	if status in VISIT_CLOSED_STATUSES:
		frappe.throw(
			_("Visit {0} is closed ({1}).").format(visit, _(status)),
			ConsultationError,
			title=_("Visit Closed"),
		)
	return visit
