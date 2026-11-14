# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""S3 state-machine audit of the Phase 4 build (Outreach: O1-O3 + W0). Never commit.

Every Phase 4 lifecycle is driven through its own API, by every relevant actor, from every status, and compared
with an independent oracle written from the module docstrings, docs/OUTREACH_PROTOCOL.md and D58 (the tables are
copied below as literals; meta-tests compare them with the code's introspectable tables). Known defects are kept
as ``@unittest.expectedFailure`` tests that assert the *correct* behaviour (comment ``DEFECT-P4-n``), so they
turn into unexpected successes once fixed. Matrix oracles stay lenient on the exact combinations a DEFECT-P4
test covers, so a fix never breaks a matrix.

Machines:

1. Outreach Session (`outreach/session.py` TRANSITIONS): activate / return / close / cancel x status x actor
   (team member, team lead, outsider with the outreach role, HIS Officer, Medical In-Charge, Nurse Receptionist,
   Clinical Officer, EMR System Administrator, Administrator); offered (`allowed_actions`) == working; close
   refused while Held / Conflict sync records exist; edit locks by status.
2. Outreach Sync Record (`outreach/sync.py`): Accepted / Failed / Held / Conflict / Discarded x re-send, for every
   record type; another member (or another session) re-sending someone else's UUID; `status`.
3. Sync Conflict (`outreach/conflicts.py`): decision x conflict type x review_only x actor; offered == working;
   no second decision; Retry; ``Patient.krcs_open_sync_conflicts`` after every path.
4. Device grant (`outreach/devices.py`, `outreach/auth.py`): enrol / expiry / revoke / recover_key / auth hook
   across device, grant, session, team and user states.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

import frappe
from frappe.utils import add_days, add_to_date, cint, getdate, now_datetime, random_string, today

from onerc_healthcare.constants import (
	COMMUNITY_ACTIVITY_DOCTYPE,
	CONFLICT_DECISIONS,
	CONFLICT_DEMOGRAPHIC_CHANGE,
	CONFLICT_DUPLICATE_REGISTRATION,
	CONFLICT_OPEN,
	CONFLICT_OVERLAPPING_VISIT,
	CONFLICT_RESOLVED,
	DECISION_ACKNOWLEDGE,
	DECISION_KEEP_FACILITY,
	DECISION_KEEP_OUTREACH,
	DECISION_LINK,
	DECISION_MERGE,
	DEVICE_ACTIVE,
	DEVICE_REVOKED,
	IDENTIFIER_UNHCR,
	OUTREACH_AUTH_SCHEME,
	OUTREACH_DEVICE_DOCTYPE,
	OUTREACH_GRANT_DOCTYPE,
	OUTREACH_RECORD_DOCTYPE,
	OUTREACH_SESSION_DOCTYPE,
	PATIENT_OPEN_CONFLICTS_FIELD,
	RECORD_COMMUNITY_ACTIVITY,
	RECORD_PATIENT_REGISTER,
	RECORD_PATIENT_UPDATE,
	RECORD_STATUS_ACCEPTED,
	RECORD_STATUS_CONFLICT,
	RECORD_STATUS_DISCARDED,
	RECORD_STATUS_FAILED,
	RECORD_STATUS_HELD,
	RECORD_STATUSES,
	RECORD_VISIT_CAPTURE,
	ROLE_HIS,
	ROLE_MIC,
	ROLE_SYSADMIN,
	SESSION_ACTIVE,
	SESSION_CANCELLED,
	SESSION_CLOSED,
	SESSION_OPEN_STATUSES,
	SESSION_PLANNED,
	SESSION_RETURNED,
	SYNC_CONFLICT_DOCTYPE,
	VISIT_DOCTYPE,
)
from onerc_healthcare.outreach import apply, auth, conflicts, devices, sync
from onerc_healthcare.outreach import session as sessions
from onerc_healthcare.outreach.session import IllegalSessionTransitionError, OutreachSessionError
from onerc_healthcare.tests import sm_helpers as sm
from onerc_healthcare.tests.factories import get_facility, make_user
from onerc_healthcare.tests.outreach_factories import make_catchment, make_session
from onerc_healthcare.tests.sm_helpers import ADMIN, as_user, savepoint
from onerc_healthcare.tests.test_outreach_conflicts import RATION, ConflictCase
from onerc_healthcare.tests.test_outreach_devices import fake_request, new_device_id, new_key
from onerc_healthcare.tests.test_outreach_sync import new_uuid, random_name
from onerc_healthcare.tests.visit_factories import make_visit
from onerc_healthcare.visit import engine

PLANNED, ACTIVE, RETURNED, CLOSED, CANCELLED = (
	SESSION_PLANNED,
	SESSION_ACTIVE,
	SESSION_RETURNED,
	SESSION_CLOSED,
	SESSION_CANCELLED,
)
ALL_SESSION_STATUSES = (PLANNED, ACTIVE, RETURNED, CLOSED, CANCELLED)
ACCEPTED, FAILED, HELD, CONFLICT, DISCARDED = (
	RECORD_STATUS_ACCEPTED,
	RECORD_STATUS_FAILED,
	RECORD_STATUS_HELD,
	RECORD_STATUS_CONFLICT,
	RECORD_STATUS_DISCARDED,
)
DUP, DEMO, OVERLAP = CONFLICT_DUPLICATE_REGISTRATION, CONFLICT_DEMOGRAPHIC_CHANGE, CONFLICT_OVERLAPPING_VISIT
KF, KO, MERGE, LINK, ACK = (
	DECISION_KEEP_FACILITY,
	DECISION_KEEP_OUTREACH,
	DECISION_MERGE,
	DECISION_LINK,
	DECISION_ACKNOWLEDGE,
)
SYSTEM = "system"
REASON = "T- s3 reason"
MANAGERS = (ROLE_HIS, ROLE_MIC)

# Actor key -> role. "member" is the team lead (first member), "member2" a plain team member, "outsider" holds the
# outreach role but is not on the team (outreach_factories.OUTREACH_USERS + an S3 EMR System Administrator).
ROLE_OF = {
	"member": "Outreach Team Member",
	"member2": "Outreach Team Member",
	"outsider": "Outreach Team Member",
	"his": ROLE_HIS,
	"mic": ROLE_MIC,
	"reception": "Nurse Receptionist",
	"clinician": "Clinical Officer",
	"sysadmin": ROLE_SYSADMIN,
	ADMIN: ADMIN,
}
LEAD = "member"
SESSION_ACTORS = tuple(ROLE_OF)
NON_RESOLVERS = ("member", "member2", "outsider", "reception", "clinician", "sysadmin")
RESOLVERS = ("his", "mic")
RESOLVERS_ROLES = (ROLE_HIS, ROLE_MIC)  # conflicts docstring / D58 (OI-11)


def _try(fn, *args, **kwargs):
	try:
		return fn(*args, **kwargs), None
	except Exception as e:  # compared with the oracle by the caller
		return None, e


# =====================================================================================================
# Oracles (literal copies of the documented tables)
# =====================================================================================================

# outreach/session.py docstring lifecycle table: action -> (from, to, roles or SYSTEM, lead allowed, reason required)
EXPECTED_SESSION: dict[str, tuple[tuple[str, ...], str, tuple[str, ...] | str, bool, bool]] = {
	"activate": ((PLANNED,), ACTIVE, SYSTEM, False, False),
	"return": ((ACTIVE,), RETURNED, MANAGERS, True, False),
	"close": ((RETURNED,), CLOSED, MANAGERS, False, False),
	"cancel": ((PLANNED,), CANCELLED, MANAGERS, False, True),
}
# Docstring "Planning rules": what may change in which status.
EDIT_ALLOWED_IN = {
	"title": ALL_SESSION_STATUSES,  # not restricted by the docstring (control)
	"team_add": (PLANNED, ACTIVE),
	"team_remove": (PLANNED, ACTIVE),
	"lead_change": (PLANNED, ACTIVE),
	"departments": (PLANNED, ACTIVE),
	"start_date": (PLANNED, ACTIVE),
	"end_date": (PLANNED, ACTIVE),
	"catchment": (PLANNED,),
	"facility": (PLANNED,),
}


def expect_session(status: str, action: str, actor: str, reason: str | None):
	"""Expected exception class or the new status for `apply_action(action)` by `actor` (docstring table)."""
	from_statuses, to_status, roles, lead_ok, needs_reason = EXPECTED_SESSION[action]
	if roles == SYSTEM:
		return IllegalSessionTransitionError  # a system event is never an apply_action action
	if actor != ADMIN and ROLE_OF[actor] not in roles and not (lead_ok and actor == LEAD):
		return frappe.PermissionError
	if status not in from_statuses:
		return IllegalSessionTransitionError
	if needs_reason and not reason:
		return IllegalSessionTransitionError
	return to_status


# outreach/sync.py docstring + protocol §4: stored status -> (first answer, answer to a re-send)
EXPECTED_RESULT = {
	ACCEPTED: ("accepted", "duplicate"),
	DISCARDED: ("accepted", "duplicate"),
	FAILED: ("failed", "failed"),
	HELD: ("held", "held"),
	CONFLICT: ("conflict", "conflict"),
}
EXPECTED_RECORD_TRANSITIONS = {
	"receive": ((None,), (ACCEPTED, FAILED, HELD, CONFLICT)),
	"retry": ((FAILED,), (ACCEPTED, FAILED, HELD, CONFLICT)),
	"replay": ((HELD,), (ACCEPTED, FAILED, HELD, CONFLICT)),
	"resolve": ((CONFLICT,), (ACCEPTED, DISCARDED)),
}

# outreach/conflicts.py docstring decision table: (type, review_only) -> {decision: record status afterwards}
# (None: the record keeps its status).
EXPECTED_DECISIONS: dict[tuple[str, bool], dict[str, str | None]] = {
	(DUP, False): {KF: DISCARDED, LINK: ACCEPTED, MERGE: ACCEPTED, KO: ACCEPTED},
	(DEMO, False): {KF: DISCARDED, MERGE: ACCEPTED, KO: ACCEPTED},
	(OVERLAP, False): {KF: DISCARDED, KO: ACCEPTED},
	(OVERLAP, True): {ACK: None},
}
CONFLICT_KINDS = {
	"dup": (DUP, False),
	"demo": (DEMO, False),
	"overlap": (OVERLAP, False),
	"review": (OVERLAP, True),
}


# =====================================================================================================
# Base case
# =====================================================================================================


class P4Case(ConflictCase):
	"""SyncCase's Planned session (member = lead, member2) plus an EMR System Administrator."""

	def setUp(self):
		super().setUp()
		self.users["sysadmin"] = make_user("s3-sysadmin", [ROLE_SYSADMIN])

	def u(self, actor: str) -> str:
		return ADMIN if actor == ADMIN else self.users[actor]

	def act_as(self, actor: str, fn, *args, **kwargs):
		with as_user(self.u(actor)):
			return _try(fn, *args, **kwargs)

	# -- sessions ---------------------------------------------------------------------------------------
	def new_session(self, status: str = PLANNED, departments=("Outpatient",)) -> str:
		name = make_session(
			members=[self.users["member"], self.users["member2"]],
			catchment_area=self.area,
			departments=departments,
			start_date=add_days(today(), -3),
			days=10,
		).name
		self.move_session(name, status)
		return name

	def move_session(self, name: str, status: str) -> None:
		path = {
			PLANNED: [],
			ACTIVE: ["activate"],
			RETURNED: ["activate", "return"],
			CLOSED: ["activate", "return", "close"],
			CANCELLED: ["cancel"],
		}[status]
		for action in path:
			sessions.transition(name, action, reason=REASON if action == "cancel" else None, system=True)
		self.assertEqual(self.session_status(name), status)

	def session_status(self, name: str) -> str:
		return frappe.db.get_value(OUTREACH_SESSION_DOCTYPE, name, "status")

	def session_state(self, name: str) -> dict:
		doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, name)
		return {"status": doc.status, "log": len(doc.status_log), "reason": doc.cancellation_reason}

	def raw_record(self, session: str, status: str) -> str:
		"""An Outreach Sync Record row of `session` in `status` (as O1's own close test inserts it)."""
		name = new_uuid()
		frappe.db.sql(
			f"""insert into `tab{OUTREACH_RECORD_DOCTYPE}`
			(name, client_uuid, record_type, status, session, user, creation, modified, owner, modified_by)
			values (%s, %s, 'visit.capture', %s, %s, %s, now(), now(), 'Administrator', 'Administrator')""",
			(name, name, status, session, self.users["member"]),
		)
		return name

	# -- sync -------------------------------------------------------------------------------------------
	def activity_payload(self, activity_type: str = "Health Talk") -> dict:
		return {
			"activity_type": activity_type,
			"activity_date": str(self.captured.date()),
			"location": "T- Village 3 market",
			"topic": "T- malaria prevention",
			"participants_male": 3,
			"participants_female": 4,
			"participants_total": 7,
			"notes": None,
		}

	def counts(self) -> dict:
		return {
			dt: frappe.db.count(dt)
			for dt in (
				"Patient",
				VISIT_DOCTYPE,
				COMMUNITY_ACTIVITY_DOCTYPE,
				SYNC_CONFLICT_DOCTYPE,
				OUTREACH_RECORD_DOCTYPE,
				"Patient Identifier",
			)
		}

	def rec_state(self, client_uuid: str) -> dict:
		row = frappe.db.get_value(
			OUTREACH_RECORD_DOCTYPE,
			client_uuid,
			["status", "attempts", "user", "session", "target_doctype", "target_name", "conflict", "payload"],
			as_dict=True,
		)
		return dict(row) if row else {}

	def conflicted_registration(self) -> tuple[dict, str, str]:
		"""A patient.register carrying an existing patient's UNHCR ID: (envelope, conflict, existing patient)."""
		existing = self.existing()
		unhcr = frappe.get_all(
			"Patient Identifier", filters={"parent": existing.name}, pluck="identifier_value", limit=1
		)[0]
		env = self.env(
			RECORD_PATIENT_REGISTER,
			self.register_payload(
				identifiers=[{"identifier_type": IDENTIFIER_UNHCR, "identifier_value": unhcr}]
			),
		)
		result = self.push([env])["results"][0]
		self.assertEqual(result["result"], "conflict", result)
		return env, result["conflict"], existing.name

	def close_facility_visit(self, visit: str) -> None:
		engine.transition(visit, "close_visit", system=True, closure_reason="Other", comment="T- s3")

	def open_conflicts(self, patient: str) -> int:
		return cint(frappe.db.get_value("Patient", patient, PATIENT_OPEN_CONFLICTS_FIELD))


