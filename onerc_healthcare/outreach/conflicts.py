# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Sync Conflicts: raising, marking and resolving them (Phase 4, O2; FR-26, OUT-19..23; OI-11 / D58).

**Raised** by `outreach.apply` while a synced record is applied (the record is then stored Conflict, with its
payload, and the records that depend on it are stored Held):

| Type | When | Outreach record |
|---|---|---|
| Duplicate registration | a patient.register whose identifier is already registered, which is a strong duplicate (same name, DOB, sex), or whose similarity score is at least ``conflict_score`` | not applied (Conflict) |
| Demographic change | a patient.update whose patient changed on the server after the package (``modified`` later than ``base_modified``) and a changed field now differs from the server value | not applied (Conflict) |
| Overlapping visit | a visit.capture for a patient with an open visit (the one-open-visit rule) | not applied (Conflict, held) |
| Overlapping visit, review only | a visit.capture for a patient with a closed facility visit on the same date | applied (Accepted) |

``Patient.krcs_open_sync_conflicts`` counts the patient's Open conflicts that hold an outreach record (review-only
conflicts hold nothing and are not counted), so the record is marked (FR-26).

**Resolved** only through `resolve` (POST) by the HIS Officer, or the Medical In-Charge as backup
(``CONFLICT_RESOLVER_ROLES``), with a reason. Lifecycle (`TRANSITIONS`; every action appends a row to the
conflict's log):

| Action | From | To | Who |
|---|---|---|---|
| raise | (new) | Open | the sync (as the team member) |
| resolve (a decision valid for the type, `DECISIONS`) | Open | Resolved | HIS Officer, Medical In-Charge |
| retry (held overlapping visit, after the facility visit was closed) | Open | Resolved, or Open when it still fails (logged) | HIS Officer, Medical In-Charge |

What each decision does (`DECISION_EFFECTS`). Outreach data is always applied as the team member who captured it
(`apply.acting_as` + `outreach_actor`), so they remain its author; nothing is deleted, and a discarded payload
stays on its Outreach Sync Record:

| Type | Decision | Effect |
|---|---|---|
| Duplicate registration | Keep facility version | registration Discarded; held records use the chosen existing patient |
| Duplicate registration | Link as the same patient | outreach identifiers the patient lacks are added; Accepted as the existing patient |
| Duplicate registration | Merge field by field | the fields chosen from the outreach version and the new identifiers are written to the existing patient; Accepted |
| Duplicate registration | Keep outreach version | a new patient is registered (the reason is the duplicate override reason); Accepted |
| Demographic change | Keep facility version | changes Discarded |
| Demographic change | Keep outreach version | every change applied; Accepted |
| Demographic change | Merge field by field | only the fields chosen from the outreach version applied; Accepted |
| Overlapping visit | Keep facility version | outreach visit Discarded |
| Overlapping visit | Keep outreach version (Retry) | the visit is applied now; refused while the patient still has an open visit |
| Overlapping visit (review only) | Reviewed - keep both | nothing changes; both visits stand |

After a decision the records held on the conflict are replayed automatically, in the order received.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, cstr, now_datetime

from onerc_healthcare.constants import (
	CONFLICT_DEMOGRAPHIC_CHANGE,
	CONFLICT_DUPLICATE_REGISTRATION,
	CONFLICT_OPEN,
	CONFLICT_OVERLAPPING_VISIT,
	CONFLICT_RESOLVED,
	CONFLICT_RESOLVER_ROLES,
	DECISION_ACKNOWLEDGE,
	DECISION_KEEP_FACILITY,
	DECISION_KEEP_OUTREACH,
	DECISION_LINK,
	DECISION_MERGE,
	OUTREACH_RECORD_DOCTYPE,
	PATIENT_DOB_ESTIMATED_FIELD,
	PATIENT_ESTIMATED_AGE_MONTHS_FIELD,
	PATIENT_ESTIMATED_AGE_YEARS_FIELD,
	PATIENT_IDENTIFIERS_FIELD,
	PATIENT_OPEN_CONFLICTS_FIELD,
	RECORD_STATUS_ACCEPTED,
	RECORD_STATUS_CONFLICT,
	RECORD_STATUS_DISCARDED,
	RECORD_STATUS_HELD,
	SYNC_CONFLICT_DOCTYPE,
	VISIT_DOCTYPE,
)
from onerc_healthcare.outreach import apply
from onerc_healthcare.outreach.context import outreach_actor
from onerc_healthcare.registration.identifiers import RESIDENCE_FIELD, find_identifier_owner

MAX_REASON_LENGTH = 1000
RATE_WINDOW = 60 * 60
RESOLVE_RATE_LIMIT = 600
SAVEPOINT = "krcs_conflict_resolve"
RETRY_REASON = "Retried after the facility visit was closed."

# Decisions valid for each (conflict type, review only).
DECISIONS: dict[tuple[str, bool], tuple[str, ...]] = {
	(CONFLICT_DUPLICATE_REGISTRATION, False): (
		DECISION_KEEP_FACILITY,
		DECISION_LINK,
		DECISION_MERGE,
		DECISION_KEEP_OUTREACH,
	),
	(CONFLICT_DEMOGRAPHIC_CHANGE, False): (DECISION_KEEP_FACILITY, DECISION_MERGE, DECISION_KEEP_OUTREACH),
	(CONFLICT_OVERLAPPING_VISIT, False): (DECISION_KEEP_FACILITY, DECISION_KEEP_OUTREACH),
	(CONFLICT_OVERLAPPING_VISIT, True): (DECISION_ACKNOWLEDGE,),
}
# (type, decision) -> Outreach Sync Record status afterwards (None: unchanged) and the module-docstring effect.
DECISION_EFFECTS: dict[tuple[str, str], tuple[str | None, str]] = {
	(CONFLICT_DUPLICATE_REGISTRATION, DECISION_KEEP_FACILITY): (
		RECORD_STATUS_DISCARDED,
		"registration discarded; held records use the existing patient",
	),
	(CONFLICT_DUPLICATE_REGISTRATION, DECISION_LINK): (
		RECORD_STATUS_ACCEPTED,
		"identifiers the existing patient lacks are added; held records use the existing patient",
	),
	(CONFLICT_DUPLICATE_REGISTRATION, DECISION_MERGE): (
		RECORD_STATUS_ACCEPTED,
		"chosen outreach fields and new identifiers written to the existing patient",
	),
	(CONFLICT_DUPLICATE_REGISTRATION, DECISION_KEEP_OUTREACH): (
		RECORD_STATUS_ACCEPTED,
		"a new patient is registered with the reason as duplicate override",
	),
	(CONFLICT_DEMOGRAPHIC_CHANGE, DECISION_KEEP_FACILITY): (RECORD_STATUS_DISCARDED, "changes discarded"),
	(CONFLICT_DEMOGRAPHIC_CHANGE, DECISION_MERGE): (RECORD_STATUS_ACCEPTED, "chosen changes applied"),
	(CONFLICT_DEMOGRAPHIC_CHANGE, DECISION_KEEP_OUTREACH): (RECORD_STATUS_ACCEPTED, "all changes applied"),
	(CONFLICT_OVERLAPPING_VISIT, DECISION_KEEP_FACILITY): (
		RECORD_STATUS_DISCARDED,
		"outreach visit discarded",
	),
	(CONFLICT_OVERLAPPING_VISIT, DECISION_KEEP_OUTREACH): (
		RECORD_STATUS_ACCEPTED,
		"outreach visit applied (refused while an open visit remains)",
	),
	(CONFLICT_OVERLAPPING_VISIT, DECISION_ACKNOWLEDGE): (None, "nothing changes; both visits stand"),
}
# Decisions that need an existing patient (a candidate) for a duplicate registration.
NEEDS_EXISTING_PATIENT = (DECISION_KEEP_FACILITY, DECISION_LINK, DECISION_MERGE)


@dataclass(frozen=True)
class ConflictTransition:
	label: str
	from_statuses: tuple[str | None, ...]
	to_status: str
	roles: tuple[str, ...]


TRANSITIONS: dict[str, ConflictTransition] = {
	"raise": ConflictTransition(_("Raise"), (None,), CONFLICT_OPEN, ()),
	"resolve": ConflictTransition(_("Resolve"), (CONFLICT_OPEN,), CONFLICT_RESOLVED, CONFLICT_RESOLVER_ROLES),
	"retry": ConflictTransition(_("Retry"), (CONFLICT_OPEN,), CONFLICT_RESOLVED, CONFLICT_RESOLVER_ROLES),
}

# Patient fields compared side by side, and those a Merge may take from the outreach version.
PATIENT_COMPARE = (
	("first_name", "First Name"),
	("middle_name", "Middle Name"),
	("last_name", "Last Name"),
	("sex", "Sex"),
	("dob", "Date of Birth"),
	("mobile", "Mobile"),
	("residence", "Residence"),
	("allergies", "Allergies"),
	("identifiers", "Identifiers"),
)
MERGE_FIELDS = ("first_name", "middle_name", "last_name", "sex", "dob", "mobile", "residence", "allergies")
VISIT_COMPARE = (
	("visit", "Visit"),
	("date", "Date"),
	("department", "Department"),
	("status", "Status"),
	("complaint", "Complaint"),
	("diagnoses", "Diagnoses"),
	("outcome", "Outcome"),
)


class SyncConflictError(frappe.ValidationError):
	"""The decision is not possible for this conflict."""


def decisions_for(doc) -> tuple[str, ...]:
	return DECISIONS.get((doc.conflict_type, bool(cint(doc.review_only))), ())


# --------------------------------------------------------------------------------------------- raising


def _insert(rec, conflict_type: str, patient: str | None, summary: str, facility, outreach, **kw) -> str:
	doc = frappe.get_doc(
		{
			"doctype": SYNC_CONFLICT_DOCTYPE,
			"conflict_type": conflict_type,
			"status": CONFLICT_OPEN,
			"review_only": cint(kw.get("review_only")),
			"summary": summary[:500],
			"patient": patient,
			"session": rec.session.name,
			"sync_record": rec.name,
			"raised_by": rec.user,
			"raised_at": now_datetime(),
			"facility_version": json.dumps(facility, default=str),
			"outreach_version": json.dumps(outreach, default=str),
			"candidates": json.dumps(kw.get("candidates") or [], default=str),
		}
	)
	doc.append(
		"log",
		{
			"action": "raise",
			"from_status": "",
			"to_status": CONFLICT_OPEN,
			"user": rec.user,
			"timestamp": now_datetime(),
			"note": summary[:500],
		},
	)
	doc.insert(ignore_permissions=True)  # raised by the sync as the team member (no DocPerm on it)
	if patient:
		refresh_open_conflicts(patient)
	return doc.name


def patient_version(patient: str) -> dict:
	"""The facility copy of a patient's demographics (for the side-by-side view)."""
	row = frappe.db.get_value(
		"Patient",
		patient,
		[
			"name",
			"patient_name",
			"first_name",
			"middle_name",
			"last_name",
			"sex",
			"dob",
			"mobile",
			"allergies",
			"modified",
			RESIDENCE_FIELD,
			PATIENT_DOB_ESTIMATED_FIELD,
			PATIENT_ESTIMATED_AGE_YEARS_FIELD,
			PATIENT_ESTIMATED_AGE_MONTHS_FIELD,
		],
		as_dict=True,
	)
	out = {k: row.get(k) for k in ("name", "patient_name", "first_name", "middle_name", "last_name", "sex")}
	out.update(
		{
			"dob": cstr(row.dob) or None,
			"dob_estimated": cint(row.get(PATIENT_DOB_ESTIMATED_FIELD)),
			"mobile": row.mobile,
			"residence": row.get(RESIDENCE_FIELD),
			"allergies": row.allergies,
			"modified": cstr(row.modified),
			"identifiers": _identifiers(patient),
		}
	)
	return out


def _identifiers(patient: str) -> list[str]:
	rows = frappe.get_all(
		"Patient Identifier",
		filters={"parent": patient, "parenttype": "Patient", "parentfield": PATIENT_IDENTIFIERS_FIELD},
		fields=["identifier_type", "identifier_value"],
		order_by="idx asc",
	)
	return [f"{r.identifier_type}: {r.identifier_value}" for r in rows]


def raise_registration_conflict(rec, doc, candidates: list[dict]) -> str:
	"""Duplicate registration: `doc` is the unsaved outreach Patient (its DOB rules applied)."""
	best = candidates[0]
	outreach = {
		"first_name": doc.first_name,
		"middle_name": doc.middle_name,
		"last_name": doc.last_name,
		"sex": doc.sex,
		"dob": cstr(doc.dob) or None,
		"dob_estimated": cint(doc.get(PATIENT_DOB_ESTIMATED_FIELD)),
		"estimated_age_years": cint(doc.get(PATIENT_ESTIMATED_AGE_YEARS_FIELD)),
		"estimated_age_months": cint(doc.get(PATIENT_ESTIMATED_AGE_MONTHS_FIELD)),
		"mobile": doc.mobile,
		"residence": doc.get(RESIDENCE_FIELD),
		"allergies": doc.allergies,
		"identifiers": [
			f"{r.identifier_type}: {r.identifier_value}" for r in doc.get(PATIENT_IDENTIFIERS_FIELD)
		],
	}
	how = ", ".join(_(m) for m in best["matched_by"])
	summary = _("Duplicate registration: the outreach patient matches {0} ({1}).").format(
		best["patient"], how
	)
	return _insert(
		rec,
		CONFLICT_DUPLICATE_REGISTRATION,
		best["patient"],
		summary,
		patient_version(best["patient"]),
		outreach,
		candidates=candidates,
	)


def raise_update_conflict(rec, doc, changes: dict, differing: list[str]) -> str:
	"""Demographic change: the server Patient `doc` changed after the package."""
	facility = {key: _wire_value(doc, key) for key in changes}
	facility.update({"modified": cstr(doc.modified), "name": doc.name, "patient_name": doc.patient_name})
	outreach = dict(changes)
	outreach["base_modified"] = rec.payload.get("base_modified")
	outreach["add_identifiers"] = rec.payload.get("add_identifiers") or []
	summary = _("Demographic change: {0} changed on the server after the package ({1}).").format(
		doc.name, ", ".join(differing)
	)
	return _insert(rec, CONFLICT_DEMOGRAPHIC_CHANGE, doc.name, summary, facility, outreach)


def _wire_value(doc, key: str):
	value = doc.get(apply.PATIENT_FIELDS[key])
	return cstr(value) if value not in (None, "") else None


def raise_visit_conflict(rec, patient: str, visits: list, review_only: bool, visit: str | None = None) -> str:
	"""Overlapping visit: `visits` are the facility visits involved (the open one, or the closed ones of the
	same date when `review_only`; `visit` is then the outreach visit just applied)."""
	facility = [_visit_version(v.name) for v in visits]
	p = rec.payload
	consult = p.get("consultation") or {}
	vitals = p.get("vitals") or {}
	outreach = {
		"visit": visit,
		"date": cstr(rec.captured_at),
		"department": p.get("department"),
		"status": _("Applied") if visit else _("Held"),
		"complaint": vitals.get("chief_complaint") or consult.get("presenting_complaint"),
		"diagnoses": [cstr(d.get("code")) for d in consult.get("diagnoses") or [] if isinstance(d, dict)],
		"outcome": p.get("outcome"),
	}
	if review_only:
		summary = _(
			"Overlapping visit (review): outreach visit {0} and facility visit {1} on the same day for {2}."
		).format(visit, ", ".join(v.name for v in visits), patient)
	else:
		summary = _("Overlapping visit: {0} has the open visit {1}; the outreach visit is held.").format(
			patient, visits[0].name
		)
	return _insert(
		rec, CONFLICT_OVERLAPPING_VISIT, patient, summary, facility, outreach, review_only=review_only
	)


def _visit_version(visit: str) -> dict:
	row = frappe.db.get_value(
		VISIT_DOCTYPE,
		visit,
		["name", "visit_date", "status", "current_department", "encounter", "outcome", "triage_vital_signs"],
		as_dict=True,
	)
	diagnoses, complaint = [], None
	if row.encounter:
		diagnoses = frappe.get_all(
			"EMR Encounter Diagnosis",
			filters={"parent": row.encounter, "parenttype": "Patient Encounter"},
			pluck="code",
			order_by="idx asc",
		)
		complaint = frappe.db.get_value("Patient Encounter", row.encounter, "krcs_presenting_complaint")
	if not complaint and row.triage_vital_signs:
		complaint = frappe.db.get_value("Vital Signs", row.triage_vital_signs, "krcs_chief_complaint")
	return {
		"visit": row.name,
		"date": cstr(row.visit_date),
		"department": row.current_department,
		"status": row.status,
		"complaint": complaint,
		"diagnoses": diagnoses,
		"outcome": row.outcome,
	}


def refresh_open_conflicts(patient: str | None) -> int:
	"""Patient.krcs_open_sync_conflicts = Open conflicts holding an outreach record of the patient."""
	if not patient or not frappe.db.exists("Patient", patient):
		return 0
	count = frappe.db.count(
		SYNC_CONFLICT_DOCTYPE, {"patient": patient, "status": CONFLICT_OPEN, "review_only": 0}
	)
	frappe.db.set_value("Patient", patient, PATIENT_OPEN_CONFLICTS_FIELD, count, update_modified=False)
	return count


# --------------------------------------------------------------------------------------------- resolving


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=RESOLVE_RATE_LIMIT, seconds=RATE_WINDOW, ip_based=False, user_based=True)
def resolve(
	conflict: str,
	decision: str,
	reason: str,
	chosen_fields: dict | str | None = None,
	patient: str | None = None,
) -> dict:
	"""Resolve an Open conflict with `decision` (valid for its type, see `DECISIONS`) and a reason.

	- `chosen_fields` (Merge): {field: "outreach" | "facility"}; fields not listed keep the facility value.
	- `patient` (duplicate registration): the existing patient the decision is about, one of the candidates
	  (default: the best match).
	"""
	check_resolver()
	frappe.db.savepoint(SAVEPOINT)
	try:
		result = _resolve(conflict, decision, reason, chosen_fields, patient)
	except Exception:
		frappe.db.rollback(save_point=SAVEPOINT)
		raise
	result["replayed"] = replay_held(conflict)
	return result


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=RESOLVE_RATE_LIMIT, seconds=RATE_WINDOW, ip_based=False, user_based=True)
def retry(conflict: str, reason: str | None = None) -> dict:
	"""Retry a held Overlapping visit (after the facility visit was closed): the outreach visit is applied
	and the conflict resolved (Keep outreach version). When it still fails, the conflict stays Open and the
	attempt is logged. Returns {"resolved": bool, "error": message or None, ...}."""
	check_resolver()
	doc = frappe.get_doc(SYNC_CONFLICT_DOCTYPE, conflict)
	if doc.conflict_type != CONFLICT_OVERLAPPING_VISIT or cint(doc.review_only):
		frappe.throw(_("Only a held overlapping visit can be retried."), SyncConflictError)
	reason = (reason or "").strip() or _(RETRY_REASON)
	log_start = len(frappe.local.message_log)
	frappe.db.savepoint(SAVEPOINT)
	try:
		result = _resolve(conflict, DECISION_KEEP_OUTREACH, reason, None, None)
	except (frappe.ValidationError, frappe.PermissionError) as exc:
		from onerc_healthcare.outreach.sync import readable_error

		message = readable_error(exc, log_start)
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.local.message_log = frappe.local.message_log[:log_start]
		doc = frappe.get_doc(SYNC_CONFLICT_DOCTYPE, conflict)
		_log(doc, "retry_failed", doc.status, doc.status, None, reason, message)
		_save(doc)
		return {"conflict": conflict, "resolved": False, "status": doc.status, "error": message}
	result["replayed"] = replay_held(conflict)
	result.update({"resolved": True, "error": None})
	return result


