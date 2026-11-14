# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Observation / Service Request / Sample Collection doc_events (wired in hooks.py). Owned by B2.

Visit events come from Observations and Service Request cancellation only, never from Service Request
status (Phase 2 brief, fact 3). Lock order is Visit -> Encounter -> Observation / Diagnostic Report: the
approve / reject endpoints (`lab.guards`) and Sample Collection cancel lock the visit before any lab row;
``before_submit`` / ``before_cancel`` lock it (NOWAIT) before Frappe writes the Observation; the ``on_*``
handlers re-read the encounter's Observations with ``FOR UPDATE SKIP LOCKED``
(`lab.tasks.EncounterLab(lock=True)`), so two results approved at the same moment can neither both see
the other as pending (write skew) nor wait on each other (deadlock). Visit events are
raised only through `visit.tasks.raise_event`, which never fails a lab record because of the visit's state.

| Event | Condition | Effect |
|---|---|---|
| Observation before submit | ``time_of_approval`` empty | stamped with the submit time (the approval), see `stamp_approval_time` |
| Observation submit | its request becomes done | "Lab result ready" notification to the clinician (FR-17) |
| Observation submit | the encounter's lab task becomes complete | ``lab_results_ready`` |
| Observation cancel (rejection) | its request was done and no longer is; visit open, ``lab_open == 0`` | ``request_lab`` with "Result ... rejected; repeat pending" |
| Observation cancel (rejection) | as above | "Lab result withdrawn" notification to the clinician; also the Medical In-Charge when the visit is closed |
| Observation cancel | always | its Patient Medical Record removed as the system (see `remove_medical_record`) |
| Service Request cancel | lab order, ``lab_open == 1``, lab task now complete | ``lab_results_ready`` |
| Sample Collection before cancel | always | unlink its Diagnostic Report, so Marley's ``on_cancel`` does not try to delete it; re-point its Observations to the encounter |

"Becomes" compares the state with this document's change undone against the state with it applied, so a
panel approved component by component notifies once, and a stray later approval raises nothing.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import now_datetime

from onerc_healthcare.constants import ENCOUNTER_DOCTYPE, VISIT_DOCTYPE, VISIT_OPEN_STATUSES
from onerc_healthcare.lab import notify
from onerc_healthcare.lab.tasks import LAB_TEMPLATE_DT, EncounterLab
from onerc_healthcare.visit import engine
from onerc_healthcare.visit.tasks import raise_event, visit_for_encounter

_TREE_FIELDS = ["name", "parent_observation", "service_request", "reference_doctype", "reference_docname"]
MAX_DEPTH = 20


def _root(doc) -> frappe._dict:
	row = frappe._dict({f: doc.get(f) for f in _TREE_FIELDS})
	seen = {row.name}
	for _depth in range(MAX_DEPTH):
		if not row.parent_observation or row.parent_observation in seen:
			break
		parent = frappe.db.get_value("Observation", row.parent_observation, _TREE_FIELDS, as_dict=True)
		if not parent:
			break
		seen.add(parent.name)
		row = parent
	return row


def observation_context(doc) -> frappe._dict | None:
	"""Encounter and visit of an Observation: root Observation -> its Service Request's encounter, else
	the root's reference (the encounter, or a Sample Collection made for the encounter)."""
	root = _root(doc)
	encounter = None
	request = root.service_request or doc.get("service_request")
	if request:
		sr = frappe.db.get_value(
			"Service Request", request, ["source_doc", "order_group", "template_dt"], as_dict=True
		)
		if sr and sr.template_dt == LAB_TEMPLATE_DT and sr.source_doc == ENCOUNTER_DOCTYPE:
			encounter = sr.order_group
	if not encounter and root.reference_docname:
		if root.reference_doctype == ENCOUNTER_DOCTYPE:
			encounter = root.reference_docname
		elif root.reference_doctype == "Sample Collection":
			ref = frappe.db.get_value(
				"Sample Collection", root.reference_docname, ["reference_doc", "reference_name"], as_dict=True
			)
			if ref and ref.reference_doc == ENCOUNTER_DOCTYPE:
				encounter = ref.reference_name
	if not encounter:
		return None
	return frappe._dict(encounter=encounter, visit=visit_for_encounter(encounter), root=root.name)