# =====================================================================================================
# 1. Outreach Session lifecycle
# =====================================================================================================


class TestSessionLifecycle(P4Case):
	def test_transition_table_matches_docstring(self):
		self.assertEqual(set(sessions.TRANSITIONS), set(EXPECTED_SESSION))
		for action, rule in sessions.TRANSITIONS.items():
			from_statuses, to_status, roles, lead_ok, needs_reason = EXPECTED_SESSION[action]
			code_roles = SYSTEM if rule.is_system else set(rule.roles)
			self.assertEqual(
				(set(rule.from_statuses), rule.to_status, code_roles, rule.lead_allowed, rule.needs_reason),
				(
					set(from_statuses),
					to_status,
					roles if roles == SYSTEM else set(roles),
					lead_ok,
					needs_reason,
				),
				action,
			)
		self.assertEqual(set(SESSION_OPEN_STATUSES), {PLANNED, ACTIVE, RETURNED})
		# Held and Conflict block the close (Failed too since protocol §6 F1: DEFECT-P4-5)
		self.assertLessEqual({HELD, CONFLICT}, set(sessions.UNRESOLVED_RECORD_STATUSES))

	def check_step(self, name, before, action, exp, err, actor, reason):
		after = self.session_state(name)
		if isinstance(exp, type):
			self.assertTrue(isinstance(err, exp), f"expected {exp.__name__}, got {err!r}")
			self.assertEqual(after, before)
			return False
		self.assertIsNone(err, f"expected success, got {err!r}")
		self.assertEqual((after["status"], after["log"]), (exp, before["log"] + 1))
		last = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, name).status_log[-1]
		self.assertEqual(
			(last.from_status, last.to_status, last.action, last.user, last.comment or None),
			(before["status"], exp, action, self.u(actor), reason),
		)
		if action == "cancel":
			self.assertEqual(after["reason"], reason)
		return True

	def test_lifecycle_matrix_and_offered_actions(self):
		"""Every action x every status x every actor x (with / without reason) through `apply_action`, compared
		with the docstring table; then `allowed_actions` (the form's buttons) == the actions that worked."""
		for status in ALL_SESSION_STATUSES:
			name = self.new_session(status)
			for actor in SESSION_ACTORS:
				works = set()
				for action in EXPECTED_SESSION:
					for reason in (None, REASON):
						exp = expect_session(status, action, actor, reason)
						with self.subTest(status=status, actor=actor, action=action, reason=reason):
							with savepoint("s3_session"):
								before = self.session_state(name)
								_out, err = self.act_as(actor, sessions.apply_action, name, action, reason)
								if self.check_step(name, before, action, exp, err, actor, reason) and reason:
									works.add(action)
				with self.subTest(status=status, actor=actor, check="offered"):
					doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, name)
					offered = {a["action"]: a for a in sessions.allowed_actions(doc, self.u(actor))}
					self.assertEqual(set(offered), works)
					for action, a in offered.items():
						self.assertEqual(a["needs_reason"], EXPECTED_SESSION[action][4])
					# onload hands the same list to the form
					with as_user(self.u(actor)):
						if actor == ADMIN or frappe.has_permission(OUTREACH_SESSION_DOCTYPE, "read", doc=doc):
							doc.run_method("onload")
							self.assertEqual(
								{a["action"] for a in doc.get_onload().get("allowed_actions") or []}, works
							)

	def test_activate_is_a_system_event(self):
		"""activate: Planned -> Active, only as server code (transition(system=True), the first package
		download) or Administrator; any other user is refused before the status is looked at."""
		for status in ALL_SESSION_STATUSES:
			name = self.new_session(status)
			with self.subTest(status=status, path="system"), savepoint("s3_act"):
				_out, err = _try(sessions.transition, name, "activate", system=True)
				self.assertEqual(err is None, status == PLANNED, repr(err))
				self.assertEqual(self.session_status(name), ACTIVE if status == PLANNED else status)
			with self.subTest(status=status, path="download"), savepoint("s3_act"):
				self.assertEqual(sessions.activate_on_download(name), status == PLANNED)
				self.assertEqual(self.session_status(name), ACTIVE if status == PLANNED else status)
			for actor in SESSION_ACTORS:
				with self.subTest(status=status, actor=actor), savepoint("s3_act"):
					_out, err = self.act_as(actor, sessions.transition, name, "activate")
					if actor == ADMIN:
						self.assertEqual(err is None, status == PLANNED, repr(err))
					else:
						self.assertIsInstance(err, frappe.PermissionError)
						self.assertEqual(self.session_status(name), status)

	def test_unknown_action_refused(self):
		name = self.new_session(ACTIVE)
		for actor in ("his", ADMIN):
			_out, err = self.act_as(actor, sessions.apply_action, name, "reopen")
			self.assertIsInstance(err, IllegalSessionTransitionError)
		self.assertEqual(self.session_status(name), ACTIVE)

	def test_close_refused_while_held_or_conflict(self):
		"""close is refused while an Outreach Sync Record of *this* session is Held or Conflict (and, protocol
		§6 F1, Failed: DEFECT-P4-5, lenient here); Accepted and Discarded never block, nor a Held record of
		another session."""
		name = self.new_session(RETURNED)
		other = self.new_session(ACTIVE)
		for record_status in RECORD_STATUSES:
			for actor in ("his", "mic", ADMIN):
				with self.subTest(record=record_status, actor=actor), savepoint("s3_close"):
					self.raw_record(name, record_status)
					self.raw_record(other, HELD)
					before = self.session_state(name)
					_out, err = self.act_as(actor, sessions.apply_action, name, "close")
					if record_status == FAILED:  # DEFECT-P4-5
						self.assertEqual(self.session_status(name), CLOSED if err is None else RETURNED)
					elif record_status in (HELD, CONFLICT):
						self.assertIsInstance(err, IllegalSessionTransitionError)
						self.assertIn("held or in conflict", str(err))
						self.assertEqual(self.session_state(name), before)
					else:
						self.assertIsNone(err, repr(err))
						self.assertEqual(self.session_status(name), CLOSED)

	def test_close_after_real_conflict_resolved(self):
		"""Through the real sync: a duplicate registration (Conflict) plus an activity held on it block the close;
		after the HIS Officer's decision (held activity replayed) the session closes."""
		reg, conflict, _existing = self.conflicted_registration()
		held = self.env(RECORD_COMMUNITY_ACTIVITY, self.activity_payload(), depends_on=[reg["client_uuid"]])
		self.assertEqual(self.results(self.push([held])), ["held"])
		self.move_session(self.session, ACTIVE)
		with as_user(self.users["member"]):  # the team lead marks the team returned
			sessions.apply_action(self.session, "return")
		_out, err = self.act_as("his", sessions.apply_action, self.session, "close")
		self.assertIsInstance(err, IllegalSessionTransitionError)
		self.assertIn("2 synced record", str(err))
		# Records may still sync while Returned
		late = self.env(RECORD_COMMUNITY_ACTIVITY, self.activity_payload())
		self.assertEqual(self.results(self.push([late])), ["accepted"])
		self.resolve(conflict, KF)
		self.assertEqual(self.rec_state(held["client_uuid"])["status"], ACCEPTED)
		_out, err = self.act_as("mic", sessions.apply_action, self.session, "close")
		self.assertIsNone(err, repr(err))
		self.assertEqual(self.session_status(self.session), CLOSED)
		# The member (online sign-in) still reads the final results (sync.status docstring)
		with as_user(self.users["member"]):
			out = sync.status(self.session, [reg["client_uuid"], held["client_uuid"]])["results"]
		self.assertEqual(
			[(r["result"], bool(r.get("discarded"))) for r in out], [("accepted", True), ("accepted", False)]
		)

	# DEFECT-P4-5: protocol §6 F1, closing is also refused while the session has Failed records
	@unittest.expectedFailure
	def test_close_refused_while_failed(self):
		"""DEFECT-P4-5: protocol §6 F1 ("Closing is also refused while the session has Failed records, as well as
		Held or Conflict ones"). `session.UNRESOLVED_RECORD_STATUSES` is still (Held, Conflict), so a Returned
		session with a Failed record closes. Expected: IllegalSessionTransitionError; once the record is
		corrected (Accepted) the session closes."""
		name = self.new_session(RETURNED)
		uid = self.raw_record(name, FAILED)
		_out, err = self.act_as("his", sessions.apply_action, name, "close")
		self.assertEqual(self.session_status(name), RETURNED)
		self.assertIsInstance(err, IllegalSessionTransitionError)
		frappe.db.set_value(OUTREACH_RECORD_DOCTYPE, uid, "status", ACCEPTED, update_modified=False)
		_out, err = self.act_as("his", sessions.apply_action, name, "close")
		self.assertIsNone(err, repr(err))

	# DEFECT-P4-1: Close Session offered while Held / Conflict records keep the session from closing
	@unittest.expectedFailure
	def test_close_not_offered_while_records_unresolved(self):
		"""DEFECT-P4-1: `allowed_actions` checks role and status only, so the Returned session's form offers
		"Close Session" to the HIS Officer / Medical In-Charge while a Held or Conflict record exists, and the
		button is then refused ("still held or in conflict"). Offered must equal working."""
		name = self.new_session(RETURNED)
		self.raw_record(name, CONFLICT)
		doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, name)
		for actor in ("his", "mic"):
			offered = {a["action"] for a in sessions.allowed_actions(doc, self.u(actor))}
			_out, err = self.act_as(actor, sessions.apply_action, name, "close")
			self.assertEqual("close" in offered, err is None, actor)