def check_resolver(user: str | None = None) -> None:
	user = user or frappe.session.user
	if user == "Administrator" or set(frappe.get_roles(user)) & set(CONFLICT_RESOLVER_ROLES):
		return
	frappe.throw(
		_("Only the {0} resolves sync conflicts.").format(
			_(" or the ").join(_(r) for r in CONFLICT_RESOLVER_ROLES)
		),
		frappe.PermissionError,
	)


def _resolve(conflict: str, decision: str, reason: str, chosen_fields, patient: str | None) -> dict:
	reason = (reason or "").strip()
	if not reason:
		frappe.throw(_("Give the reason for the decision; it is kept with the conflict."), SyncConflictError)
	if len(reason) > MAX_REASON_LENGTH:
		frappe.throw(
			_("The reason is limited to {0} characters.").format(MAX_REASON_LENGTH), SyncConflictError
		)
	doc = frappe.get_doc(SYNC_CONFLICT_DOCTYPE, conflict, for_update=True)
	frappe.has_permission(SYNC_CONFLICT_DOCTYPE, "write", doc=doc, throw=True)
	if doc.status != CONFLICT_OPEN:
		frappe.throw(
			_("Sync Conflict {0} was already resolved ({1}).").format(doc.name, _(doc.decision or "")),
			SyncConflictError,
			title=_("Already Resolved"),
		)
	allowed = decisions_for(doc)
	if decision not in allowed:
		frappe.throw(
			_("{0} is not a decision for this conflict. Choose one of: {1}.").format(
				decision, ", ".join(_(d) for d in allowed)
			),
			SyncConflictError,
		)
	chosen = _parse_json(chosen_fields) or {}
	if not isinstance(chosen, dict):
		frappe.throw(_("chosen_fields must map each field to 'outreach' or 'facility'."), SyncConflictError)

	record = frappe.get_doc(OUTREACH_RECORD_DOCTYPE, doc.sync_record, for_update=True)
	outcome = _apply_decision(doc, record, decision, reason, chosen, patient)
	if outcome is not None:
		_store_outcome(record, outcome)

	before = doc.status
	doc.status = CONFLICT_RESOLVED
	doc.decision = decision
	doc.reason = reason
	doc.resolved_by = frappe.session.user
	doc.resolved_at = now_datetime()
	doc.chosen_fields = json.dumps(chosen) if chosen else None
	doc.chosen_patient = outcome.get("chosen_patient") if outcome else None
	note = DECISION_EFFECTS[(doc.conflict_type, decision)][1]
	if outcome and outcome.get("note"):
		note = f"{note}. {outcome.note}"
	doc.result_note = note
	_log(doc, "resolve", before, CONFLICT_RESOLVED, decision, reason, note)
	_save(doc)
	for name in {doc.patient, doc.chosen_patient}:
		refresh_open_conflicts(name)
	return {
		"conflict": doc.name,
		"status": doc.status,
		"decision": decision,
		"record": record.name,
		"record_status": record.status,
		"doctype": record.target_doctype,
		"name": record.target_name,
	}


