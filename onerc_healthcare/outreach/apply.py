# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Apply one offline outreach record through the same pipelines as the desk (Phase 4, O2; SDD §5.6, FR-24/25).

`outreach.sync` calls `apply_record` inside a savepoint, after it has checked the team membership, the
session, the envelope and the dependencies. Everything here runs as the team member (``frappe.session.user``)
inside `outreach.context.outreach_actor`, so the member is the author of every record (owner, visit status log,
practitioner, dispenser) and the clinical role gates (`clinical.utils.has_any_role`) accept them.

**Same validation as the desk.** Records are inserted and submitted through their controllers and hooks
(registration rules, visit controller and engine, Vital Signs plausibility, encounter guards and the diagnosis
rule, `lab.orders.ensure_observations`, the Observation and Dispensing Record controllers). The desk *endpoints*
(`clinical.consultation.*`, `lab.guards.set_observation_status`, Marley's sample collection) also check
DocPerms that the Outreach Team Member deliberately does not have (D58: no clinical DocPerms, because
``desk_access=0`` does not block ``/api/resource``). Those endpoints are therefore not called; their steps are
run here in the same order with the same building blocks, and only the DocPerm check of the specific
insert / save / submit is skipped (``ignore_permissions``; every place is marked ``# DocPerm:``):

| Step | Desk endpoint | Here |
|---|---|---|
| register | Patient insert (Receptionist) | `Patient.insert` with its hooks; DocPerm skipped |
| visit | Patient Visit insert (Receptionist), ``flags.from_workflow`` for back-dating | same, DocPerm skipped |
| vitals | Vital Signs insert + submit (Triage Nurse) -> ``start_triage`` | same, DocPerm skipped |
| route | ``engine.transition(route)`` | ``engine.transition(route, system=True)`` |
| consult | `consultation.start_consultation` | ``engine.transition(start_consultation, system=True)``, encounter insert (DocPerm skipped), ``engine.link_encounter`` |
| lab orders | `consultation.send_lab_orders` | ``make_service_request``, save (DocPerm skipped), ``ensure_observations``, ``raise_event(request_lab)`` |
| POC result | Marley sample collection, result entry, `lab.guards.set_observation_status` | Specimen + Marley ``new_observation``, result save, visit lock then Observation submit, run as the system because Marley's ``Observation.on_update`` lists Observations with the user's read permission; the member is then set as owner (`_record_poc`) |
| complete | `consultation.complete_consultation` | `consultation.submit_encounter` (DocPerm skipped), ``raise_event(prescribe)``, ``engine.transition(complete_consultation, system=True)`` |
| dispense | Dispensing Record insert + submit (Pharmacist) | same, DocPerm skipped; ``dispensed_at`` back-dated |
| activity | Community Activity Record insert + submit | same, with the member's own DocPerm (no skip) |

**Time.** ``captured_at`` is the clinical time: the visit date / arrival, the vital signs, the encounter date and
the visit's stage times (triage, routing, consultation start, outcome and close) are set to it. POC results use
their ``resulted_at`` (collection, result and approval time) and dispenses their ``dispensed_at``. The visit
status log keeps the server time of each action (it is the audit trail of when the system applied it).

**Patients a session may touch** (protocol §6 F5, `assert_session_patient`). A ``patient.update``, and a
``visit.capture`` naming a server ``patient``, are applied only for a patient whose ``krcs_residence`` lies in
the session's catchment subtrees (`outreach.package.patient_in_catchment`), or who was registered, linked or
already updated / visited through an applied record of this session (an Accepted or Discarded Outreach Sync
Record of the session for that patient: a patient.register, including a Link / Keep facility decision). Any
other patient: "This patient is not in the session's catchment area." A HIS decision (`rec.resolution`) is not
checked again (the record passed the check when it was received).

**`patient.update`** (§6 F4). ``base_modified`` is required and may not be later than the patient's current
``modified`` (failed). When the patient changed after ``base_modified``, a changed field conflicts only when the
facility changed it too: its server value differs from the ``base_values`` value (as downloaded) and from the
device's value. Without ``base_values`` (or for a key missing from it) the older rule applies: the server value
differs from the device's value. An applied patient.update (and patient.register) returns the patient's new
``modified`` (``Outcome.modified``), which the sync stores and sends back.

**Field dispensing** (§6 F3). The Dispensing Record of a line dispensed in the field is ``final``: a partial
quantity closes the line (Closed), so the facility pharmacy worklist does not offer it again; over-dispensing is
refused by the Dispensing Record controller as at the desk.

**Death.** "Died" needs a ``DIED_ROLES`` role at the desk. D58 lets any team member consult, so inside the
outreach context the member may record it (`clinical.utils.has_any_role(DIED_ROLES)` accepts them); the engine
call is then made as a system call.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate

from onerc_healthcare.clinical import consultation, diagnosis, practitioner
from onerc_healthcare.clinical.utils import has_any_role, lock_visit_and_encounter
from onerc_healthcare.constants import (
	COMMUNITY_ACTIVITY_DOCTYPE,
	CONFLICT_DEMOGRAPHIC_CHANGE,
	CONFLICT_DUPLICATE_REGISTRATION,
	CONFLICT_OVERLAPPING_VISIT,
	DEFAULT_APPOINTMENT_TYPE,
	DIED_ROLES,
	DISPENSING_DOCTYPE,
	ENCOUNTER_DOCTYPE,
	ENCOUNTER_VISIT_FIELD,
	OUTCOME_DIED,
	OUTCOME_DISCHARGED,
	OUTREACH_DEFAULTS,
	OUTREACH_DEPARTMENT_DOCTYPE,
	OUTREACH_EXCLUDED_DEPARTMENTS,
	OUTREACH_LAB_PENDING_REASON,
	OUTREACH_NO_VITALS_REASON,
	OUTREACH_SESSION_DOCTYPE,
	PATIENT_DOB_ESTIMATED_FIELD,
	PATIENT_DOB_ESTIMATED_ON_FIELD,
	PATIENT_ESTIMATED_AGE_MONTHS_FIELD,
	PATIENT_ESTIMATED_AGE_YEARS_FIELD,
	PATIENT_IDENTIFIERS_FIELD,
	PATIENT_WALK_IN_FIELD,
	POINT_OF_CARE_FIELD,
	RECORD_COMMUNITY_ACTIVITY,
	RECORD_PATIENT_REGISTER,
	RECORD_PATIENT_UPDATE,
	RECORD_STATUS_ACCEPTED,
	RECORD_STATUS_CONFLICT,
	RECORD_VISIT_CAPTURE,
	VISIT_CLOSED_STATUSES,
	VISIT_DOCTYPE,
	VISIT_STATUS_REGISTERED,
)
from onerc_healthcare.outreach.context import outreach_actor
from onerc_healthcare.registration.identifiers import (
	OVERRIDE_REASON_FIELD,
	REGISTRATION_FACILITY_FIELD,
	RESIDENCE_FIELD,
	find_identifier_owner,
	find_strong_duplicates,
)
from onerc_healthcare.visit import engine, tasks

# Wire field -> Patient field (patient.register / patient.update payloads, protocol §4).
PATIENT_FIELDS = {
	"first_name": "first_name",
	"middle_name": "middle_name",
	"last_name": "last_name",
	"sex": "sex",
	"dob": "dob",
	"estimated_age_years": PATIENT_ESTIMATED_AGE_YEARS_FIELD,
	"estimated_age_months": PATIENT_ESTIMATED_AGE_MONTHS_FIELD,
	"mobile": "mobile",
	"residence": RESIDENCE_FIELD,
	"allergies": "allergies",
}
UPDATE_KEYS = tuple(PATIENT_FIELDS)
# Wire vitals key -> Vital Signs field
VITAL_FIELDS = {
	"temperature": "temperature",
	"pulse": "pulse",
	"respiratory_rate": "respiratory_rate",
	"bp_systolic": "bp_systolic",
	"bp_diastolic": "bp_diastolic",
	"weight": "weight",
	"height": "height",
	"spo2": "krcs_spo2",
}
OUTREACH_OUTCOMES = (OUTCOME_DISCHARGED, OUTCOME_DIED)  # protocol §4 visit.capture
STAGE_TIMES = ("triage_started_at", "routed_at", "consultation_started_at")
SAMPLE_COLLECTION = "Sample Collection"
SAMPLE_ROW = "Observation Sample Collection"


class OutreachRecordError(frappe.ValidationError):
	"""The offline record cannot be applied as sent (the device shows the message)."""


class Outcome(frappe._dict):
	"""What applying a record produced: status (Outreach Sync Record status), doctype / name of the record
	created, patient, conflict (Sync Conflict name), error."""


def accepted(doctype: str, name: str, patient: str | None = None, conflict: str | None = None) -> Outcome:
	return Outcome(
		status=RECORD_STATUS_ACCEPTED, doctype=doctype, name=name, patient=patient, conflict=conflict
	)


NOT_IN_CATCHMENT = "This patient is not in the session's catchment area."


def in_conflict(conflict: str, patient: str | None = None) -> Outcome:
	return Outcome(status=RECORD_STATUS_CONFLICT, conflict=conflict, patient=patient)


def fail(message: str) -> None:
	frappe.throw(message, OutreachRecordError, title=_("Record Not Applied"))


# --------------------------------------------------------------------------------------------- dispatch


def apply_record(rec: frappe._dict) -> Outcome:
	"""Apply `rec` as the team member. `rec`: name (client UUID), record_type, payload (dict), captured_at
	(datetime), session (session row with ``departments``), user, depends_on (list), resolution (dict or None:
	set by `outreach.conflicts` when a decision applies the record)."""
	handler = APPLIERS.get(rec.record_type)
	if not handler:
		fail(_("Unknown record type {0}.").format(rec.record_type))
	if not isinstance(rec.payload, dict):
		fail(_("The record has no payload."))
	with outreach_actor(rec.user, rec.session.name):
		return handler(rec)


def session_row(session: str) -> frappe._dict:
	"""The fields of an Outreach Session the appliers need, plus its departments (never Mental Health)."""
	row = frappe.db.get_value(
		OUTREACH_SESSION_DOCTYPE,
		session,
		["name", "status", "facility", "company", "start_date", "end_date", "catchment_area"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("Outreach Session {0} not found.").format(session), frappe.DoesNotExistError)
	row.departments = session_departments(session)
	return row


def session_departments(session: str) -> list[str]:
	rows = frappe.get_all(
		OUTREACH_DEPARTMENT_DOCTYPE,
		filters={"parent": session, "parenttype": OUTREACH_SESSION_DOCTYPE},
		pluck="department",
		order_by="idx asc",
	)
	return [d for d in rows if d and d not in OUTREACH_EXCLUDED_DEPARTMENTS]


def get_setting(key: str) -> int:
	"""EMR Outreach Settings value (O1's `get_settings`, defaults filled in from OUTREACH_DEFAULTS)."""
	from onerc_healthcare.emr_outreach.doctype.emr_outreach_settings.emr_outreach_settings import get_settings

	return cint(get_settings().get(key)) or OUTREACH_DEFAULTS[key]


# --------------------------------------------------------------------------------------------- users


@contextmanager
def acting_as(user: str):
	"""Run the body as `user` (``frappe.session.user``) and restore the caller afterwards.

	Used by `outreach.conflicts` to replay held records as the team member who captured them (the HIS Officer
	resolves; the member stays the author). Only the user and the per-request permission caches are switched;
	unlike ``frappe.set_user`` the request's session data and form dict are left alone."""
	previous = frappe.session.user
	if previous == user:
		yield
		return
	_switch_user(user)
	try:
		yield
	finally:
		_switch_user(previous)


def _switch_user(user: str) -> None:
	frappe.session.user = user
	frappe.local.role_permissions = {}
	frappe.local.user_perms = None
	frappe.local.new_doc_templates = {}


@contextmanager
def system_read():
	"""A read-only lookup as Administrator. `registration.similarity.find_similar` filters its candidates with
	``frappe.get_list("Patient")`` (row-level access), which refuses the Outreach Team Member (no Patient DocPerm,
	D58). The duplicate check of an offline registration is a system check over every patient, so it runs here.
	Nothing is written inside this block."""
	with acting_as("Administrator"):
		yield


# --------------------------------------------------------------------------------------------- helpers


def _text(value) -> str | None:
	value = cstr(value).strip() if value is not None else ""
	return value or None


def _captured_date(rec):
	return getdate(rec.captured_at)


def _check_time(value, label: str, rec, not_before: datetime | None = None) -> datetime:
	"""A device time (`resulted_at`, `dispensed_at`): parseable, not before `not_before` (default the capture),
	not more than the allowed skew in the future."""
	from onerc_healthcare.outreach.sync import FUTURE_TOLERANCE, parse_device_time

	when = parse_device_time(value)
	if not when:
		fail(_("{0} must be a date and time (YYYY-MM-DD HH:mm:ss).").format(label))
	floor = not_before or rec.captured_at
	if when < floor:
		fail(_("{0} ({1}) is before the visit was captured ({2}).").format(label, when, floor))
	if when > frappe.utils.now_datetime() + FUTURE_TOLERANCE:
		fail(_("{0} ({1}) is in the future.").format(label, when))
	return when


def _patient_from_ref(rec) -> str:
	"""`patient` (a server name) or `patient_ref` (the UUID of a patient.register this record depends on)."""
	p = rec.payload
	patient, ref = _text(p.get("patient")), _text(p.get("patient_ref"))
	if bool(patient) == bool(ref):
		fail(_("Give exactly one of patient and patient_ref."))
	if patient:
		if not frappe.db.exists("Patient", patient):
			fail(_("Patient {0} does not exist on the server.").format(patient))
		assert_session_patient(rec, patient)
		return patient
	ref = ref.lower()
	if ref not in (rec.depends_on or []):
		fail(_("patient_ref {0} must also be listed in depends_on.").format(ref))
	from onerc_healthcare.constants import OUTREACH_RECORD_DOCTYPE, RECORD_STATUS_DISCARDED

	dep = frappe.db.get_value(
		OUTREACH_RECORD_DOCTYPE, ref, ["status", "record_type", "target_doctype", "target_name"], as_dict=True
	)
	if not dep or dep.record_type != RECORD_PATIENT_REGISTER:
		fail(_("patient_ref {0} is not a patient registration received by the server.").format(ref))
	if dep.status not in (RECORD_STATUS_ACCEPTED, RECORD_STATUS_DISCARDED) or dep.target_doctype != "Patient":
		fail(_("The registration {0} this visit needs has not been applied.").format(ref))
	return dep.target_name


def session_linked_patient(session: str, patient: str) -> bool:
	"""`patient` was registered, linked or already handled through an applied record of `session`."""
	from onerc_healthcare.constants import OUTREACH_RECORD_DOCTYPE, RECORD_STATUS_DISCARDED

	return bool(
		frappe.db.exists(
			OUTREACH_RECORD_DOCTYPE,
			{
				"session": session,
				"patient": patient,
				"status": ["in", [RECORD_STATUS_ACCEPTED, RECORD_STATUS_DISCARDED]],
			},
		)
	)


def assert_session_patient(rec, patient: str) -> None:
	"""Protocol §6 F5: the session may touch only its catchment's patients and those it registered or linked.
	Not checked again when a HIS decision applies the record."""
	if rec.resolution:
		return
	from onerc_healthcare.outreach.package import patient_in_catchment

	if patient_in_catchment(patient, rec.session.catchment_area):
		return
	if session_linked_patient(rec.session.name, patient):
		return
	fail(_(NOT_IN_CATCHMENT))


# ------------------------------------------------------------------------------------- patient.register


def build_patient(payload: dict, rec, override_reason: str | None = None):
	"""A new (unsaved) Patient from a patient.register payload; the registration facility is the session's.
	An estimated age is estimated as of the capture date (registration/dob.py)."""
	doc = frappe.new_doc("Patient")
	for key in ("first_name", "middle_name", "last_name", "sex", "mobile", "allergies"):
		doc.set(PATIENT_FIELDS[key], _text(payload.get(key)))
	doc.set(RESIDENCE_FIELD, _text(payload.get("residence")))
	years, months = payload.get("estimated_age_years"), payload.get("estimated_age_months")
	if _text(payload.get("dob")):
		doc.dob = getdate(payload.get("dob"))
	if cint(years) or cint(months):
		if _text(payload.get("dob")):
			fail(_("Give either the date of birth or the estimated age, not both."))
		doc.set(PATIENT_ESTIMATED_AGE_YEARS_FIELD, cint(years))
		doc.set(PATIENT_ESTIMATED_AGE_MONTHS_FIELD, cint(months))
		doc.set(PATIENT_DOB_ESTIMATED_FIELD, 1)
		doc.set(PATIENT_DOB_ESTIMATED_ON_FIELD, _captured_date(rec))
	doc.set(PATIENT_WALK_IN_FIELD, cint(payload.get("is_walk_in")))
	doc.set(REGISTRATION_FACILITY_FIELD, rec.session.facility)
	identifiers = payload.get("identifiers") or []
	if not isinstance(identifiers, list):
		fail(_("identifiers must be a list."))
	for row in identifiers:
		if not isinstance(row, dict):
			fail(_("Each identifier needs identifier_type and identifier_value."))
		doc.append(
			PATIENT_IDENTIFIERS_FIELD,
			{
				"identifier_type": _text(row.get("identifier_type")),
				"identifier_value": _text(row.get("identifier_value")),
			},
		)
	if override_reason:
		doc.set(OVERRIDE_REASON_FIELD, override_reason)
	return doc


def registration_candidates(doc) -> list[dict]:
	"""Existing patients the new registration may duplicate, best first: an identifier already registered, a
	strong duplicate (same name, DOB and sex), or a similarity score of at least ``conflict_score``."""
	from onerc_healthcare.registration.dob import apply_dob_rules
	from onerc_healthcare.registration.similarity import find_similar

	apply_dob_rules(doc)  # the final DOB (estimate) for the checks; the insert applies it again (idempotent)
	found: dict[str, dict] = {}

	def add(patient: str, matched_by: str, score: int | None = None, reasons: list | None = None) -> None:
		row = found.setdefault(patient, {"patient": patient, "matched_by": [], "score": None, "reasons": []})
		if matched_by not in row["matched_by"]:
			row["matched_by"].append(matched_by)
		if score is not None and (row["score"] is None or score > row["score"]):
			row["score"] = score
		for reason in reasons or []:
			if reason not in row["reasons"]:
				row["reasons"].append(reason)

	for row in doc.get(PATIENT_IDENTIFIERS_FIELD) or []:
		owner = find_identifier_owner(row.identifier_type, row.identifier_value)
		if owner:
			add(owner, "identifier", reasons=[_("same {0}").format(row.identifier_type)])
	for patient in find_strong_duplicates(doc):
		add(patient, "strong duplicate", reasons=[_("same name, date of birth and sex")])
	if doc.first_name:
		threshold = get_setting("conflict_score")
		with system_read():
			similar = find_similar(
				doc.first_name,
				doc.last_name,
				doc.sex,
				doc.dob,
				doc.get(RESIDENCE_FIELD),
				dob_estimated=bool(cint(doc.get(PATIENT_DOB_ESTIMATED_FIELD))),
			)
		for row in similar:
			if row["score"] >= threshold or row["name"] in found:
				add(row["name"], "similarity", row["score"], row.get("reasons"))
	rows = list(found.values())
	for row in rows:
		row["patient_name"] = frappe.db.get_value("Patient", row["patient"], "patient_name")
		row["reasons"] = [cstr(r) for r in row["reasons"]]
	order = {"identifier": 0, "strong duplicate": 1, "similarity": 2}
	rows.sort(key=lambda r: (min(order[m] for m in r["matched_by"]), -(r["score"] or 0)))
	return rows


def apply_patient_register(rec) -> Outcome:
	"""Register the patient, or raise a Duplicate registration conflict (protocol §4)."""
	resolution = rec.resolution or {}
	doc = build_patient(rec.payload, rec, override_reason=resolution.get("override_reason"))
	if not resolution.get("force"):
		candidates = registration_candidates(doc)
		if candidates:
			from onerc_healthcare.outreach import conflicts

			name = conflicts.raise_registration_conflict(rec, doc, candidates)
			return in_conflict(name, patient=None)
	doc.insert(ignore_permissions=True)  # DocPerm: Patient create is the Receptionist's (D58)
	out = accepted("Patient", doc.name, patient=doc.name)
	out.modified = frappe.db.get_value("Patient", doc.name, "modified")
	return out


# --------------------------------------------------------------------------------------- patient.update


def comparable(fieldname: str, value):
	"""Value of a Patient field in a form both sides can be compared in."""
	if value in (None, ""):
		return None
	if fieldname == "dob":
		return str(getdate(value))
	if fieldname in (PATIENT_ESTIMATED_AGE_YEARS_FIELD, PATIENT_ESTIMATED_AGE_MONTHS_FIELD):
		return cint(value) or None
	return cstr(value).strip() or None


def update_changes(payload: dict) -> dict:
	changes = payload.get("changes") or {}
	if not isinstance(changes, dict):
		fail(_("changes must be an object."))
	unknown = sorted(set(changes) - set(UPDATE_KEYS))
	if unknown:
		fail(_("These patient fields cannot be changed offline: {0}.").format(", ".join(unknown)))
	return changes


def apply_patient_changes(doc, changes: dict, add_identifiers: list | None = None) -> None:
	"""Set the wire `changes` (and new identifiers) on Patient `doc` and save it through its hooks."""
	for key, value in changes.items():
		field = PATIENT_FIELDS[key]
		if field == "dob":
			doc.dob = getdate(value) if _text(value) else None
		elif field in (PATIENT_ESTIMATED_AGE_YEARS_FIELD, PATIENT_ESTIMATED_AGE_MONTHS_FIELD):
			doc.set(field, cint(value))
		else:
			doc.set(field, _text(value))
	for row in add_identifiers or []:
		if not isinstance(row, dict):
			fail(_("Each identifier needs identifier_type and identifier_value."))
		doc.append(
			PATIENT_IDENTIFIERS_FIELD,
			{
				"identifier_type": _text(row.get("identifier_type")),
				"identifier_value": _text(row.get("identifier_value")),
			},
		)
	doc.save(ignore_permissions=True)  # DocPerm: Patient write is the Receptionist's (D58)


def _base_modified(p: dict):
	value = _text(p.get("base_modified"))
	if not value:
		return None
	try:
		return get_datetime(value)
	except Exception:
		fail(_("base_modified {0} is not a date and time.").format(value))


def conflicting_fields(doc, changes: dict, base_values: dict | None) -> list[str]:
	"""The changed fields the facility changed too (protocol §6 F4). With a base value: the server value differs
	from it and from the device value. Without one (no ``base_values``, or the key is missing): the server value
	differs from the device value (the older rule)."""
	out = []
	for key, value in changes.items():
		field = PATIENT_FIELDS[key]
		server = comparable(field, doc.get(field))
		if server == comparable(field, value):
			continue
		if base_values is not None and key in base_values and server == comparable(field, base_values[key]):
			continue  # unchanged at the facility since the package: the device's edit wins
		out.append(key)
	return out


def apply_patient_update(rec) -> Outcome:
	"""Apply demographic changes, or raise a Demographic change conflict when the server copy changed after the
	package (``modified`` later than ``base_modified``) and the facility changed a field the device changed too
	(`conflicting_fields`, protocol §6 F4)."""
	p = rec.payload
	patient = _text(p.get("patient"))
	if not patient or not frappe.db.exists("Patient", patient):
		fail(_("Patient {0} does not exist on the server.").format(patient or ""))
	assert_session_patient(rec, patient)
	changes = update_changes(p)
	add_identifiers = p.get("add_identifiers") or []
	if not isinstance(add_identifiers, list):
		fail(_("add_identifiers must be a list."))
	base_values = p.get("base_values")
	if base_values is not None and not isinstance(base_values, dict):
		fail(_("base_values must be an object."))
	resolution = rec.resolution or {}
	if (
		"fields" in resolution
	):  # a Merge decision: only the fields the reviewer took from the outreach version
		changes = {k: v for k, v in changes.items() if k in resolution["fields"]}
	doc = frappe.get_doc("Patient", patient, for_update=True)
	if not resolution.get("force"):
		base = _base_modified(p)
		if not base:
			fail(_("base_modified (the package version of the patient) is required."))
		current = get_datetime(doc.modified)
		if base > current:
			fail(
				_(
					"base_modified {0} is later than the last change of patient {1} on the server ({2}). "
					"Download the patient list again."
				).format(base, patient, current)
			)
		if current > base:
			differing = conflicting_fields(doc, changes, base_values)
			if differing:
				from onerc_healthcare.outreach import conflicts

				name = conflicts.raise_update_conflict(rec, doc, changes, differing)
				return in_conflict(name, patient=patient)
	apply_patient_changes(doc, changes, add_identifiers)
	out = accepted("Patient", doc.name, patient=doc.name)
	out.modified = frappe.db.get_value("Patient", doc.name, "modified")
	return out


# ---------------------------------------------------------------------------------------- visit.capture


def check_department(rec, department: str | None) -> str:
	department = _text(department)
	if not department:
		fail(_("Choose the department that saw the patient."))
	if department in OUTREACH_EXCLUDED_DEPARTMENTS:
		fail(_("{0} is never offered by an outreach team (OI-7).").format(department))
	if department not in rec.session.departments:
		fail(
			_("{0} is not offered by outreach session {1} (offered: {2}).").format(
				department, rec.session.name, ", ".join(rec.session.departments) or _("none")
			)
		)
	return department


def _check_visit_payload(rec) -> frappe._dict:
	"""Shape checks of a visit.capture payload that the desk forms would enforce before any record exists."""
	p = rec.payload
	consult = p.get("consultation")
	if not isinstance(consult, dict):
		fail(_("A visit needs its consultation (notes and diagnoses, or the reason there is none)."))
	vitals = p.get("vitals")
	if vitals is not None and not isinstance(vitals, dict):
		fail(_("vitals must be an object."))
	labs = p.get("lab_requests") or []
	drugs = p.get("prescriptions") or []
	if not isinstance(labs, list) or not isinstance(drugs, list):
		fail(_("lab_requests and prescriptions must be lists."))
	diagnoses = consult.get("diagnoses") or []
	if not isinstance(diagnoses, list):
		fail(_("diagnoses must be a list."))
	outcome = _text(p.get("outcome")) or OUTCOME_DISCHARGED
	if outcome not in OUTREACH_OUTCOMES:
		fail(_("Outcome must be one of: {0}.").format(", ".join(OUTREACH_OUTCOMES)))
	if outcome == OUTCOME_DIED and not has_any_role(DIED_ROLES):
		fail(_("You are not allowed to record that a patient died."))
	for line in labs:
		if not isinstance(line, dict) or not _text(line.get("observation_template")):
			fail(_("Each laboratory request needs its observation_template."))
		template = line["observation_template"]
		if not frappe.db.exists("Observation Template", template):
			fail(_("Laboratory test {0} does not exist.").format(template))
		poc = line.get("poc_result")
		if poc:
			if not cint(frappe.db.get_value("Observation Template", template, POINT_OF_CARE_FIELD)):
				fail(
					_("{0} is not a point-of-care test; it must go to the facility laboratory.").format(
						template
					)
				)
			if not isinstance(poc, dict) or not _text(poc.get("result")):
				fail(_("Enter the point-of-care result of {0}.").format(template))
			_check_poc_value(template, poc.get("result"))
			poc["_resulted_at"] = _check_time(
				poc.get("resulted_at"), _("Result time of {0}").format(template), rec
			)
	for line in drugs:
		if not isinstance(line, dict) or not _text(line.get("medication")):
			fail(_("Each prescription needs its medication."))
		if not frappe.db.exists("Medication", line["medication"]):
			fail(_("Medication {0} does not exist.").format(line["medication"]))
		dispensed = line.get("dispensed")
		if dispensed:
			if not isinstance(dispensed, dict) or flt(dispensed.get("quantity")) <= 0:
				fail(_("Enter the quantity dispensed of {0} (more than 0).").format(line["medication"]))
			dispensed["_dispensed_at"] = _check_time(
				dispensed.get("dispensed_at"), _("Dispensing time of {0}").format(line["medication"]), rec
			)
	return frappe._dict(consult=consult, vitals=vitals, labs=labs, drugs=drugs, outcome=outcome)


def _check_poc_value(template: str, value) -> None:
	"""A Select result must be one of the template's options (the desk widget only offers those)."""
	row = frappe.db.get_value(
		"Observation Template", template, ["permitted_data_type", "options"], as_dict=True
	)
	if row and row.permitted_data_type == "Select" and row.options:
		options = [o.strip() for o in row.options.split("\n") if o.strip()]
		if cstr(value).strip() not in options:
			fail(
				_("{0} is not a result of {1}. Choose one of: {2}.").format(
					value, template, ", ".join(options)
				)
			)


def open_visit_of(patient: str) -> frappe._dict | None:
	"""The patient's open visit (any date), read after the Patient row lock (as the visit controller does)."""
	from onerc_healthcare.emr_visit.doctype.patient_visit.patient_visit import find_open_visit
	from onerc_healthcare.visit.patients import lock_patient

	lock_patient(patient)
	return find_open_visit(patient, for_update=True)


def closed_facility_visits_on(patient: str, day) -> list[frappe._dict]:
	"""Closed facility (non-outreach) visits of the patient on `day`."""
	return frappe.get_all(
		VISIT_DOCTYPE,
		filters={
			"patient": patient,
			"visit_date": getdate(day),
			"status": ["in", list(VISIT_CLOSED_STATUSES)],
			"is_outreach": 0,
		},
		fields=[
			"name",
			"status",
			"visit_date",
			"facility",
			"current_department",
			"closure_reason",
			"outcome",
		],
		order_by="creation asc",
	)


def apply_visit_capture(rec) -> Outcome:
	"""One finished outreach visit (protocol §4 server steps 1-8), or an Overlapping visit conflict."""
	from onerc_healthcare.visit.patients import assert_patient_active

	resolution = rec.resolution or {}
	patient = _patient_from_ref(rec)
	department = check_department(rec, rec.payload.get("department"))
	parts = _check_visit_payload(rec)
	assert_patient_active(patient)
	existing = open_visit_of(patient)
	if existing:
		if resolution.get("force"):
			fail(
				_(
					"Patient {0} still has the open visit {1} ({2}). Close that visit at the facility, then retry."
				).format(patient, existing.name, _(existing.status))
			)
		from onerc_healthcare.outreach import conflicts

		name = conflicts.raise_visit_conflict(rec, patient, [existing], review_only=False)
		return in_conflict(name, patient=patient)
	same_day = closed_facility_visits_on(patient, rec.captured_at)

	visit = _insert_visit(rec, patient)
	_triage(rec, visit, patient, parts.vitals, department)
	encounter = _consult(rec, visit, patient, department, parts)
	review = None
	if same_day:
		from onerc_healthcare.outreach import conflicts

		review = conflicts.raise_visit_conflict(rec, patient, same_day, review_only=True, visit=visit)
	out = accepted(VISIT_DOCTYPE, visit, patient=patient, conflict=review)
	out.encounter = encounter
	return out


def _insert_visit(rec, patient: str) -> str:
	doc = frappe.get_doc(
		{
			"doctype": VISIT_DOCTYPE,
			"patient": patient,
			"facility": rec.session.facility,
			"is_outreach": 1,
			"outreach_session": rec.session.name,
			"visit_date": _captured_date(rec),
			"arrived_at": rec.captured_at,
		}
	)
	doc.flags.from_workflow = True  # keeps the supplied visit date and arrival time (patient_visit.py)
	try:
		doc.insert(ignore_permissions=True)  # DocPerm: Patient Visit create is the Receptionist's (D58)
	finally:
		doc.flags.from_workflow = False
	return doc.name


def _triage(rec, visit: str, patient: str, vitals: dict | None, department: str) -> None:
	"""Submit the vital signs (which starts triage) or start triage, then route (FR-9)."""
	if vitals:
		values = {
			"doctype": "Vital Signs",
			"patient": patient,
			"krcs_visit": visit,
			"signs_date": _captured_date(rec),
			"signs_time": rec.captured_at.strftime("%H:%M:%S"),
			"krcs_triage_level": _text(vitals.get("triage_level")),
			"krcs_chief_complaint": _text(vitals.get("chief_complaint")),
		}
		for key, field in VITAL_FIELDS.items():
			value = vitals.get(key)
			if value not in (None, ""):
				values[field] = value if field in ("weight", "height") else cstr(value)
		doc = frappe.get_doc(values)
		doc.insert(ignore_permissions=True)  # DocPerm: Vital Signs is the Triage Nurse's (D58)
		doc.submit()  # vital_signs_on_submit -> start_triage (system event)
	if frappe.db.get_value(VISIT_DOCTYPE, visit, "status") == VISIT_STATUS_REGISTERED:
		engine.transition(visit, engine.ACTION_START_TRIAGE, system=True)
	engine.transition(
		visit,
		engine.ACTION_ROUTE,
		system=True,
		department=department,
		without_vitals_reason=None if vitals else OUTREACH_NO_VITALS_REASON,
	)


def _drug_code(medication: str) -> str | None:
	from onerc_healthcare.pharmacy.catalogue import items_for_medication

	items = items_for_medication(medication)
	return items[0] if items else None


def _consult(rec, visit: str, patient: str, department: str, parts) -> str:
	"""Steps 4-8: consultation, lab orders, POC results, completion, dispensing. Returns the encounter."""
	visit_doc = engine.transition(visit, engine.ACTION_START_CONSULTATION, system=True)
	c = parts.consult
	values = {
		"doctype": ENCOUNTER_DOCTYPE,
		"patient": patient,
		"practitioner": practitioner.get_or_create(),
		"appointment_type": DEFAULT_APPOINTMENT_TYPE,
		"company": visit_doc.company,
		"encounter_date": _captured_date(rec),
		"encounter_time": rec.captured_at.strftime("%H:%M:%S"),
		"medical_department": department,
		ENCOUNTER_VISIT_FIELD: visit,
		"krcs_department": department,
		"krcs_presenting_complaint": _text(c.get("presenting_complaint"))
		or consultation._triage_complaint(visit_doc),
		"krcs_history": _text(c.get("history")),
		"krcs_examination_findings": _text(c.get("examination_findings")),
		"krcs_management_plan": _text(c.get("management_plan")),
		diagnosis.DIAGNOSES_FIELD: [
			{
				"code": _text(d.get("code")),
				"case_type": _text(d.get("case_type")),
				"is_primary": cint(d.get("is_primary")),
			}
			for d in c.get("diagnoses") or []
			if isinstance(d, dict)
		],
		"lab_test_prescription": [
			{"observation_template": line["observation_template"]} for line in parts.labs
		],
		"drug_prescription": [
			{
				"medication": line["medication"],
				"drug_code": _drug_code(line["medication"]),
				"dosage": _text(line.get("dosage")),
				"period": _text(line.get("period")),
				"dosage_form": _text(line.get("dosage_form"))
				or frappe.db.get_value("Medication", line["medication"], "dosage_form"),
				"comment": _text(line.get("comment")),
			}
			for line in parts.drugs
		],
	}
	if not values[diagnosis.DIAGNOSES_FIELD]:
		values[diagnosis.NO_DIAGNOSIS_REASON_FIELD] = _text(c.get("no_diagnosis_reason"))
	encounter = frappe.get_doc(values)
	encounter.insert(ignore_permissions=True)  # DocPerm: Patient Encounter is the Clinical Officer's (D58)
	engine.link_encounter(visit, encounter.name)

	results_at: list[datetime] = []
	if parts.labs:
		orders = _send_lab_orders(encounter.name, visit)
		for line, service_request in zip(parts.labs, orders, strict=True):
			poc = line.get("poc_result")
			if poc:
				_record_poc(service_request, visit, cstr(poc["result"]).strip(), poc["_resulted_at"])
				results_at.append(poc["_resulted_at"])
	_complete(encounter.name, visit, parts.outcome)
	dispensed_at = _dispense(encounter.name, parts.drugs)
	_backdate_visit(visit, rec.captured_at, max([rec.captured_at, *results_at, *dispensed_at]))
	return encounter.name


def _send_lab_orders(encounter: str, visit: str) -> list[str]:
	"""`consultation.send_lab_orders` without its DocPerm checks: Marley creates the Service Requests, every
	order is provisioned for the laboratory, the visit gets ``request_lab``. Returns the orders in row order."""
	from onerc_healthcare.lab.orders import ensure_observations

	_visit_doc, doc = lock_visit_and_encounter(encounter)
	doc.make_service_request()
	doc.flags.ignore_permissions = True  # DocPerm: Patient Encounter write (D58)
	doc.save()
	orders = [row.service_request for row in doc.lab_test_prescription]
	for service_request in orders:
		ensure_observations(service_request)
	tasks.raise_event(visit, engine.ACTION_REQUEST_LAB)
	return orders


def _record_poc(service_request: str, visit: str, value: str, resulted_at: datetime) -> str:
	"""Record a point-of-care result: the sample (Marley's collect step), the result, then the approval with
	the visit locked first (`lab.guards.set_observation_status` semantics). The approved (submitted)
	Observation counts for the MOH reports (e.g. a positive MRDT confirms malaria).

	Marley's ``Observation.on_update`` (`set_diagnostic_report_status`) lists the encounter's Observations with
	``frappe.get_list``, i.e. with the *session user's* read permission, which the Outreach Team Member must not
	have (D58). So the Observation steps run as the system (`acting_as("Administrator")`) and the member is then
	recorded as the author (owner / modified_by) of the Specimen and Observation. A ``lab_results_ready`` visit
	event raised by the approval is logged as Administrator (a system event)."""
	from onerc_healthcare.lab.events import _lock_visit
	from onerc_healthcare.lab.orders import root_observation

	member = frappe.session.user
	sr = frappe.get_doc("Service Request", service_request)
	if cint(frappe.db.get_value("Observation Template", sr.template_dn, "has_component")):
		fail(_("{0} is a panel; panels are not recorded at the point of care.").format(sr.template_dn))
	with acting_as("Administrator"):
		observation = root_observation(sr) or _collect_sample(sr, resulted_at)
		doc = frappe.get_doc("Observation", observation)
		doc.result = value
		doc.time_of_result = resulted_at
		doc.save()
		_lock_visit(visit)  # lock order Visit -> Observation (lab.guards)
		doc = frappe.get_doc("Observation", observation)
		if not doc.is_ready_for_approval():
			fail(_("Enter the result of {0} to approve it.").format(sr.template_dn))
		doc.status = "Approved"
		doc.submit()  # Observation hooks: lab_results_ready when the encounter's lab work is complete
	# Marley clears time_of_approval in validate and `lab.events.stamp_approval_time` stamps the submit time;
	# the clinical approval time is the device's resulted_at. The member did the test: they are its author.
	frappe.db.set_value(
		"Observation",
		observation,
		{
			"time_of_approval": resulted_at,
			"owner": member,
			"modified_by": member,
		},
		update_modified=False,
	)
	specimen = frappe.db.get_value("Observation", observation, "specimen")
	if specimen:
		frappe.db.set_value(
			"Specimen", specimen, {"owner": member, "modified_by": member}, update_modified=False
		)
	return observation


def _collect_sample(sr, collected_at: datetime) -> str:
	"""Marley's `sample_collection.collect_sample` for one single-test row: a Specimen, the Observation
	(Marley's `new_observation`), the row marked Collected. Marley's whitelisted `create_observation` checks
	Sample Collection write and saves the Specimen with the user's DocPerms, which the member has not."""
	from healthcare.healthcare.doctype.observation.observation import new_observation
	from healthcare.healthcare.doctype.sample_collection.sample_collection import update_collection_status

	row = frappe.db.get_value(
		SAMPLE_ROW,
		{
			"service_request": sr.name,
			"parenttype": SAMPLE_COLLECTION,
			"docstatus": ["!=", 2],
			"status": "Open",
		},
		["name", "parent", "observation_template", "sample", "sample_type"],
		as_dict=True,
	)
	if not row:
		fail(_("Laboratory order {0} has no sample to collect.").format(sr.name))
	collection = frappe.db.get_value(
		SAMPLE_COLLECTION, row.parent, ["patient", "company", "referring_practitioner"], as_dict=True
	)
	specimen = frappe.new_doc("Specimen")
	specimen.received_time = collected_at
	specimen.patient = collection.patient
	specimen.sample_collection = row.parent
	specimen.specimen_type = row.sample or row.sample_type
	specimen.insert()  # as the system (see _record_poc)
	observation = new_observation(
		patient=collection.patient,
		template=row.observation_template,
		doc=SAMPLE_COLLECTION,
		docname=row.parent,
		company=collection.company,
		specimen=specimen.name,
		practitioner=collection.referring_practitioner,
		child="",
		service_request=sr.name,
	)
	frappe.db.set_value(
		SAMPLE_ROW,
		row.name,
		{"status": "Collected", "collection_date_time": collected_at, "specimen": specimen.name},
	)
	update_collection_status(frappe._dict(sample_collection=row.parent))
	return observation


def _complete(encounter: str, visit: str, outcome: str) -> None:
	"""`consultation.complete_consultation` without its DocPerm checks (reason recorded while a sample went to
	the facility laboratory)."""
	visit_doc, doc = lock_visit_and_encounter(encounter)
	rule = engine.TRANSITIONS[engine.ACTION_COMPLETE_CONSULTATION]
	engine.resolve_outcome(visit_doc, engine.ACTION_COMPLETE_CONSULTATION, outcome, system=True)
	if visit_doc.status not in rule.from_statuses:
		fail(_("Visit {0} is {1}; the consultation cannot be completed.").format(visit, _(visit_doc.status)))
	reason = OUTREACH_LAB_PENDING_REASON if cint(visit_doc.lab_open) else None
	doc.flags.ignore_permissions = True  # DocPerm: Patient Encounter submit (D58)
	medication_requests = consultation.submit_encounter(doc)
	if medication_requests:
		tasks.raise_event(visit, engine.ACTION_PRESCRIBE)
	engine.transition(
		visit, engine.ACTION_COMPLETE_CONSULTATION, comment=reason, outcome=outcome, system=True
	)


def _dispense(encounter: str, drugs: list[dict]) -> list[datetime]:
	"""Step 8: a submitted Dispensing Record for each line dispensed in the field. Returns the times."""
	rows = sorted(
		frappe.get_all(
			"Drug Prescription",
			filters={"parent": encounter, "parenttype": ENCOUNTER_DOCTYPE},
			fields=["idx", "medication", "medication_request"],
		),
		key=lambda r: r.idx,
	)
	times = []
	for line, row in zip(drugs, rows, strict=True):
		dispensed = line.get("dispensed")
		if not dispensed:
			continue
		if not row.medication_request:
			fail(_("The prescription of {0} was not released.").format(line["medication"]))
		doc = frappe.get_doc(
			{
				"doctype": DISPENSING_DOCTYPE,
				"medication_request": row.medication_request,
				"outcome": "Dispensed",
				"quantity": flt(dispensed.get("quantity")),
				# §6 F3: given in the field, the line is done (a partial quantity closes it as Closed) so the
				# facility pharmacy worklist does not offer it again.
				"final": 1,
			}
		)
		doc.insert(ignore_permissions=True)  # DocPerm: Dispensing Record is the Pharmacist's (D58)
		doc.submit()  # before_submit sets dispensed_by = the member and dispensed_at = now
		frappe.db.set_value(
			DISPENSING_DOCTYPE, doc.name, "dispensed_at", dispensed["_dispensed_at"], update_modified=False
		)
		times.append(dispensed["_dispensed_at"])
	return times


def _backdate_visit(visit: str, captured_at: datetime, ended_at: datetime) -> None:
	"""The visit's stage times are the field times, not the sync time (the status log keeps the sync time)."""
	row = frappe.db.get_value(
		VISIT_DOCTYPE, visit, [*STAGE_TIMES, "status", "closed_at", "outcome_at"], as_dict=True
	)
	values = {field: captured_at for field in STAGE_TIMES if row.get(field)}
	values["status_changed_at"] = ended_at
	if row.outcome_at:
		values["outcome_at"] = ended_at
	if row.closed_at:
		values["closed_at"] = ended_at
	frappe.db.set_value(VISIT_DOCTYPE, visit, values, update_modified=False)


# ----------------------------------------------------------------------------------- community.activity


def apply_community_activity(rec) -> Outcome:
	"""A Community Activity Record (health talk, home visit) at the session facility, led by the member. The
	Outreach Team Member has its own DocPerm on it (create and submit, own records), so no skip here."""
	p = rec.payload
	doc = frappe.get_doc(
		{
			"doctype": COMMUNITY_ACTIVITY_DOCTYPE,
			"activity_type": _text(p.get("activity_type")),
			"activity_date": getdate(p.get("activity_date"))
			if _text(p.get("activity_date"))
			else _captured_date(rec),
			"facility": rec.session.facility,
			"location": _text(p.get("location")),
			"topic": _text(p.get("topic")),
			"participants_male": cint(p.get("participants_male")),
			"participants_female": cint(p.get("participants_female")),
			"participants_total": cint(p.get("participants_total")),
			"lead_staff": rec.user,
			"notes": _text(p.get("notes")),
		}
	)
	doc.insert()
	doc.submit()
	return accepted(COMMUNITY_ACTIVITY_DOCTYPE, doc.name)


APPLIERS = {
	RECORD_PATIENT_REGISTER: apply_patient_register,
	RECORD_PATIENT_UPDATE: apply_patient_update,
	RECORD_VISIT_CAPTURE: apply_visit_capture,
	RECORD_COMMUNITY_ACTIVITY: apply_community_activity,
}
# The Sync Conflict type each record type may raise (FR-26).
CONFLICTS_RAISED = {
	RECORD_PATIENT_REGISTER: CONFLICT_DUPLICATE_REGISTRATION,
	RECORD_PATIENT_UPDATE: CONFLICT_DEMOGRAPHIC_CHANGE,
	RECORD_VISIT_CAPTURE: CONFLICT_OVERLAPPING_VISIT,
}