class TestSessionEditLocks(P4Case):
	"""Planning rules: team, departments and dates change only while Planned or Active; facility and catchment
	area only while Planned; only the managers write a session."""

	def setUp(self):
		super().setUp()
		self.area2, _leaf = make_catchment(get_facility(0))
		try:
			self.other_facility = get_facility(1)
		except IndexError:
			self.other_facility = None
		self.area3 = make_catchment(self.other_facility)[0] if self.other_facility else None

	def edit(self, doc, kind: str) -> tuple[str, object]:
		"""Apply edit `kind` to `doc`; returns (field to read back, expected value)."""
		if kind == "title":
			doc.title = f"T- renamed {random_string(4)}"
			return "title", doc.title
		if kind == "team_add":
			doc.append("team", {"user": self.users["outsider"], "is_lead": 0})
			return "team", 3
		if kind == "team_remove":
			doc.set("team", [r for r in doc.team if r.user != self.users["member2"]])
			return "team", 1
		if kind == "lead_change":
			for row in doc.team:
				row.is_lead = int(row.user == self.users["member2"])
			return "lead", self.users["member2"]
		if kind == "departments":
			doc.append("departments", {"department": "Maternal and Child Health"})
			return "departments", 2
		if kind == "start_date":
			doc.start_date = add_days(doc.start_date, -1)
			return "start_date", getdate(doc.start_date)
		if kind == "end_date":
			doc.end_date = add_days(doc.end_date, 1)
			return "end_date", getdate(doc.end_date)
		if kind == "catchment":
			doc.catchment_area = self.area2
			return "catchment_area", self.area2
		if kind == "facility":
			doc.catchment_area, doc.facility = self.area3, self.other_facility
			return "facility", self.other_facility
		raise AssertionError(kind)

	def read_back(self, name: str, what: str):
		doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, name)
		if what == "team":
			return len(doc.team)
		if what == "lead":
			return next((r.user for r in doc.team if r.is_lead), None)
		if what == "departments":
			return len(doc.departments)
		value = doc.get(what)
		return getdate(value) if what.endswith("_date") else value

	def test_edit_matrix(self):
		kinds = [k for k in EDIT_ALLOWED_IN if k != "facility" or self.other_facility]
		for status in ALL_SESSION_STATUSES:
			name = self.new_session(status)
			for kind in kinds:
				for actor in ("his", "mic"):
					with self.subTest(status=status, kind=kind, actor=actor), savepoint("s3_edit"):
						doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, name)
						what, value = self.edit(doc, kind)
						before = self.read_back(name, what)
						with as_user(self.u(actor)):
							_out, err = _try(doc.save)
						if status in EDIT_ALLOWED_IN[kind]:
							self.assertIsNone(err, repr(err))
							self.assertEqual(self.read_back(name, what), value)
						else:
							self.assertIsInstance(err, OutreachSessionError)
							self.assertIn("cannot change while", str(err))
							self.assertEqual(self.read_back(name, what), before)
						self.assertEqual(self.session_status(name), status)

	def test_only_managers_write(self):
		"""DocPerm + has_permission hook: a team lead, team member, outsider, receptionist, clinician or the EMR
		System Administrator cannot save a session (even a title edit while Planned)."""
		name = self.new_session(PLANNED)
		for actor in ("member", "member2", "outsider", "reception", "clinician", "sysadmin"):
			with self.subTest(actor=actor), savepoint("s3_write"):
				doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, name)
				doc.title = "T- hijacked"
				with as_user(self.u(actor)):
					_out, err = _try(doc.save)
				self.assertIsInstance(err, frappe.PermissionError)
				self.assertNotEqual(
					frappe.db.get_value(OUTREACH_SESSION_DOCTYPE, name, "title"), "T- hijacked"
				)

	def test_status_fields_only_through_transition(self):
		"""status, cancellation_reason and the log never change through a save, in any status."""
		for status in ALL_SESSION_STATUSES:
			name = self.new_session(status)
			for field, value in (
				("status", CLOSED if status != CLOSED else PLANNED),
				("cancellation_reason", "x"),
			):
				with self.subTest(status=status, field=field), savepoint("s3_guard"):
					doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, name)
					doc.set(field, value)
					with as_user(self.users["his"]):
						_out, err = _try(doc.save)
					self.assertIsInstance(err, frappe.ValidationError)
					self.assertEqual(self.session_status(name), status)