def _apply_decision(doc, record, decision: str, reason: str, chosen: dict, patient: str | None):
	"""Apply the decision to the conflicted record. Returns an `apply.Outcome` for it (None: unchanged)."""
	if decision == DECISION_ACKNOWLEDGE:
		return None
	if doc.conflict_type == CONFLICT_DUPLICATE_REGISTRATION:
		return _decide_registration(doc, record, decision, reason, chosen, patient)
	if doc.conflict_type == CONFLICT_DEMOGRAPHIC_CHANGE:
		if decision == DECISION_KEEP_FACILITY:
			return _discarded("Patient", doc.patient)
		resolution = {"force": True}
		if decision == DECISION_MERGE:
			resolution["fields"] = _outreach_fields(chosen)
		return _apply_as_member(record, resolution)
	# Overlapping visit (held)
	if decision == DECISION_KEEP_FACILITY:
		facility = _parse_json(doc.facility_version) or [{}]
		return _discarded(VISIT_DOCTYPE, facility[0].get("visit"), patient=doc.patient)
	return _apply_as_member(record, {"force": True})


def _decide_registration(doc, record, decision: str, reason: str, chosen: dict, patient: str | None):
	if decision == DECISION_KEEP_OUTREACH:
		override = _("Outreach sync conflict {0}: {1}").format(doc.name, reason)
		return _apply_as_member(record, {"force": True, "override_reason": override})
	candidates = [c.get("patient") for c in _parse_json(doc.candidates) or []]
	existing = (patient or "").strip() or doc.patient
	if existing not in candidates:
		frappe.throw(
			_("Choose the existing patient among the matches: {0}.").format(", ".join(candidates)),
			SyncConflictError,
		)
	if decision == DECISION_KEEP_FACILITY:
		out = _discarded("Patient", existing)
		out.chosen_patient = existing
		return out
	fields = _outreach_fields(chosen) if decision == DECISION_MERGE else []
	payload = _payload(record)
	changes = _registration_changes(payload, fields)
	identifiers, skipped = _new_identifiers(existing, payload.get("identifiers") or [])
	ctx = _member_context(record)
	with apply.acting_as(record.user), outreach_actor(record.user, ctx.session.name):
		if changes or identifiers:
			apply.apply_patient_changes(
				frappe.get_doc("Patient", existing, for_update=True), changes, identifiers
			)
	out = apply.accepted("Patient", existing, patient=existing)
	out.chosen_patient = existing
	notes = []
	if fields:
		notes.append(_("Taken from the outreach version: {0}").format(", ".join(fields)))
	if identifiers:
		notes.append(
			_("Identifiers added: {0}").format(
				", ".join(f"{i['identifier_type']}: {i['identifier_value']}" for i in identifiers)
			)
		)
	if skipped:
		notes.append(_("Identifiers not added: {0}").format("; ".join(skipped)))
	out.note = ". ".join(notes)
	return out