class VisitBusyError(frappe.ValidationError):
	"""The visit is locked by another transaction and this one already holds a lab row: retry."""


def _lock_visit(visit: str | None, wait: bool = True) -> None:
	"""Row-lock the Patient Visit. ``wait=False`` (NOWAIT) is for callers that already hold a lab row
	(Frappe's ``check_if_latest`` locks the Observation before any hook): waiting there could close a cycle
	with a transaction that holds the visit and wants that row, so it fails fast with `VisitBusyError`. A
	lock this transaction already holds is granted at once either way."""
	if not visit:
		return
	if wait:
		frappe.db.get_value(VISIT_DOCTYPE, visit, "name", for_update=True)
		return
	try:
		frappe.db.get_value(VISIT_DOCTYPE, visit, "name", for_update=True, wait=False)
	except frappe.QueryTimeoutError:
		frappe.throw(
			_("Another laboratory update for visit {0} is in progress. Try again in a moment.").format(visit),
			VisitBusyError,
			title=_("Busy"),
		)


def _request_row(lab: EncounterLab, name: str) -> frappe._dict:
	return next(r for r in lab.requests if r.name == name)


def lock_observation_visit(doc) -> None:
	"""before_submit / before_cancel: lock the Observation's visit before Frappe writes the Observation.

	The approval and rejection endpoints (`lab.guards`) take the visit lock before they load any
	Observation, so here it is already held. Any other path (desk Submit) arrives holding the Observation
	row, so it takes the visit lock with NOWAIT and gets "try again" instead of a deadlock."""
	ctx = observation_context(doc)
	if ctx:
		_lock_visit(ctx.visit, wait=False)


def visit_for_observation(observation: str) -> str | None:
	ctx = observation_context(frappe.get_doc("Observation", observation))
	return ctx.visit if ctx else None


def visit_for_report(diagnostic_report: str) -> str | None:
	ref = frappe.db.get_value(
		"Diagnostic Report", diagnostic_report, ["ref_doctype", "docname"], as_dict=True
	)
	if ref and ref.ref_doctype == ENCOUNTER_DOCTYPE and ref.docname:
		return visit_for_encounter(ref.docname)
	return None


def visit_for_sample_collection(doc) -> str | None:
	if doc.reference_doc == ENCOUNTER_DOCTYPE and doc.reference_name:
		return visit_for_encounter(doc.reference_name)
	return None


def observation_before_submit(doc, method=None) -> None:
	lock_observation_visit(doc)
	stamp_approval_time(doc)


def stamp_approval_time(doc) -> None:
	"""Record when the result was approved (submitted), for MOH lab counts (R4 review L1).

	Marley's ``validate`` fills ``time_of_approval`` only for status "Final" and clears it otherwise; KRCS
	approves with "Approved", so it stayed empty and reports fell back to ``modified``, which later writes
	(``add_note``) move. Frappe runs ``validate`` before ``before_submit`` and never after submit, so the
	value set here is written by the submit itself and Marley never clears it."""
	if not doc.get("time_of_approval"):
		doc.time_of_approval = now_datetime()


def observation_before_cancel(doc, method=None) -> None:
	lock_observation_visit(doc)


def observation_on_submit(doc, method=None) -> None:
	ctx = observation_context(doc)
	if not ctx:
		return
	_lock_visit(ctx.visit)
	lab = EncounterLab(ctx.encounter, lock=True)
	if doc.name not in lab.rows:
		return
	before = lab.states({doc.name: 0})
	now = lab.states()
	request = lab.request_of(doc.name)
	if request and now[request] and not before[request]:
		notify.result_ready(ctx, _request_row(lab, request))
	if ctx.visit and all(now.values()) and not all(before.values()):
		raise_event(ctx.visit, engine.ACTION_LAB_RESULTS_READY)


