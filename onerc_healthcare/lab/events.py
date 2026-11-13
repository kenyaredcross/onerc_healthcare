# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Observation / Service Request / Sample Collection doc_events (wired in hooks.py). Owned by B2.

Visit events come from Observations and Service Request cancellation only, never from Service Request
status (Phase 2 brief, fact 3). Every handler locks the Patient Visit row first, then reads the
encounter's Observations with ``FOR UPDATE`` (`lab.tasks.EncounterLab(lock=True)`), so two results
approved at the same moment cannot both see the other one as pending (write skew). Visit events are
raised only through `visit.tasks.raise_event`, which never fails a lab record because of the visit's state.

| Event | Condition | Effect |
|---|---|---|
| Observation submit | its request becomes done | "Lab result ready" notification to the clinician (FR-17) |
| Observation submit | the encounter's lab task becomes complete | ``lab_results_ready`` |
| Observation cancel (rejection) | its request was done and no longer is; visit open, ``lab_open == 0`` | ``request_lab`` with "Result ... rejected; repeat pending" |
| Observation cancel (rejection) | as above | "Lab result withdrawn" notification to the clinician; also the Medical In-Charge when the visit is closed |
| Observation cancel | always | its Patient Medical Record removed as the system (see `remove_medical_record`) |
| Service Request cancel | lab order, ``lab_open == 1``, lab task now complete | ``lab_results_ready`` |
| Sample Collection before cancel | always | unlink its Diagnostic Report, so Marley's ``on_cancel`` does not try to delete it |

"Becomes" compares the state with this document's change undone against the state with it applied, so a
panel approved component by component notifies once, and a stray later approval raises nothing.
"""

from __future__ import annotations

import frappe
from frappe import _

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


def _lock_visit(visit: str | None) -> None:
	if visit:
		frappe.db.get_value(VISIT_DOCTYPE, visit, "name", for_update=True)


def _request_row(lab: EncounterLab, name: str) -> frappe._dict:
	return next(r for r in lab.requests if r.name == name)


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
		visit = frappe.db.get_value(VISIT_DOCTYPE, ctx.visit, ["status", "lab_open"], as_dict=True)
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
	if not frappe.db.get_value(VISIT_DOCTYPE, visit, "lab_open"):
		return
	if EncounterLab(doc.order_group, lock=True).is_complete():
		raise_event(
			visit,
			engine.ACTION_LAB_RESULTS_READY,
			comment=_("Laboratory request {0} cancelled").format(doc.name),
		)


def sample_collection_before_cancel(doc, method=None) -> None:
	"""Marley's `Sample Collection.on_cancel` deletes the linked Diagnostic Report, which the delete guard
	refuses (NFR-9). Unlink it first: the report belongs to the encounter and keeps its results."""
	frappe.db.set_value("Diagnostic Report", {"sample_collection": doc.name}, "sample_collection", None)