def _registration_changes(payload: dict, fields: list[str]) -> dict:
	"""Wire `changes` that write the chosen outreach registration fields to the existing patient."""
	changes = {}
	for field in fields:
		if field == "dob":
			if cint(payload.get("estimated_age_years")) or cint(payload.get("estimated_age_months")):
				changes["estimated_age_years"] = cint(payload.get("estimated_age_years"))
				changes["estimated_age_months"] = cint(payload.get("estimated_age_months"))
			else:
				changes["dob"] = payload.get("dob")
		else:
			changes[field] = payload.get(field)
	return changes


def _new_identifiers(patient: str, identifiers: list) -> tuple[list[dict], list[str]]:
	"""Outreach identifiers the existing patient can take: a type it does not hold yet, a value nobody else
	holds. Returns (rows to add, descriptions of those skipped)."""
	held_types = {
		r.identifier_type
		for r in frappe.get_all(
			"Patient Identifier",
			filters={"parent": patient, "parenttype": "Patient", "parentfield": PATIENT_IDENTIFIERS_FIELD},
			fields=["identifier_type"],
		)
	}
	add, skipped = [], []
	for row in identifiers:
		if not isinstance(row, dict):
			continue
		itype, value = cstr(row.get("identifier_type")).strip(), cstr(row.get("identifier_value")).strip()
		if not itype or not value:
			continue
		owner = find_identifier_owner(itype, value)
		if owner == patient:
			continue  # already on the record
		if itype in held_types:
			skipped.append(_("{0} {1} (the patient already has a {0})").format(itype, value))
		elif owner:
			skipped.append(_("{0} {1} (registered to {2})").format(itype, value, owner))
		else:
			add.append({"identifier_type": itype, "identifier_value": value})
			held_types.add(itype)
	return add, skipped