# =====================================================================================================
# 2. Outreach Sync Record statuses x re-send
# =====================================================================================================


class TestSyncRecordStates(P4Case):
	"""Every reachable stored status of every record type, re-sent: the stored result comes back, nothing is
	applied twice, Failed is retried; someone else's UUID is never applied or changed."""

	def test_tables_match_docstring(self):
		self.assertEqual(
			{k: (tuple(v[0]), set(v[1])) for k, v in sync.RECORD_TRANSITIONS.items()},
			{k: (tuple(v[0]), set(v[1])) for k, v in EXPECTED_RECORD_TRANSITIONS.items()},
		)
		self.assertEqual(sync.STATUS_RESULT, EXPECTED_RESULT)
		self.assertEqual(set(sync.FINAL_STATUSES), {ACCEPTED, DISCARDED})
		self.assertEqual(set(RECORD_STATUSES), set(EXPECTED_RESULT))
		# community.activity never raises a conflict (so it is never Conflict or Discarded)
		self.assertNotIn(RECORD_COMMUNITY_ACTIVITY, apply.CONFLICTS_RAISED)

	# -- builders: (envelope, fixed envelope or None) for a record of `rtype` stored as `status` ---------
	def build(self, rtype: str, status: str) -> tuple[dict, dict | None]:
		method = getattr(self, f"build_{rtype.replace('.', '_')}")
		env, fixed = method(status)
		out = self.push([env])["results"][0]
		if status == DISCARDED:
			self.assertEqual(out["result"], "conflict", out)
			self.resolve(out["conflict"], KF)
		else:
			self.assertEqual(out["result"], EXPECTED_RESULT[status][0], out)
		self.assertEqual(self.rec_state(env["client_uuid"])["status"], status)
		return env, fixed

	def held_on(self) -> str:
		reg, _conflict, _p = self.conflicted_registration()
		return reg["client_uuid"]

	def build_patient_register(self, status):
		if status in (CONFLICT, DISCARDED):
			existing = self.existing()
			unhcr = frappe.get_all(
				"Patient Identifier", filters={"parent": existing.name}, pluck="identifier_value", limit=1
			)[0]
			payload = self.register_payload(
				identifiers=[{"identifier_type": IDENTIFIER_UNHCR, "identifier_value": unhcr}]
			)
			return self.env(RECORD_PATIENT_REGISTER, payload), None
		if status == HELD:
			return self.env(
				RECORD_PATIENT_REGISTER, self.register_payload(), depends_on=[self.held_on()]
			), None
		if status == FAILED:
			env = self.env(RECORD_PATIENT_REGISTER, self.register_payload(estimated_age_years=None))
			fixed = {**env, "payload": {**env["payload"], "estimated_age_years": 30}}
			return env, fixed
		return self.env(RECORD_PATIENT_REGISTER, self.register_payload()), None

	def build_patient_update(self, status):
		patient = self.existing()
		base = str(frappe.db.get_value("Patient", patient.name, "modified"))
		payload = {"patient": patient.name, "base_modified": base, "changes": {"mobile": "0722000111"}}
		payload["add_identifiers"] = []
		if status in (CONFLICT, DISCARDED):
			doc = frappe.get_doc("Patient", patient.name)
			doc.mobile = "0711000111"
			doc.save(ignore_permissions=True)
			return self.env(RECORD_PATIENT_UPDATE, payload), None
		if status == HELD:
			return self.env(RECORD_PATIENT_UPDATE, payload, depends_on=[self.held_on()]), None
		if status == FAILED:
			env = self.env(RECORD_PATIENT_UPDATE, {**payload, "patient": "T-NO-SUCH-PATIENT"})
			return env, {**env, "payload": payload}
		return self.env(RECORD_PATIENT_UPDATE, payload), None

	def build_visit_capture(self, status):
		if status == HELD:
			ref = self.held_on()
			return self.env(RECORD_VISIT_CAPTURE, self.visit_payload(patient_ref=ref), depends_on=[ref]), None
		patient = self.existing().name
		if status in (CONFLICT, DISCARDED):
			make_visit(patient=patient)  # open facility visit
		if status == FAILED:
			env = self.env(RECORD_VISIT_CAPTURE, self.visit_payload(patient=patient, department="Nutrition"))
			return env, {**env, "payload": self.visit_payload(patient=patient)}
		return self.env(RECORD_VISIT_CAPTURE, self.visit_payload(patient=patient)), None

	def build_community_activity(self, status):
		if status == HELD:
			return self.env(
				RECORD_COMMUNITY_ACTIVITY, self.activity_payload(), depends_on=[self.held_on()]
			), None
		if status == FAILED:
			env = self.env(RECORD_COMMUNITY_ACTIVITY, self.activity_payload("T- No Such Activity Type"))
			return env, {**env, "payload": self.activity_payload()}
		return self.env(RECORD_COMMUNITY_ACTIVITY, self.activity_payload()), None

	# -- the matrix --------------------------------------------------------------------------------------
	def check_resend(self, env: dict, status: str, fixed: dict | None) -> None:
		uid = env["client_uuid"]
		stored = self.rec_state(uid)
		counts = self.counts()
		first, again = EXPECTED_RESULT[status]

		# status endpoint: the owner sees the first answer, member2 sees nothing
		with as_user(self.users["member"]):
			mine = sync.status(self.session, [uid])["results"][0]
		with as_user(self.users["member2"]):
			theirs = sync.status(self.session, [uid])["results"][0]
		self.assertEqual(mine["result"], first)
		self.assertEqual(bool(mine.get("discarded")), status == DISCARDED)
		self.assertIsNone(theirs["result"])

		# Another member re-sends the UUID: failed, record untouched, nothing applied
		other = self.push([env], user="member2")["results"][0]
		self.assertEqual(other["result"], "failed")
		self.assertIn("another user", other["error"])
		self.assertEqual(self.rec_state(uid), stored)
		self.assertEqual(self.counts(), counts)

		# Same member, the same UUID twice in one call and once more in a new batch
		out = self.push([env, env])["results"] + self.push([env])["results"]
		self.assertEqual([r["result"] for r in out], [again] * 3)
		for r in out:
			self.assertEqual(
				(r["client_uuid"], r["doctype"], r["name"], r["conflict"], bool(r.get("discarded"))),
				(
					uid,
					stored["target_doctype"],
					stored["target_name"],
					stored["conflict"],
					status == DISCARDED,
				),
			)
			self.assertEqual(bool(r["error"]), status == FAILED)
		after = self.rec_state(uid)
		self.assertEqual({**after, "attempts": None}, {**stored, "attempts": None})
		self.assertEqual(after["attempts"], stored["attempts"] + 3)
		self.assertEqual(self.counts(), counts)  # nothing applied (a failed retry rolls back)

		if status != FAILED:  # protocol §6 F2: never re-applied, whatever payload is sent
			changed = {**env, "payload": {**env["payload"], "t_s3_changed": 1}}
			out = self.push([changed])["results"][0]
			self.assertEqual(
				(out["result"], out["name"], out["conflict"]),
				(again, stored["target_name"], stored["conflict"]),
			)
			self.assertEqual(
				{k: self.rec_state(uid)[k] for k in ("payload", "status")},
				{"payload": stored["payload"], "status": status},
			)
			self.assertEqual(self.counts(), counts)

		if fixed is not None:  # Failed: retried with the corrected payload, then a duplicate
			result = self.push([fixed])["results"][0]
			self.assertEqual(result["result"], "accepted", result)
			self.assertEqual(self.rec_state(uid)["status"], ACCEPTED)
			snapshot = self.counts()
			self.assertEqual(frappe.parse_json(self.rec_state(uid)["payload"]), fixed["payload"])  # §6 F2
			dup = self.push([fixed])["results"][0]
			keys = ("client_uuid", "doctype", "name", "conflict", "error")
			self.assertEqual(
				({k: dup[k] for k in keys}, dup["result"]), ({k: result[k] for k in keys}, "duplicate")
			)
			self.assertEqual(self.counts(), snapshot)

	def run_type(self, rtype: str, statuses=RECORD_STATUSES):
		for status in statuses:
			with self.subTest(type=rtype, status=status), savepoint("s3_rec"):
				env, fixed = self.build(rtype, status)
				self.check_resend(env, status, fixed)

	def test_patient_register(self):
		self.run_type(RECORD_PATIENT_REGISTER)

	def test_patient_update(self):
		self.run_type(RECORD_PATIENT_UPDATE)

	def test_visit_capture(self):
		self.run_type(RECORD_VISIT_CAPTURE)

	def test_community_activity(self):
		self.run_type(RECORD_COMMUNITY_ACTIVITY, (ACCEPTED, FAILED, HELD))

	def test_uuid_from_another_session(self):
		"""The same member re-sends a stored UUID into a second session: failed, record untouched."""
		other = self.new_session(PLANNED)
		env = self.env(RECORD_COMMUNITY_ACTIVITY, self.activity_payload())
		self.assertEqual(self.results(self.push([env])), ["accepted"])
		stored, counts = self.rec_state(env["client_uuid"]), self.counts()
		moved = {**env, "session": other}
		out = self.push([moved], session=other)["results"][0]
		self.assertEqual(out["result"], "failed")
		self.assertEqual((self.rec_state(env["client_uuid"]), self.counts()), (stored, counts))

	def test_outsider_and_staff_cannot_resend(self):
		"""Not on the team: the whole call is refused, whatever the record's status."""
		env = self.env(RECORD_COMMUNITY_ACTIVITY, self.activity_payload())
		self.push([env])
		stored = self.rec_state(env["client_uuid"])
		for actor in ("outsider", "his", "mic", "reception", "clinician", "sysadmin"):
			with self.subTest(actor=actor):
				with self.assertRaises(frappe.PermissionError):
					self.push([env], user=actor)
		self.assertEqual(self.rec_state(env["client_uuid"]), stored)

	def test_held_replayed_after_decision(self):
		"""A Held record of every type is replayed when its conflict is resolved (Keep facility version)."""
		reg, conflict, existing = self.conflicted_registration()
		dep = [reg["client_uuid"]]
		base = str(frappe.db.get_value("Patient", existing, "modified"))
		envs = [
			self.env(RECORD_VISIT_CAPTURE, self.visit_payload(patient_ref=dep[0]), depends_on=dep),
			self.env(
				RECORD_PATIENT_UPDATE,
				{"patient": existing, "base_modified": base, "changes": {"allergies": "T- sulfa"}},
				depends_on=dep,
			),
			self.env(RECORD_COMMUNITY_ACTIVITY, self.activity_payload(), depends_on=dep),
		]
		self.assertEqual(self.results(self.push(envs)), ["held"] * 3)
		self.assertEqual(self.open_conflicts(existing), 1)
		out = self.resolve(conflict, KF)
		self.assertEqual([r["result"] for r in out["replayed"]], ["accepted"] * 3)
		for env in envs:
			self.assertEqual(self.rec_state(env["client_uuid"])["status"], ACCEPTED)
		self.assertEqual(frappe.db.get_value("Patient", existing, "allergies"), "T- sulfa")
		self.assertEqual(self.open_conflicts(existing), 0)

	# DEFECT-P4-2: a Held record replayed before the Held record it depends on stays Held forever
	@unittest.expectedFailure
	def test_held_chain_replayed_in_dependency_order(self):
		"""DEFECT-P4-2: activity A depends on activity V, which depends on registration R. A arrives first and
		fails ("dependency not received"); then [R, V, A] are sent: R conflict, V held, A retried -> held on the
		same conflict. A's first_received_at is older than V's, and `replay_held` replays in received order:
		A is replayed while V is still Held (held again), then V is accepted. Nothing replays A again: it stays
		Held on a Resolved conflict, a re-send answers "held" without retrying, and the session can never close.
		Expected (conflicts docstring: "the records held on the conflict are replayed"; protocol §6 F6: "records
		still Held on a Resolved conflict are replayed on the next sync.push or sync.status"): A Accepted at the
		latest after the member's next `status` call."""
		reg = self.env(RECORD_PATIENT_REGISTER, self.conflicted_registration()[0]["payload"])
		v = self.env(RECORD_COMMUNITY_ACTIVITY, self.activity_payload(), depends_on=[reg["client_uuid"]])
		a = self.env(RECORD_COMMUNITY_ACTIVITY, self.activity_payload(), depends_on=[v["client_uuid"]])
		self.assertEqual(self.results(self.push([a])), ["failed"])
		out = self.push([reg, v, a])
		self.assertEqual(self.results(out), ["conflict", "held", "held"])
		self.resolve(out["results"][0]["conflict"], KF)
		self.assertEqual(self.rec_state(v["client_uuid"])["status"], ACCEPTED)
		with as_user(self.users["member"]):  # protocol §6 F6: replayed at the latest on the next status call
			answer = sync.status(self.session, [a["client_uuid"]])["results"][0]
		self.assertEqual(
			(answer["result"], self.rec_state(a["client_uuid"])["status"]), ("accepted", ACCEPTED)
		)