def observation_on_cancel(doc, method=None) -> None:
	"""A rejection cancels the Observation, then Marley inserts a draft copy for the repeat."""
	remove_medical_record(doc)
	ctx = observation_context(doc)
	if not ctx:
		return
	_lock_visit(ctx.visit)
	lab = EncounterLab(ctx.encounter, lock=True)
	if doc.name not in lab.rows:
		return
	request = lab.request_of(doc.name)
	if not request:
		return
	was_done = lab.is_done(request, {doc.name: 1})
	if not was_done or lab.is_done(request, strict=True):
		return
	visit_closed = True
	if ctx.visit:
		visit = frappe.db.get_value(
			VISIT_DOCTYPE, ctx.visit, ["status", "lab_open"], as_dict=True, for_update=True
		)
		visit_closed = visit.status not in VISIT_OPEN_STATUSES
		if not visit_closed and not visit.lab_open:
			raise_event(
				ctx.visit,
				engine.ACTION_REQUEST_LAB,
				comment=_("Result {0} ({1}) rejected; repeat pending").format(
					doc.observation_template, doc.name
				),
			)
	notify.result_rejected(ctx, _request_row(lab, request), visit_closed)


def remove_medical_record(doc) -> None:
	"""Marley's '*' ``on_cancel`` hook `delete_medical_record` (it runs after this DocType's hooks) deletes
	the cancelled Observation's Patient Medical Record with the *user's* delete right. No EMR role has
	delete, so a Lab Technician's rejection would fail. Remove the same row as the system first (Patient
	Medical Record is outside the delete guard for this reason); Marley's hook then finds nothing."""
	from healthcare.healthcare.doctype.patient_history_settings.patient_history_settings import (
		validate_medical_record_required,
	)

	if not validate_medical_record_required(doc):
		return
	record = frappe.db.exists("Patient Medical Record", {"reference_name": doc.name})
	if record:
		frappe.delete_doc("Patient Medical Record", record, force=1, ignore_permissions=True)


def service_request_on_cancel(doc, method=None) -> None:
	if doc.template_dt != LAB_TEMPLATE_DT or doc.source_doc != ENCOUNTER_DOCTYPE or not doc.order_group:
		return
	visit = visit_for_encounter(doc.order_group)
	if not visit:
		return
	_lock_visit(visit)
	if not frappe.db.get_value(VISIT_DOCTYPE, visit, "lab_open", for_update=True):
		return
	if EncounterLab(doc.order_group, lock=True).is_complete():
		raise_event(
			visit,
			engine.ACTION_LAB_RESULTS_READY,
			comment=_("Laboratory request {0} cancelled").format(doc.name),
		)


def sample_collection_before_cancel(doc, method=None) -> None:
	"""Marley's `Sample Collection.on_cancel` deletes the linked Diagnostic Report, which the delete guard
	refuses (NFR-9). Unlink it first: the report belongs to the encounter and keeps its results.

	The Observations collected on this Sample Collection reference it; once it is cancelled, saving a result
	on them fails ("Cannot link cancelled document"). They are re-pointed to the encounter, which is the same
	Diagnostic Report key Marley derives from the Sample Collection (`set_diagnostic_report_status`)."""
	_lock_visit(visit_for_sample_collection(doc))  # lock order Visit -> Observation / Diagnostic Report
	frappe.db.set_value("Diagnostic Report", {"sample_collection": doc.name}, "sample_collection", None)
	if doc.reference_doc == ENCOUNTER_DOCTYPE and doc.reference_name:
		obs = DocType("Observation")
		names = (
			frappe.qb.from_(obs)
			.select(obs.name)
			.where(obs.patient == doc.patient)
			.where(obs.reference_doctype == "Sample Collection")
			.where(obs.reference_docname == doc.name)
			.where(obs.docstatus != 2)
			.for_update()
			.run(pluck=True)
		)
		for name in names:
			frappe.db.set_value(
				"Observation",
				name,
				{"reference_doctype": ENCOUNTER_DOCTYPE, "reference_docname": doc.reference_name},
				update_modified=False,
			)