def _outreach_fields(chosen: dict) -> list[str]:
	fields = [f for f, side in chosen.items() if cstr(side).strip().lower() == "outreach"]
	unknown = sorted(set(chosen) - set(MERGE_FIELDS) - set(apply.UPDATE_KEYS))
	if unknown:
		frappe.throw(_("These fields cannot be merged: {0}.").format(", ".join(unknown)), SyncConflictError)
	if not fields:
		frappe.throw(
			_("Choose at least one field from the outreach version, or use Keep facility version."),
			SyncConflictError,
		)
	return fields


def _discarded(doctype: str | None, name: str | None, patient: str | None = None) -> apply.Outcome:
	return apply.Outcome(
		status=RECORD_STATUS_DISCARDED,
		doctype=doctype if name else None,
		name=name,
		patient=patient or (name if doctype == "Patient" else None),
	)


def _member_context(record, resolution: dict | None = None) -> frappe._dict:
	from onerc_healthcare.outreach.sync import record_context

	return record_context(record, apply.session_row(record.session), resolution)


def _apply_as_member(record, resolution: dict) -> apply.Outcome:
	"""Apply the conflicted record with the decision's `resolution`, as the member who captured it."""
	ctx = _member_context(record, resolution)
	with apply.acting_as(record.user):
		outcome = apply.apply_record(ctx)
	if outcome.status != RECORD_STATUS_ACCEPTED:
		frappe.throw(
			_("The outreach record could not be applied ({0}).").format(outcome.status), SyncConflictError
		)
	return outcome