# =====================================================================================================
# 3. Sync Conflict decisions
# =====================================================================================================


class TestConflictDecisions(P4Case):
	def test_decision_table_matches_docstring(self):
		self.assertEqual(
			{k: set(v) for k, v in conflicts.DECISIONS.items()},
			{k: set(v) for k, v in EXPECTED_DECISIONS.items()},
		)
		for (ctype, _review), decisions in EXPECTED_DECISIONS.items():
			for decision, record_status in decisions.items():
				self.assertEqual(conflicts.DECISION_EFFECTS[(ctype, decision)][0], record_status)
		self.assertEqual(set(conflicts.TRANSITIONS["resolve"].roles), set(RESOLVERS_ROLES))
		self.assertEqual(set(conflicts.TRANSITIONS["retry"].roles), set(RESOLVERS_ROLES))
		self.assertEqual(conflicts.TRANSITIONS["retry"].from_statuses, (CONFLICT_OPEN,))

	# -- conflict builders -------------------------------------------------------------------------------
	def build(self, kind: str) -> frappe._dict:
		"""An Open conflict of `kind` with (except review) a community.activity held on it."""
		c = frappe._dict(kind=kind, held=[])
		if kind == "dup":
			existing = self.existing()
			c.patient = existing.name
			payload = self.register_payload(
				first_name=existing.first_name,
				last_name=existing.last_name + "e",
				dob="1990-05-01",
				estimated_age_years=None,
				allergies="T- sulfa",
				identifiers=[
					{"identifier_type": IDENTIFIER_UNHCR, "identifier_value": f"T-UN-{random_name()}"},
					{"identifier_type": RATION, "identifier_value": f"T-RC-{random_name()}"},
				],
			)
			env = self.env(RECORD_PATIENT_REGISTER, payload)
			c.ration = payload["identifiers"][1]["identifier_value"].upper()
		elif kind == "demo":
			existing = self.existing()
			c.patient = existing.name
			base = str(frappe.db.get_value("Patient", existing.name, "modified"))
			doc = frappe.get_doc("Patient", existing.name)
			doc.mobile = "0711000222"
			doc.save(ignore_permissions=True)
			env = self.env(
				RECORD_PATIENT_UPDATE,
				{
					"patient": existing.name,
					"base_modified": base,
					"changes": {"mobile": "0722000222", "allergies": "T- sulfa"},
					"add_identifiers": [],
				},
			)
		elif kind == "overlap":
			c.patient = self.existing().name
			c.facility_visit = make_visit(patient=c.patient).name
			env = self.env(RECORD_VISIT_CAPTURE, self.visit_payload(patient=c.patient))
		else:  # review
			c.patient = self.existing().name
			captured = (now_datetime() - timedelta(minutes=1)).replace(microsecond=0)
			c.facility_visit = make_visit(patient=c.patient, system=True, visit_date=captured.date()).name
			self.close_facility_visit(c.facility_visit)
			env = self.env(RECORD_VISIT_CAPTURE, self.visit_payload(patient=c.patient), captured=captured)
		result = self.push([env])["results"][0]
		self.assertEqual(result["result"], "accepted" if kind == "review" else "conflict", result)
		c.record, c.name = env["client_uuid"], result["conflict"]
		if kind != "review":
			held = self.env(RECORD_COMMUNITY_ACTIVITY, self.activity_payload(), depends_on=[c.record])
			self.assertEqual(self.results(self.push([held])), ["held"])
			c.held.append(held["client_uuid"])
		c.expected_open = 0 if kind == "review" else 1
		self.assertEqual(self.open_conflicts(c.patient), c.expected_open)
		return c

	def inputs(self, c, decision: str) -> dict:
		if decision == MERGE:
			return {"chosen_fields": {"allergies": "outreach"}}
		return {}

	def state(self, c) -> dict:
		doc = frappe.get_doc(SYNC_CONFLICT_DOCTYPE, c.name)
		return {
			"status": doc.status,
			"decision": doc.decision,
			"log": len(doc.log),
			"record": self.rec_state(c.record)["status"],
			"held": [self.rec_state(h)["status"] for h in c.held],
			"open": self.open_conflicts(c.patient),
		}

	def offered(self, c, actor: str) -> dict:
		with as_user(self.u(actor)):
			ctx = conflicts.form_context(frappe.get_doc(SYNC_CONFLICT_DOCTYPE, c.name))
		return {"decisions": set(ctx["decisions"]), "retry": bool(ctx["can_retry"])}

	def check_effect(self, c, decision: str, actor: str, result: dict) -> None:
		ctype, review = CONFLICT_KINDS[c.kind]
		expected_record = EXPECTED_DECISIONS[(ctype, review)][decision] or ACCEPTED
		rec = self.rec_state(c.record)
		self.assertEqual((rec["status"], result["record_status"]), (expected_record, expected_record))
		doc = frappe.get_doc(SYNC_CONFLICT_DOCTYPE, c.name)
		self.assertEqual(
			(doc.status, doc.decision, doc.resolved_by, doc.reason),
			(CONFLICT_RESOLVED, decision, self.u(actor), REASON),
		)
		last = doc.log[-1] if c.held == [] else [r for r in doc.log if r.action == "resolve"][-1]
		self.assertEqual(
			(last.action, last.from_status, last.to_status, last.decision, last.user),
			("resolve", CONFLICT_OPEN, CONFLICT_RESOLVED, decision, self.u(actor)),
		)
		for h in c.held:  # replayed after the decision
			self.assertEqual(self.rec_state(h)["status"], ACCEPTED, (c.kind, decision))
		self.assertEqual(self.open_conflicts(c.patient), 0)
		patient = frappe.get_doc("Patient", c.patient)
		if c.kind == "dup":
			if decision == KO:
				self.assertNotEqual(rec["target_name"], c.patient)
				self.assertIn(
					c.name,
					frappe.db.get_value("Patient", rec["target_name"], "krcs_duplicate_override_reason"),
				)
				self.assertEqual(self.open_conflicts(rec["target_name"]), 0)
			else:
				self.assertEqual((rec["target_doctype"], rec["target_name"]), ("Patient", c.patient))
			ration = {r.identifier_value for r in patient.krcs_identifiers if r.identifier_type == RATION}
			self.assertEqual(c.ration in ration, decision in (LINK, MERGE))
			self.assertEqual(patient.allergies == "T- sulfa", decision == MERGE)
		elif c.kind == "demo":
			self.assertEqual(patient.mobile, "0722000222" if decision == KO else "0711000222")
			self.assertEqual(patient.allergies == "T- sulfa", decision in (KO, MERGE))
		elif c.kind == "overlap":
			if decision == KF:
				self.assertEqual(rec["target_name"], c.facility_visit)
				self.assertEqual(frappe.db.count(VISIT_DOCTYPE, {"patient": c.patient}), 1)
			else:
				self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, rec["target_name"], "is_outreach"), 1)
				self.assertEqual(frappe.db.count(VISIT_DOCTYPE, {"patient": c.patient}), 2)
		else:
			self.assertEqual(frappe.db.count(VISIT_DOCTYPE, {"patient": c.patient}), 2)

	def run_kind(self, kind: str) -> None:
		ctype, review = CONFLICT_KINDS[kind]
		valid = EXPECTED_DECISIONS[(ctype, review)]
		for resolver in (*RESOLVERS, ADMIN):
			with self.subTest(kind=kind, resolver=resolver), savepoint("s3_conflict"):
				c = self.build(kind)
				if kind == "overlap":
					# Keep outreach version (and Retry) is refused while the facility visit is open
					before = self.state(c)
					_out, err = self.act_as(resolver, self.resolve_as, c, KO)
					self.assertIsInstance(err, frappe.ValidationError)
					self.assertIn("still has the open visit", str(err))
					self.assertEqual(self.state(c), before)
					self.close_facility_visit(c.facility_visit)
				# Denied actors: offered nothing, every decision and Retry refused, nothing changes
				for actor in NON_RESOLVERS:
					self.assertEqual(self.offered(c, actor), {"decisions": set(), "retry": False}, actor)
					before = self.state(c)
					for decision in CONFLICT_DECISIONS:
						_out, err = self.act_as(actor, self.resolve_as, c, decision)
						self.assertIsInstance(err, frappe.PermissionError, (actor, decision))
					_out, err = self.act_as(actor, conflicts.retry, c.name, REASON)
					self.assertIsInstance(err, frappe.PermissionError, actor)
					self.assertEqual(self.state(c), before, actor)
				# Resolver: every decision tried on the Open conflict (each rolled back); offered == working
				works = set()
				offered = self.offered(c, resolver)
				for decision in CONFLICT_DECISIONS:
					with savepoint("s3_decision"):
						before = self.state(c)
						result, err = self.act_as(resolver, self.resolve_as, c, decision)
						if decision not in valid:
							self.assertIsInstance(err, conflicts.SyncConflictError, decision)
							self.assertEqual(self.state(c), before, decision)
							continue
						self.assertIsNone(err, f"{decision}: {err!r}")
						works.add(decision)
						self.check_effect(c, decision, resolver, result)
						# No second decision, by anyone
						resolved = self.state(c)
						for again in CONFLICT_DECISIONS:
							for other in (*RESOLVERS, ADMIN):
								_out, err = self.act_as(other, self.resolve_as, c, again)
								self.assertIsInstance(err, conflicts.SyncConflictError, (again, other))
						self.assertEqual(self.state(c), resolved)
						self.assertEqual(self.offered(c, resolver), {"decisions": set(), "retry": False})
				self.assertEqual(offered["decisions"], works)
				self.assertEqual(works, set(valid))
				self.assertEqual(offered["retry"], kind == "overlap")

	def resolve_as(self, c, decision: str) -> dict:
		return conflicts.resolve(c.name, decision, REASON, **self.inputs(c, decision))

	def test_duplicate_registration(self):
		self.run_kind("dup")

	def test_demographic_change(self):
		self.run_kind("demo")

	def test_overlapping_visit(self):
		self.run_kind("overlap")

	def test_overlapping_visit_review_only(self):
		self.run_kind("review")

	def test_reason_and_inputs_required(self):
		"""Empty reason, Merge without an outreach field, a duplicate-registration patient that is not a candidate:
		refused, nothing changes, the counter stays 1."""
		c = self.build("dup")
		before = self.state(c)
		other = self.existing().name
		cases = [
			(KF, {"reason": "  "}),
			(MERGE, {"chosen_fields": {"mobile": "facility"}}),
			(MERGE, {"chosen_fields": {}}),
			(LINK, {"patient": other}),
			(MERGE, {"chosen_fields": {"not_a_field": "outreach"}}),
		]
		for decision, kw in cases:
			with self.subTest(decision=decision, kw=kw):
				reason = kw.pop("reason", REASON)
				with as_user(self.users["his"]):
					_out, err = _try(conflicts.resolve, c.name, decision, reason, **kw)
				self.assertIsInstance(err, conflicts.SyncConflictError)
				self.assertEqual(self.state(c), before)

	def test_retry_semantics(self):
		"""Retry (held Overlapping visit only, resolvers only): while the facility visit is open -> not resolved,
		a retry_failed log row, conflict Open, record Conflict, counter 1; denied actors refused without a log
		row; other types refused; after the facility visit closes -> resolved as Keep outreach version, record
		Accepted, held activity replayed, counter 0."""
		c = self.build("overlap")
		for actor in NON_RESOLVERS:
			before = self.state(c)
			_out, err = self.act_as(actor, conflicts.retry, c.name, REASON)
			self.assertIsInstance(err, frappe.PermissionError)
			self.assertEqual(self.state(c), before)
		for actor in RESOLVERS:
			before = self.state(c)
			out, err = self.act_as(actor, conflicts.retry, c.name)
			self.assertIsNone(err, repr(err))
			self.assertEqual((out["resolved"], out["status"]), (False, CONFLICT_OPEN))
			after = self.state(c)
			self.assertEqual({**after, "log": 0}, {**before, "log": 0})
			self.assertEqual(after["log"], before["log"] + 1)
			last = frappe.get_doc(SYNC_CONFLICT_DOCTYPE, c.name).log[-1]
			self.assertEqual(
				(last.action, last.from_status, last.to_status, last.user, last.reason),
				("retry_failed", CONFLICT_OPEN, CONFLICT_OPEN, self.u(actor), conflicts.RETRY_REASON),
			)
			self.assertEqual(self.offered(c, actor)["retry"], True)
		for kind in ("dup", "demo", "review"):
			other = self.build(kind)
			before = self.state(other)
			self.assertFalse(self.offered(other, "his")["retry"])
			_out, err = self.act_as("his", conflicts.retry, other.name)
			self.assertIsInstance(err, conflicts.SyncConflictError, kind)
			self.assertEqual(self.state(other), before)
		self.close_facility_visit(c.facility_visit)
		out, err = self.act_as("mic", conflicts.retry, c.name, REASON)
		self.assertIsNone(err, repr(err))
		self.assertTrue(out["resolved"])
		doc = frappe.get_doc(SYNC_CONFLICT_DOCTYPE, c.name)
		self.assertEqual((doc.status, doc.decision, doc.resolved_by), (CONFLICT_RESOLVED, KO, self.u("mic")))
		self.assertEqual(self.rec_state(c.record)["status"], ACCEPTED)
		self.assertEqual(self.rec_state(c.held[0])["status"], ACCEPTED)
		self.assertEqual(self.open_conflicts(c.patient), 0)
		for decision in CONFLICT_DECISIONS:
			_out, err = self.act_as("his", self.resolve_as, c, decision)
			self.assertIsInstance(err, conflicts.SyncConflictError)

	# DEFECT-P4-3: Retry on a Resolved conflict is answered as a failed attempt and logged on the closed conflict
	@unittest.expectedFailure
	def test_retry_on_resolved_conflict_refused(self):
		"""DEFECT-P4-3: `retry` catches every ValidationError of `_resolve`, including "already resolved", so a
		second Retry (two HIS screens, or Retry after Keep facility version) returns {"resolved": False, "error":
		"... was already resolved"} and appends a "retry_failed" row (Resolved -> Resolved) to the resolved
		conflict's log. TRANSITIONS: retry is only from Open; expected: SyncConflictError, log unchanged."""
		c = self.build("overlap")
		self.resolve(c.name, KF, reason=REASON)
		before = self.state(c)
		_out, err = self.act_as("his", conflicts.retry, c.name, REASON)
		self.assertEqual(self.state(c), before)
		self.assertIsInstance(err, conflicts.SyncConflictError)

	def test_counter_with_several_conflicts(self):
		"""Two demographic conflicts and a review-only overlap on one patient: counter 2 (review-only not counted);
		each decision lowers it by one; denied attempts and refused decisions leave it."""
		patient = self.existing()
		base = str(frappe.db.get_value("Patient", patient.name, "modified"))
		doc = frappe.get_doc("Patient", patient.name)
		doc.mobile = "0711000333"
		doc.save(ignore_permissions=True)
		names = []
		for mobile in ("0722000333", "0733000333"):
			env = self.env(
				RECORD_PATIENT_UPDATE,
				{"patient": patient.name, "base_modified": base, "changes": {"mobile": mobile}},
			)
			names.append(self.push([env])["results"][0]["conflict"])
		captured = (now_datetime() - timedelta(minutes=1)).replace(microsecond=0)
		fv = make_visit(patient=patient.name, system=True, visit_date=captured.date()).name
		self.close_facility_visit(fv)
		review = self.push(
			[self.env(RECORD_VISIT_CAPTURE, self.visit_payload(patient=patient.name), captured=captured)]
		)["results"][0]["conflict"]
		self.assertEqual(self.open_conflicts(patient.name), 2)
		self.act_as("reception", conflicts.resolve, names[0], KF, REASON)
		self.act_as("his", conflicts.resolve, names[0], ACK, REASON)
		self.assertEqual(self.open_conflicts(patient.name), 2)
		self.resolve(names[0], KF, reason=REASON)
		self.assertEqual(self.open_conflicts(patient.name), 1)
		self.resolve(review, ACK, reason=REASON)
		self.assertEqual(self.open_conflicts(patient.name), 1)
		self.resolve(names[1], KO, reason=REASON, user="mic")
		self.assertEqual(self.open_conflicts(patient.name), 0)
		self.assertEqual(frappe.db.get_value("Patient", patient.name, "mobile"), "0733000333")

	def held_visit_of_member2(self) -> tuple[str, str, dict]:
		"""member2's overlapping visit (Conflict) plus an activity held on it: (patient, facility visit, push out)."""
		patient = self.existing().name
		facility_visit = make_visit(patient=patient).name
		visit = self.env(RECORD_VISIT_CAPTURE, self.visit_payload(patient=patient))
		held = self.env(RECORD_COMMUNITY_ACTIVITY, self.activity_payload(), depends_on=[visit["client_uuid"]])
		out = self.push([visit, held], user="member2")
		self.assertEqual(self.results(out), ["conflict", "held"])
		return patient, facility_visit, out

	def keep_outreach_applies_as_member2(self, patient: str, facility_visit: str, out: dict) -> None:
		self.close_facility_visit(facility_visit)
		result = self.resolve(out["results"][0]["conflict"], KO, reason=REASON)
		self.assertEqual(result["record_status"], ACCEPTED)
		self.assertEqual([r["result"] for r in result["replayed"]], ["accepted"])
		new_visit = frappe.get_doc(VISIT_DOCTYPE, result["name"])
		self.assertEqual((new_visit.owner, new_visit.is_outreach), (self.users["member2"], 1))
		self.assertEqual(self.open_conflicts(patient), 0)

	def test_decision_after_member_removed(self):
		"""No record loss: member2's held overlapping visit (and the activity held on it) is still applied as
		member2 (the author) after member2 was taken off the team (grants revoked) before the decision."""
		patient, facility_visit, out = self.held_visit_of_member2()
		doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, self.session)
		doc.set("team", [r for r in doc.team if r.user != self.users["member2"]])
		with as_user(self.users["his"]):
			doc.save()
		self.keep_outreach_applies_as_member2(patient, facility_visit, out)

	# DEFECT-P4-4: Keep outreach version / Retry impossible once the author's account is disabled
	@unittest.expectedFailure
	def test_decision_after_member_disabled(self):
		"""DEFECT-P4-4: the outreach visit is applied as its author (`apply.acting_as(record.user)`), and
		`clinical.practitioner.get_or_create` refuses to create the Healthcare Practitioner of a disabled user
		(PermissionError "User ... is disabled."). A member whose first consultation was the held visit and whose
		account is disabled before the HIS decision (staff left after the outreach) can therefore never have the
		visit applied: Keep outreach version raises a PermissionError at the HIS Officer, Retry logs it as a
		failed attempt, and only Keep facility version (discard) remains. Conflicts docstring: Keep outreach
		version is refused only while the patient still has an open visit; D58: the member stays the author."""
		patient, facility_visit, out = self.held_visit_of_member2()
		frappe.db.set_value("User", self.users["member2"], "enabled", 0)
		self.keep_outreach_applies_as_member2(patient, facility_visit, out)

	def test_conflict_resolvable_after_session_cancelled(self):
		"""No record loss: a Planned session (records may sync while Planned) can be cancelled with a Conflict
		record (the cancel rule has no record check); the conflict is still resolved and its held record
		replayed afterwards."""
		c = self.build("dup")
		with as_user(self.users["his"]):
			sessions.apply_action(self.session, "cancel", "T- rain")
		self.assertEqual(self.session_status(self.session), CANCELLED)
		self.resolve(c.name, KF, reason=REASON)
		self.assertEqual(self.rec_state(c.record)["status"], DISCARDED)
		self.assertEqual(self.rec_state(c.held[0])["status"], ACCEPTED)
		self.assertEqual(self.open_conflicts(c.patient), 0)


# =====================================================================================================
# 4. Device grants
# =====================================================================================================

# State -> (auth hook accepts the grant on sync.push, re-enrol outcome, recover_key on the grant). Re-enrol:
# "same" (grant refreshed, new secret), "new" (a new grant next to the revoked one), or the exception class;
# None: not applicable (a disabled user cannot sign in online) or checked in a DEFECT test. Protocol §1-2 and
# §6 (F1 closed sessions, F6 role check) + devices / auth docstrings.
EXPECTED_DEVICE: dict[str, tuple[bool, object, object]] = {
	"planned": (True, "same", True),
	"active": (True, "same", True),
	"returned": (True, "same", True),
	"expired": (False, "same", True),
	"grant revoked": (False, "new", frappe.PermissionError),
	"device revoked": (False, frappe.PermissionError, frappe.PermissionError),
	"session closed": (None, None, True),  # grant and re-enrol: protocol §6 F1, DEFECT-P4-6
	"session cancelled": (False, frappe.ValidationError, True),
	"removed from team": (False, frappe.PermissionError, frappe.PermissionError),
	"re-added to team": (False, "new", frappe.PermissionError),
	"user disabled": (False, None, None),
}


class TestDeviceGrants(P4Case):
	def enrol(self, session: str, device_id: str, key: str, user: str = "member2") -> dict:
		with as_user(self.users[user]):
			return devices.enrol(session, device_id, key, "T- s3 tablet")

	def header(self, grant: str, secret: str) -> str:
		return f"{OUTREACH_AUTH_SCHEME} {grant}:{secret}"

	def authenticates(self, grant: str, secret: str) -> tuple[bool, object]:
		with fake_request(self.header(grant, secret)):
			_out, err = _try(auth.authenticate_device)
			user = frappe.session.user
		if err is not None:
			self.assertIsInstance(err, frappe.AuthenticationError)
			return False, err
		self.assertEqual(user, self.users["member2"])
		return True, None

	def put_in(self, state: str, session: str, res: dict) -> None:
		grant = res["grant"]
		device = frappe.db.get_value(OUTREACH_GRANT_DOCTYPE, grant, "device")
		if state == "active":
			self.move_session(session, ACTIVE)
		elif state == "returned":
			self.move_session(session, RETURNED)
		elif state == "expired":
			frappe.db.set_value(
				OUTREACH_GRANT_DOCTYPE, grant, "expires_at", add_to_date(now_datetime(), seconds=-1)
			)
		elif state == "grant revoked":
			devices.revoke_grants({"name": grant}, "T- s3")
		elif state == "device revoked":
			with as_user(self.users["his"]):
				devices.revoke_device(device, "T- lost")
		elif state == "session closed":
			self.move_session(session, CLOSED)
		elif state == "session cancelled":
			self.move_session(session, CANCELLED)
		elif state in ("removed from team", "re-added to team"):
			self.move_session(session, ACTIVE)
			doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, session)
			doc.set("team", [r for r in doc.team if r.user != self.users["member2"]])
			with as_user(self.users["his"]):
				doc.save()
			if state == "re-added to team":
				doc.reload()
				doc.append("team", {"user": self.users["member2"], "is_lead": 0})
				with as_user(self.users["his"]):
					doc.save()
		elif state == "user disabled":
			frappe.db.set_value("User", self.users["member2"], "enabled", 0)

	def test_grant_state_matrix(self):
		for state, (accepts, reenrol, recover) in EXPECTED_DEVICE.items():
			with self.subTest(state=state), savepoint("s3_device"):
				session = self.new_session(PLANNED)
				device_id, key = new_device_id(), new_key()
				res = self.enrol(session, device_id, key)
				self.assertTrue(self.authenticates(res["grant"], res["secret"])[0])
				self.put_in(state, session, res)
				if accepts is not None:
					ok, err = self.authenticates(res["grant"], res["secret"])
					self.assertEqual(ok, accepts, repr(err))
				frappe.local.flags.commit = False
				# recover_key on the grant (before re-enrolling)
				if recover is not None:
					out, err = self.act_as("member2", devices.recover_key, res["grant"])
					if recover is True:
						self.assertIsNone(err, repr(err))
						self.assertEqual(out["data_key"], key)
					else:
						self.assertIsInstance(err, recover)
					frappe.local.flags.commit = False
					# never for another user
					_out, err = self.act_as("member", devices.recover_key, res["grant"])
					self.assertIsInstance(err, frappe.DoesNotExistError)
				# re-enrol the same device
				if reenrol is None:
					continue
				new, err = self.act_as("member2", devices.enrol, session, device_id, key, "T- again")
				if isinstance(reenrol, type):
					self.assertIsInstance(err, reenrol)
					self.assertFalse(self.authenticates(res["grant"], res["secret"])[0])
					continue
				self.assertIsNone(err, repr(err))
				self.assertEqual(new["grant"] == res["grant"], reenrol == "same")
				self.assertTrue(self.authenticates(new["grant"], new["secret"])[0])
				self.assertFalse(self.authenticates(res["grant"], res["secret"])[0])  # old secret / grant
				self.assertGreater(
					frappe.utils.get_datetime(new["expires_at"]), now_datetime() + timedelta(days=1)
				)
				live = frappe.get_all(
					OUTREACH_GRANT_DOCTYPE,
					filters={
						"device": device_id,
						"user": self.users["member2"],
						"session": session,
						"revoked": 0,
					},
					pluck="name",
				)
				self.assertEqual(live, [new["grant"]])  # one live grant per user, device and session

	def test_grant_syncs_only_its_session_and_device(self):
		"""A grant authenticated request may push into its own session and device only."""
		other = self.new_session(PLANNED)
		res = self.enrol(self.session, "t-dev-s3-0001", new_key())
		env = self.env(RECORD_COMMUNITY_ACTIVITY, self.activity_payload(), device_id="t-dev-s3-0001")
		with fake_request(self.header(res["grant"], res["secret"])):
			auth.authenticate_device()
			out, err = _try(sync.push, self.session, "t-dev-s3-0001", new_uuid(), [env])
			_o2, err_session = _try(sync.push, other, "t-dev-s3-0001", new_uuid(), [])
			_o3, err_device = _try(sync.push, self.session, "t-dev-s3-0002", new_uuid(), [])
			_o4, err_enrol = _try(devices.enrol, self.session, "t-dev-s3-0001", new_key())
		self.assertIsNone(err, repr(err))
		self.assertEqual(out["results"][0]["result"], "accepted")
		self.assertIsInstance(err_session, frappe.PermissionError)
		self.assertIsInstance(err_device, frappe.PermissionError)
		self.assertIsInstance(err_enrol, frappe.PermissionError)

	# DEFECT-P4-6: protocol §6 F1, a Closed session still syncs its records through the grant
	@unittest.expectedFailure
	def test_closed_session_still_syncs(self):
		"""DEFECT-P4-6: protocol §6 F1 ("Closed means no new work"): on a Closed session the grant keeps working for
		sync.push / sync.status until expires_at (records captured during the session are accepted), `enrol`
		renews an existing grant for the same user and device (new secret, new expiry) but refuses a new device,
		and the package endpoints are refused. Cancelled refuses everything. Code: `devices.verify_grant` refuses
		any session outside SESSION_OPEN_STATUSES (401), and `enrol` / `sync.push` call `assert_open`."""
		device_id = "t-dev-s3-closed1"
		res = self.enrol(self.session, device_id, new_key())
		cancelled = self.new_session(PLANNED)
		res_c = self.enrol(cancelled, "t-dev-s3-cancel1", new_key())
		self.move_session(self.session, CLOSED)
		self.move_session(cancelled, CANCELLED)
		env = self.env(RECORD_COMMUNITY_ACTIVITY, self.activity_payload(), device_id=device_id)
		# Cancelled: everything refused
		self.assertFalse(self.authenticates(res_c["grant"], res_c["secret"])[0])
		_out, err = self.act_as("member2", devices.enrol, cancelled, "t-dev-s3-cancel1", new_key())
		self.assertIsNotNone(err)
		# Closed: package refused, push / status accepted
		with fake_request(
			self.header(res["grant"], res["secret"]),
			path="/api/method/onerc_healthcare.outreach.package.get_patients",
		):
			_out, err = _try(auth.authenticate_device)
		self.assertIsInstance(err, frappe.AuthenticationError)
		with fake_request(self.header(res["grant"], res["secret"])):
			_out, err = _try(auth.authenticate_device)
			self.assertIsNone(err, repr(err))
			out, err = _try(sync.push, self.session, device_id, new_uuid(), [env])
		self.assertIsNone(err, repr(err))
		self.assertEqual(out["results"][0]["result"], "accepted")
		# Renewal of the same device's grant allowed; a new device refused
		frappe.db.set_value(
			OUTREACH_GRANT_DOCTYPE, res["grant"], "expires_at", add_to_date(now_datetime(), seconds=-1)
		)
		new, err = self.act_as("member2", devices.enrol, self.session, device_id, new_key())
		self.assertIsNone(err, repr(err))
		self.assertEqual(new["grant"], res["grant"])
		_out, err = self.act_as("member2", devices.enrol, self.session, new_device_id(), new_key())
		self.assertIsNotNone(err)

	# DEFECT-P4-7: protocol §6 F6, the grant and push are refused once the Outreach role is removed
	@unittest.expectedFailure
	def test_role_removed_ends_sync(self):
		"""DEFECT-P4-7: protocol §6 F6 ("the grant is refused (401), and push is refused, once the user no longer
		holds the Outreach Team Member role"). `verify_grant` checks enabled user and team membership only, and
		`sync.push` checks membership only: a member whose role was withdrawn (still listed on the team) keeps
		syncing through the grant and through an online sign-in."""
		res = self.enrol(self.session, "t-dev-s3-role01", new_key())
		make_user("or-member2", [])  # role withdrawn, user still enabled and on the team
		self.assertFalse(self.authenticates(res["grant"], res["secret"])[0])
		env = self.env(RECORD_COMMUNITY_ACTIVITY, self.activity_payload())
		with self.assertRaises(frappe.PermissionError):
			self.push([env], user="member2")
		self.assertFalse(frappe.db.exists(OUTREACH_RECORD_DOCTYPE, env["client_uuid"]))

	def test_enrol_by_actor(self):
		"""Only team members (outreach role) enrol; outsider and staff are refused."""
		for actor in SESSION_ACTORS:
			with self.subTest(actor=actor), savepoint("s3_enrol"):
				_out, err = self.act_as(actor, devices.enrol, self.session, new_device_id(), new_key(), "T-")
				if actor in ("member", "member2"):
					self.assertIsNone(err, repr(err))
				else:
					self.assertIsInstance(err, frappe.PermissionError)

	def test_revoke_device_by_actor(self):
		"""revoke_device: HIS Officer, Medical In-Charge, EMR System Administrator (and Administrator); never a
		team lead, member, outsider, receptionist or clinician. Revoking twice keeps the device Revoked."""
		for actor in SESSION_ACTORS:
			with self.subTest(actor=actor), savepoint("s3_revoke"):
				res = self.enrol(self.session, new_device_id(), new_key())
				device = frappe.db.get_value(OUTREACH_GRANT_DOCTYPE, res["grant"], "device")
				out, err = self.act_as(actor, devices.revoke_device, device, "T- lost")
				status = frappe.db.get_value(OUTREACH_DEVICE_DOCTYPE, device, "status")
				revoked = frappe.db.get_value(OUTREACH_GRANT_DOCTYPE, res["grant"], "revoked")
				if actor in ("his", "mic", "sysadmin", ADMIN):
					self.assertIsNone(err, repr(err))
					self.assertEqual((status, revoked, out["grants_revoked"]), (DEVICE_REVOKED, 1, 1))
					again, err = self.act_as(actor, devices.revoke_device, device, "T- again")
					self.assertIsNone(err, repr(err))
					self.assertEqual((again["status"], again["grants_revoked"]), (DEVICE_REVOKED, 0))
				else:
					self.assertIsInstance(err, frappe.PermissionError)
					self.assertEqual((status, revoked), (DEVICE_ACTIVE, 0))
					self.assertTrue(self.authenticates(res["grant"], res["secret"])[0])