def _store_outcome(record, outcome: apply.Outcome) -> None:
	"""The conflicted record's new status; its conflict link stays for the audit trail."""
	record.status = outcome.status
	record.error = None
	if outcome.get("doctype"):
		record.target_doctype = outcome.doctype
		record.target_name = outcome.name
	if outcome.get("patient"):
		record.patient = outcome.patient
	record.applied_at = now_datetime()
	from onerc_healthcare.outreach.sync import stored_result

	record.acknowledged_result = json.dumps(stored_result(record))
	record.save(ignore_permissions=True)  # server log; nobody has write on it


def replay_held(conflict: str) -> list[dict]:
	"""Re-apply the records held on `conflict`, in the order received, each as the member who sent it and in
	its own savepoint (`sync.process`). Returns their results."""
	from onerc_healthcare.outreach.sync import process

	names = frappe.get_all(
		OUTREACH_RECORD_DOCTYPE,
		filters={"conflict": conflict, "status": RECORD_STATUS_HELD},
		order_by="first_received_at asc, creation asc",
		pluck="name",
	)
	results = []
	sessions: dict[str, frappe._dict] = {}
	for name in names:
		record = frappe.get_doc(OUTREACH_RECORD_DOCTYPE, name)
		row = sessions.get(record.session) or apply.session_row(record.session)
		sessions[record.session] = row
		with apply.acting_as(record.user):
			results.append(process(record, row))
	if results:
		doc = frappe.get_doc(SYNC_CONFLICT_DOCTYPE, conflict)
		summary = ", ".join(f"{r['client_uuid']}: {r['result']}" for r in results)
		_log(
			doc, "replay", doc.status, doc.status, None, None, _("Held records replayed: {0}").format(summary)
		)
		_save(doc)
	return results


def _log(doc, action: str, from_status, to_status, decision, reason, note) -> None:
	doc.append(
		"log",
		{
			"action": action,
			"from_status": from_status or "",
			"to_status": to_status,
			"decision": decision,
			"user": frappe.session.user,
			"timestamp": now_datetime(),
			"reason": reason,
			"note": cstr(note)[:1000] or None,
		},
	)


def _save(doc) -> None:
	doc.flags.krcs_via_conflicts = True
	try:
		doc.save(ignore_permissions=True)  # the caller's role was checked (check_resolver / has_permission)
	finally:
		doc.flags.krcs_via_conflicts = False


def _payload(record) -> dict:
	return _parse_json(record.payload) or {}


def _parse_json(value):
	if value in (None, ""):
		return None
	if isinstance(value, str):
		try:
			return json.loads(value)
		except ValueError:
			return None
	return value


# --------------------------------------------------------------------------------------------- desk form


def form_context(doc) -> dict:
	"""What the Sync Conflict form shows: the valid decisions, the side-by-side rows and the candidates
	(the versions only for users who may read them, permlevel 1)."""
	user = frappe.session.user
	may_resolve = user == "Administrator" or bool(set(frappe.get_roles(user)) & set(CONFLICT_RESOLVER_ROLES))
	is_open = doc.status == CONFLICT_OPEN
	out = {
		"decisions": list(decisions_for(doc)) if is_open and may_resolve else [],
		"can_retry": is_open
		and may_resolve
		and doc.conflict_type == CONFLICT_OVERLAPPING_VISIT
		and not cint(doc.review_only),
		"merge_fields": [],
		"rows": [],
		"candidates": [],
		"needs_patient": list(NEEDS_EXISTING_PATIENT)
		if doc.conflict_type == CONFLICT_DUPLICATE_REGISTRATION
		else [],
	}
	if 1 not in doc.get_permlevel_access("read") and user != "Administrator":
		return out
	facility, outreach = _parse_json(doc.facility_version), _parse_json(doc.outreach_version)
	if doc.conflict_type == CONFLICT_OVERLAPPING_VISIT:
		facility_row = (facility or [{}])[0]
		out["rows"] = [
			_row(key, label, facility_row.get(key), (outreach or {}).get(key)) for key, label in VISIT_COMPARE
		]
		return out
	facility, outreach = facility or {}, outreach or {}
	if doc.conflict_type == CONFLICT_DEMOGRAPHIC_CHANGE:
		keys = [k for k in outreach if k in apply.UPDATE_KEYS]
		out["rows"] = [_row(k, k.replace("_", " ").title(), facility.get(k), outreach.get(k)) for k in keys]
		out["merge_fields"] = keys
		out["rows"].append(
			_row("modified", "Last Changed", facility.get("modified"), outreach.get("base_modified"))
		)
		return out
	out["rows"] = [
		_row(key, label, _display(facility, key), _display(outreach, key)) for key, label in PATIENT_COMPARE
	]
	out["merge_fields"] = list(MERGE_FIELDS)
	out["candidates"] = [
		{
			"patient": c.get("patient"),
			"patient_name": c.get("patient_name"),
			"score": c.get("score"),
			"matched_by": c.get("matched_by"),
			"reasons": c.get("reasons"),
		}
		for c in _parse_json(doc.candidates) or []
	]
	return out


def _display(version: dict, key: str):
	value = version.get(key)
	if key == "dob" and value:
		if cint(version.get("dob_estimated")):
			years = version.get("estimated_age_years")
			return _("{0} (estimated{1})").format(value, f", about {years} years" if years else "")
		return value
	if key == "identifiers":
		return ", ".join(value or [])
	return value


def _row(key: str, label: str, facility, outreach) -> dict:
	def text(value):
		if isinstance(value, list | tuple):
			return ", ".join(cstr(v) for v in value)
		return cstr(value) if value not in (None, "") else ""

	f, o = text(facility), text(outreach)
	return {"field": key, "label": _(label), "facility": f, "outreach": o, "differs": f != o}
