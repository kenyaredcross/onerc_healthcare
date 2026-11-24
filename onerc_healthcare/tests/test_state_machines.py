# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""S1 state-machine and edge-case audit (post Phase 2 build). Never commit.

Every state machine of the EMR is driven through its own API from every reachable state, by every actor,
and compared with an independent oracle (`tests.sm_helpers`). Known defects are kept as
``@unittest.expectedFailure`` tests that assert the *correct* behaviour (comment ``DEFECT-n``), so they turn
into unexpected successes once fixed; fixed ones stay as plain regression tests (``DEFECT-n (fixed)``).

Machines: Patient Visit (engine + tasks), Referral, Data Access Request, Consent Statement, Patient Consent
outcomes, dispensing line states, Visitor Record, walk-in identifiers / merge, and the Marley couplings
(Encounter, Service Request, Medication Request, Sample Collection, Observation, Diagnostic Report).
"""

from __future__ import annotations

import unittest
from collections import defaultdict
from typing import ClassVar
from unittest.mock import patch

import frappe
from frappe.utils import add_days, today
from healthcare.healthcare.doctype.observation.observation import record_observation_result

from onerc_healthcare.clinical import referral as ref
from onerc_healthcare.clinical.consultation import (
	ConsultationError,
	complete_consultation,
	start_consultation,
)
from onerc_healthcare.clinical.events import EncounterGuardError
from onerc_healthcare.clinical.referral import IllegalReferralTransitionError
from onerc_healthcare.constants import (
	DATA_ACCESS_REQUEST_DOCTYPE,
	DEPT_MENTAL_HEALTH,
	DEPT_OUTPATIENT,
	DISPENSING_DOCTYPE,
	ENCOUNTER_DOCTYPE,
	IDENTIFIER_FACILITY,
	IDENTIFIER_POL,
	PATIENT_IDENTIFIERS_FIELD,
	REFERRAL_DOCTYPE,
	REFERRAL_FACILITY_DOCTYPE,
	ROLE_HIS,
	ROLE_MIC,
	ROLE_PHARMACIST,
	ROLE_RECEPTIONIST,
	ROLE_SYSADMIN,
	VISIT_DOCTYPE,
	VISITOR_DOCTYPE,
)
from onerc_healthcare.emr_data_protection.doctype.patient_consent.patient_consent import PatientConsentError
from onerc_healthcare.emr_visit.doctype.patient_visit.patient_visit import OpenVisitExistsError
from onerc_healthcare.frontdesk import visitor
from onerc_healthcare.frontdesk.visitor import IllegalVisitorTransitionError
from onerc_healthcare.lab import events as lab_events
from onerc_healthcare.pharmacy import dispensing
from onerc_healthcare.pharmacy.dispensing import LineResolvedError, OverDispenseError
from onerc_healthcare.privacy import consent, dar, statements
from onerc_healthcare.privacy.lifecycle import IllegalTransitionError
from onerc_healthcare.registration import api as reg_api
from onerc_healthcare.registration.identifiers import (
	MERGED_INTO_FIELD,
	REGISTRATION_FACILITY_FIELD,
	FacilityIdentifierError,
)
from onerc_healthcare.tests import sm_helpers as sm
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import add_diagnosis, add_lab_row, fill_mh_note, routed_visit
from onerc_healthcare.tests.factories import get_facility, make_patient, make_user
from onerc_healthcare.tests.sm_helpers import (
	ADMIN,
	AL,
	AP,
	IC,
	IT,
	RD,
	REF,
	SYSTEM,
	D,
	R,
	as_user,
	savepoint,
	violations,
	visit_state,
)
from onerc_healthcare.tests.stock_factories import dispensed_item, stock_for_dispensing
from onerc_healthcare.tests.test_lab import HB, MRDT, LabTestCase
from onerc_healthcare.tests.visit_factories import make_submitted_encounter, make_visit, make_vital_signs
from onerc_healthcare.visit import engine, tasks
from onerc_healthcare.visit.engine import apply_action, transition

PARA = "Paracetamol 500 mg Tablet"  # provisional catalogue (BID x 5 Day = 10)
FACILITY = "T-Kakuma Mission Hospital"


def _exc_matches(err: BaseException | None, expected) -> bool:
	if err is None:
		return False
	if isinstance(expected, tuple):
		return isinstance(err, expected)
	return type(err) is expected


def _always_true(visit: str) -> bool:
	"""Patched TASK_CHECKERS entry for the forged-state reconciler run."""
	return True


class SMCase(EMRTestCase):
	"""Common setup: one user per role, the clinician's practitioner, a referral facility."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users = sm.actor_users()
		cls.users["pharmacist2"] = make_user("pv-pharmacist2", [ROLE_PHARMACIST])

	def setUp(self):
		super().setUp()
		from onerc_healthcare.clinical import practitioner

		for key in ("clinician", "mho"):
			with as_user(self.users[key]):
				practitioner.get_or_create()
		if not frappe.db.exists(REFERRAL_FACILITY_DOCTYPE, FACILITY):
			frappe.get_doc(
				{
					"doctype": REFERRAL_FACILITY_DOCTYPE,
					"facility_name": FACILITY,
					"facility_type": "Hospital",
					"county": "Turkana",
				}
			).insert(ignore_permissions=True)

	def u(self, actor: str) -> str:
		return ADMIN if actor == ADMIN else self.users[actor]

	def human_actors(self) -> list[str]:
		return [ADMIN, *sm.ACTOR_ROLES]

	def all_actors(self) -> list[str]:
		return [SYSTEM, *self.human_actors()]

	# -- consultation builders (real endpoints) ------------------------------------------------------
	def consulting(self, user: str = "clinician"):
		"""Visit In Consultation with its draft encounter, through `start_consultation`."""
		visit = routed_visit(DEPT_OUTPATIENT)
		with as_user(self.users[user]):
			enc = start_consultation(visit.name)["encounter"]
		return visit.name, enc

	def add_para(self, encounter: str) -> None:
		doc = frappe.get_doc(ENCOUNTER_DOCTYPE, encounter)
		doc.append(
			"drug_prescription",
			{
				"medication": PARA,
				"drug_code": PARA,
				"dosage": "BID",
				"period": "5 Day",
				"dosage_form": frappe.db.get_value("Medication", PARA, "dosage_form"),
			},
		)
		doc.save(ignore_permissions=True)

	def complete(self, encounter: str, reason: str | None = None, user: str = "clinician") -> dict:
		fill_mh_note(encounter)  # D63
		with as_user(self.users[user]):
			return complete_consultation(encounter, reason=reason, no_diagnosis_reason="T- auditor")

	def mrs(self, encounter: str) -> list[str]:
		return frappe.get_all(
			"Medication Request", filters={"order_group": encounter, "docstatus": 1}, pluck="name"
		)

	def dispense(self, mr: str, qty=None, outcome="Dispensed", final=0, user="pharmacist", **kw):
		if outcome == "Dispensed":
			stock_for_dispensing(mr, qty, item=dispensed_item(kw))  # Phase 6S
		with as_user(self.users[user]):
			doc = frappe.get_doc(
				{
					"doctype": DISPENSING_DOCTYPE,
					"medication_request": mr,
					"outcome": outcome,
					"quantity": qty,
					"final": final,
					**kw,
				}
			).insert()
			doc.submit()
		return doc

	def assert_reachable(self, visit: str, msg: str = "") -> sm.VState:
		s = visit_state(visit)
		self.assertEqual(violations(s), set(), f"{msg} {s}")
		return s


# =====================================================================================================
# Patient Visit engine
# =====================================================================================================


class TestVisitMachine(SMCase):
	def setUp(self):
		super().setUp()
		# Any existing Referral satisfies `receive_referral` (it checks existence only).
		_visit, enc = self.consulting()
		add_diagnosis(enc)
		with as_user(self.users["clinician"]):
			self.referral = ref.create_referral(
				enc, "Internal", "T- auditor", to_department=DEPT_MENTAL_HEALTH
			)["name"]

	# -- table ---------------------------------------------------------------------------------------
	def test_transition_table_matches_spec(self):
		"""The engine's TRANSITIONS equal the SDD §4.4 table the oracle is written from."""
		for action, rule in engine.TRANSITIONS.items():
			self.assertIn(action, sm.EXPECTED_VISIT, action)
			statuses, _roles = sm.EXPECTED_VISIT[action]
			self.assertEqual(set(rule.from_statuses), set(statuses), action)
			expected_roles = sm.rule_roles(action)
			if expected_roles == SYSTEM:
				self.assertTrue(rule.is_system, action)
			else:
				self.assertEqual(set(rule.roles), set(expected_roles), action)

	# -- attempt + compare ---------------------------------------------------------------------------
	def attempt(self, name: str, action: str, actor: str, kw: dict):
		rule = engine.TRANSITIONS[action]
		if actor == SYSTEM:
			return transition(name, action, system=True, **kw)
		with as_user(self.u(actor)):
			if rule.is_system:
				return transition(name, action, **kw)
			return apply_action(
				name,
				action,
				department=kw.get("department"),
				comment=kw.get("comment"),
				without_vitals_reason=kw.get("without_vitals_reason"),
				closure_reason=kw.get("closure_reason"),
			)

	def check_step(self, s: sm.VState, action: str, exp: sm.Outcome, err, kw: dict) -> sm.VState | None:
		if not exp.ok:
			self.assertTrue(_exc_matches(err, exp.exc), f"expected {exp.exc}, got {err!r}")
			after = visit_state(s.name)
			self.assertEqual(
				(after.status, after.lab, after.ph, after.log_len), (s.status, s.lab, s.ph, s.log_len)
			)
			return None
		self.assertIsNone(err, f"expected success, got {err!r}")
		ns = visit_state(s.name)
		self.assertEqual((ns.status, ns.lab, ns.ph), (exp.status, exp.lab, exp.ph))
		self.assertEqual(ns.enc, s.enc)
		if exp.dept:
			self.assertEqual(ns.dept, exp.dept)
		if exp.rwv is not None:
			self.assertEqual(ns.rwv, exp.rwv)
		self.assertGreaterEqual(ns.rwv, s.rwv, "routed_without_vitals is sticky")
		if s.closed_at:
			self.assertEqual(ns.closed_at, s.closed_at, "closed_at never changes once set")
		if exp.closure:
			self.assertEqual(ns.closure_reason, sm.CLOSURE_REASON)
		if action == "receive_referral":
			self.assertEqual(ns.referred_from, kw["referral"])
		self.assertEqual(ns.log_len, s.log_len + 1)
		self.assertEqual(
			(ns.log_last["from"], ns.log_last["to"], ns.log_last["action"]), (s.status, ns.status, action)
		)
		self.assertEqual(violations(ns), set(), "invariants after a legal step")
		return ns

	# -- pseudo-actions through the real endpoints ---------------------------------------------------
	def pseudo_actions(self, s: sm.VState) -> list[str]:
		out = []
		if s.status in (RD, IC) and s.enc == "none":
			out.append("ep_start")
		if s.enc == "draft":
			out.append("ep_complete")
			if not s.closed:
				out.append("ep_complete_rx")
		if s.status in (R, IT) and not s.vitals:
			out.append("vitals")
		return out

	def run_pseudo(self, s: sm.VState, p: str):
		if p == "ep_start":
			with as_user(self.users["clinician"]):
				return start_consultation(s.name)
		if p == "vitals":
			patient = frappe.db.get_value(VISIT_DOCTYPE, s.name, "patient")
			return make_vital_signs(patient, visit=s.name, submit=True)
		encounter = frappe.db.get_value(VISIT_DOCTYPE, s.name, "encounter")
		if p == "ep_complete_rx":
			self.add_para(encounter)
		return self.complete(encounter, reason="T- results pending" if s.lab else None)

	def expect_pseudo(self, s: sm.VState, p: str) -> dict | type:
		if p == "ep_start":
			return {"status": IC, "lab": s.lab, "ph": s.ph, "enc": "draft"}
		if p == "vitals":
			return {"status": IT, "lab": s.lab, "ph": s.ph, "enc": s.enc}
		if s.closed:
			return ConsultationError
		ph = 1 if p == "ep_complete_rx" else s.ph
		return {"status": sm.completed_status(s.lab, ph), "lab": s.lab, "ph": ph, "enc": "submitted"}

	# -- exploration ---------------------------------------------------------------------------------
	def explore(self, name: str, visited: set, actors: list[str], depth: int = 0, report: dict | None = None):
		s = visit_state(name)
		visited.add(s.key())
		if report is not None:
			report["states"] += 1
		self.assertEqual(violations(s), set(), f"reachable state {s.key()}")
		sp = f"sm_d{depth}"
		works: dict[str, set] = defaultdict(set)
		for action in engine.TRANSITIONS:
			for actor in actors:
				kw = sm.visit_inputs(s, action, self.referral)
				via_api = actor != SYSTEM and not engine.TRANSITIONS[action].is_system
				exp = sm.expect_visit(s, action, actor, kw, via_api=via_api)
				with self.subTest(state=s.key(), action=action, actor=actor), savepoint(sp):
					if report is not None:
						report["combinations"] += 1
					err = None
					try:
						self.attempt(name, action, actor, kw)
					except Exception as e:
						err = e
					ns = self.check_step(s, action, exp, err, kw)
					if ns is not None and actor != SYSTEM and not engine.TRANSITIONS[action].is_system:
						works[actor].add(action)
					if ns is not None and actor == SYSTEM and ns.key() not in visited:
						self.explore(name, visited, actors, depth + 1, report)
		# Inputs left out: the refusal is the documented one.
		if SYSTEM in actors:
			for action in ("route", "complete_consultation"):
				kw = sm.visit_inputs(s, action, self.referral, with_reason=False)
				exp = sm.expect_visit(s, action, SYSTEM, kw)
				with self.subTest(state=s.key(), action=action, inputs="without reason"), savepoint(sp):
					err = None
					try:
						self.attempt(name, action, SYSTEM, kw)
					except Exception as e:
						err = e
					self.check_step(s, action, exp, err, kw)
		# Offered actions == actions that work, per human actor.
		doc = frappe.get_doc(VISIT_DOCTYPE, name)
		for actor in actors:
			if actor == SYSTEM:
				continue
			with self.subTest(state=s.key(), offered_for=actor):
				offered = sm.human_offered(doc, self.u(actor))
				self.assertEqual(set(offered), works[actor])
				for action, entry in offered.items():
					rule = engine.TRANSITIONS[action]
					needs = rule.needs_comment or (action == "complete_consultation" and bool(s.lab))
					self.assertEqual(entry["needs_comment"], needs, action)
					self.assertEqual(entry["needs_department"], rule.needs_department, action)
					self.assertEqual(entry["needs_closure_reason"], rule.needs_closure_reason, action)
		# Pseudo-actions (real endpoints) lead to the encounter / vitals states.
		for p in self.pseudo_actions(s):
			expected = self.expect_pseudo(s, p)
			with self.subTest(state=s.key(), pseudo=p), savepoint(sp):
				err = None
				try:
					self.run_pseudo(s, p)
				except Exception as e:
					err = e
				if not isinstance(expected, dict):
					self.assertTrue(_exc_matches(err, expected), f"{p}: {err!r}")
					continue
				self.assertIsNone(err, f"{p}: {err!r}")
				ns = visit_state(name)
				self.assertEqual(
					{"status": ns.status, "lab": ns.lab, "ph": ns.ph, "enc": ns.enc}, expected, p
				)
				self.assertEqual(violations(ns), set(), p)
				if ns.key() not in visited:
					self.explore(name, visited, actors, depth + 1, report)

	def test_exhaustive_matrix_from_reachable_states(self):
		"""Every action x every actor (system, Administrator, each role) from every reachable state,
		reached through the engine and the real consultation endpoints; invariants after each legal step;
		offered actions == actions that work."""
		visit = make_visit()
		visited: set = set()
		report = {"states": 0, "combinations": 0}
		self.explore(visit.name, visited, self.all_actors(), report=report)
		statuses = {k[0] for k in visited}
		self.assertEqual(statuses, set(sm.OPEN) | set(sm.CLOSED), "every status reached")
		encounters = {(k[0], k[3]) for k in visited}
		for status in (IC, AL, AP, D, REF):
			self.assertIn((status, "draft"), encounters)
		for status in (AL, AP, D, REF):
			self.assertIn((status, "submitted"), encounters)
		self.assertGreaterEqual(report["states"], 25)
		print(f"\n[S1] visit matrix: {report['states']} states, {report['combinations']} combinations")

	def test_routed_without_vitals_branch(self):
		"""The emergency-routed branch (routed_without_vitals=1) explored as the system: the flag stays set
		through every reachable state."""
		visit = make_visit()
		transition(visit.name, "start_triage", system=True)
		transition(
			visit.name, "route", system=True, department=DEPT_OUTPATIENT, without_vitals_reason="T- emergency"
		)
		self.assertEqual(visit_state(visit.name).rwv, 1)
		visited: set = set()
		report = {"states": 0, "combinations": 0}
		self.explore(visit.name, visited, [SYSTEM], depth=0, report=report)
		self.assertIn(D, {k[0] for k in visited})

	# -- forged states -------------------------------------------------------------------------------
	def forged(self):
		"""(label, visit, values to forge). Built through the engine, then flags forged with db.set_value."""

		def base(status: str, encounter: str = "none"):
			v = make_visit()
			if status == R:
				return v.name
			transition(v.name, "start_triage", system=True)
			if status == IT:
				return v.name
			transition(v.name, "route", system=True, department=DEPT_OUTPATIENT, without_vitals_reason="T-")
			transition(v.name, "start_consultation", system=True)
			if encounter != "none":
				make_submitted_encounter(v.patient, v.name, submit=encounter == "submitted")
			if status == AL:
				transition(v.name, "request_lab", system=True)
			elif status == AP:
				transition(v.name, "prescribe", system=True)
			elif status == D:
				transition(v.name, "close_visit", system=True, closure_reason="Other", comment="T-")
			elif status == REF:
				transition(v.name, "refer", system=True, comment="T-")
			return v.name

		disabled = base(IC, "draft")
		frappe.db.set_value(
			"Patient", frappe.db.get_value(VISIT_DOCTYPE, disabled, "patient"), "status", "Disabled"
		)
		return [
			("AP without pharmacy_open", base(AP), {"pharmacy_open": 0}),
			("AL without lab_open", base(AL), {"lab_open": 0}),
			("Discharged with lab_open", base(D), {"lab_open": 1}),
			("Discharged with pharmacy_open", base(D), {"pharmacy_open": 1}),
			("Discharged without closed_at", base(D), {"closed_at": None}),
			("In Consultation with lab_open", base(IC, "draft"), {"lab_open": 1}),
			("completed but In Consultation", base(IC, "submitted"), {}),
			("completed In Consultation with pharmacy", base(IC, "submitted"), {"pharmacy_open": 1}),
			("Registered with lab_open", base(R), {"lab_open": 1}),
			("In Triage with pharmacy_open", base(IT), {"pharmacy_open": 1}),
			("Referred without closed_at", base(REF), {"closed_at": None, "lab_open": 1}),
			("AP with lab and pharmacy before completion", base(AP), {"lab_open": 1}),
			(
				"completed AL with pharmacy",
				base(IC, "submitted"),
				{"status": AL, "lab_open": 1, "pharmacy_open": 1},
			),
			("disabled patient, open visit", disabled, {"lab_open": 1, "status": AL}),
		]

	def test_forged_states_system_events_never_make_things_worse(self):
		"""Forged flag combinations the engine cannot produce: raise_event never raises, closed visits stay
		closed, and the set of violated invariants never grows. User actions never reopen a closed visit."""
		system_events = [a for a, r in engine.TRANSITIONS.items() if r.is_system]
		for label, name, values in self.forged():
			if values:
				frappe.db.set_value(VISIT_DOCTYPE, name, values, update_modified=False)
			before = visit_state(name)
			for action in system_events:
				kw = (
					{"department": DEPT_MENTAL_HEALTH, "referral": self.referral}
					if action == "receive_referral"
					else {}
				)
				with self.subTest(forged=label, event=action), savepoint("sm_forged"):
					try:
						result = tasks.raise_event(name, action, **kw)
					except Exception as e:
						self.fail(f"raise_event leaked {e!r}")
					self.assertIsInstance(result, bool)
					after = visit_state(name)
					if before.closed:
						self.assertEqual(after.status, before.status)
						if before.closed_at:
							self.assertEqual(after.closed_at, before.closed_at)
					self.assertLessEqual(violations(after), violations(before), f"{before} -> {after}")
			if before.closed:
				for action in engine.TRANSITIONS:
					kw = sm.visit_inputs(before, action, self.referral)
					with self.subTest(forged=label, user_action=action), savepoint("sm_forged"):
						try:
							transition(name, action, **kw)
						except frappe.ValidationError:
							pass
						self.assertEqual(visit_state(name).status, before.status)

	def test_forged_states_reconciler(self):
		"""The reconciler with checkers that always report done: no exception escapes and closed (even
		forged) visits are never touched."""
		forged = self.forged()
		for _label, name, values in forged:
			if values:
				frappe.db.set_value(VISIT_DOCTYPE, name, values, update_modified=False)
		before = {name: visit_state(name) for _l, name, _v in forged}
		path = "onerc_healthcare.tests.test_state_machines._always_true"
		with patch.object(tasks, "TASK_CHECKERS", {"lab": path, "pharmacy": path}):
			tasks.reconcile_open_tasks()
		for label, name, _values in forged:
			after = visit_state(name)
			with self.subTest(forged=label):
				if before[name].status == D:
					self.assertEqual(
						(after.status, after.lab, after.ph), (D, before[name].lab, before[name].ph)
					)
				if before[name].closed:
					self.assertEqual(after.status, before[name].status)
				self.assertLessEqual(violations(after), violations(before[name]))

	# -- known defects ---------------------------------------------------------------------------------
	# DEFECT-1 (fixed by `Transition.api_only`; kept as a regression guard): the engine's `refer` is accepted from `apply_action` (the visit form's POST endpoint): the
	# visit closes as Referred without a Referral record (BR-14) and abandons its draft encounter.
	def test_defect1_refer_via_apply_action_refused(self):
		visit, enc = self.consulting()
		with as_user(self.users["clinician"]):
			offered = {
				a["action"] for a in engine.get_available_actions(frappe.get_doc(VISIT_DOCTYPE, visit))
			}
			self.assertNotIn("refer", offered)
			with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
				apply_action(visit, "refer", comment="T- direct refer")
		self.assertEqual(visit_state(visit).status, IC)
		self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "docstatus"), 0)

	# DEFECT-1 (fixed, regression guard): `start_consultation` and `complete_consultation` are also accepted from `apply_action`, which
	# bypasses the consultation endpoints (no encounter created / no encounter submitted by the call).
	def test_defect1_consultation_actions_via_apply_action_refused(self):
		routed = routed_visit(DEPT_OUTPATIENT)
		with as_user(self.users["clinician"]):
			offered = {
				a["action"] for a in engine.get_available_actions(frappe.get_doc(VISIT_DOCTYPE, routed.name))
			}
			self.assertNotIn("start_consultation", offered)
			with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
				apply_action(routed.name, "start_consultation")
		self.assertEqual(visit_state(routed.name).status, RD)
		visit, enc = self.consulting()
		self.add_para(enc)
		self.complete(enc)
		self.assertEqual(visit_state(visit).status, AP)
		with as_user(self.users["clinician"]):
			with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
				apply_action(visit, "complete_consultation")

	# DEFECT-1b (fixed: discharge is server-only; regression guard): `discharge` through `apply_action` closes a visit that has no consultation at all (no
	# encounter): Discharged without a consultation record. Discharge is to stop being a user action.
	def test_defect1b_discharge_via_apply_action_refused(self):
		visit = routed_visit(DEPT_OUTPATIENT)
		transition(visit.name, "start_consultation", system=True)
		self.assertEqual(visit_state(visit.name).enc, "none")
		with as_user(self.users["clinician"]):
			offered = {
				a["action"] for a in engine.get_available_actions(frappe.get_doc(VISIT_DOCTYPE, visit.name))
			}
			self.assertNotIn("discharge", offered)
			with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
				apply_action(visit.name, "discharge")
		self.assertEqual(visit_state(visit.name).status, IC)

	# DEFECT-2 (fixed by `_guard_complete_consultation`; regression guard): complete_consultation is offered and applied again on a visit whose consultation is already
	# complete (Awaiting Pharmacy / Laboratory): a duplicate log row, AP -> AP.
	def test_defect2_repeated_complete_consultation_refused(self):
		visit, enc = self.consulting()
		self.add_para(enc)
		self.complete(enc)
		self.assertEqual(visit_state(visit).status, AP)
		with as_user(self.users["clinician"]):
			offered = {
				a["action"] for a in engine.get_available_actions(frappe.get_doc(VISIT_DOCTYPE, visit))
			}
			self.assertNotIn("complete_consultation", offered)
			with self.assertRaises(frappe.ValidationError):
				transition(visit, "complete_consultation")
		log = frappe.get_doc(VISIT_DOCTYPE, visit).status_log
		self.assertEqual(sum(1 for r in log if r.action == "complete_consultation"), 1)


# =====================================================================================================
# Referral
# =====================================================================================================

EXPECTED_REF = {
	"save": (("",), sm.CONSULT, False),
	"send": (("Draft",), sm.CONSULT, False),
	"receive": (("Sent",), SYSTEM, True),
	"record_outcome": (("Sent", "Received"), (*sm.CONSULT, ROLE_HIS), False),
	"mark_lost": (("Sent",), (*sm.CONSULT, ROLE_MIC), False),
	"cancel": (("Draft",), sm.CONSULT, False),
}


class TestReferralMachine(SMCase):
	def draft(self, rtype: str) -> str:
		_visit, enc = self.consulting()
		add_diagnosis(enc)
		values = {"to_department": DEPT_MENTAL_HEALTH} if rtype == "Internal" else {"to_facility": FACILITY}
		with as_user(self.users["clinician"]):
			return ref.create_referral(enc, rtype, "T- specialist review", **values)["name"]

	def referral_in(self, status: str, rtype: str) -> str:
		name = self.draft(rtype)
		path = {
			"Draft": [],
			"Cancelled": ["cancel"],
			"Sent": ["send"],
			"Received": ["send", "receive"],
			"Completed": ["send", "record_outcome"],
			"Lost to Follow-up": ["send", "mark_lost"],
		}[status]
		for action in path:
			ref.transition(name, action, system=True, comment="T- r", outcome="T- o")
		self.assertEqual(frappe.db.get_value(REFERRAL_DOCTYPE, name, "status"), status)
		return name

	def expect(self, status: str, rtype: str, action: str, actor: str):
		statuses, roles, internal = EXPECTED_REF[action]
		if actor not in (SYSTEM, ADMIN) and (roles == SYSTEM or sm.ACTOR_ROLES[actor] not in roles):
			return frappe.PermissionError
		if status not in statuses or (internal and rtype != "Internal"):
			return IllegalReferralTransitionError
		return None

	def test_table_matches_spec(self):
		for action, rule in ref.TRANSITIONS.items():
			statuses, roles, internal = EXPECTED_REF[action]
			self.assertEqual(set(rule.from_statuses), set(statuses), action)
			self.assertEqual(rule.is_system, roles == SYSTEM, action)
			if roles != SYSTEM:
				self.assertEqual(set(rule.roles), set(roles), action)
			self.assertEqual(rule.internal_only, internal, action)

	def test_matrix(self):
		"""Every action x status x type x actor; offered == works; repeated actions refused."""
		count = 0
		for rtype in ("Internal", "External"):
			for status in ref.STATUSES:
				if status == "Received" and rtype == "External":
					continue
				name = self.referral_in(status, rtype)
				works: dict[str, set] = defaultdict(set)
				for action in ref.TRANSITIONS:
					for actor in self.all_actors():
						expected = self.expect(status, rtype, action, actor)
						count += 1
						with (
							self.subTest(rtype=rtype, status=status, action=action, actor=actor),
							savepoint("sm_ref"),
						):
							err = None
							try:
								if actor == SYSTEM:
									ref.transition(name, action, system=True, comment="T- r", outcome="T- o")
								else:
									with as_user(self.u(actor)):
										ref.apply_action(name, action, comment="T- r", outcome="T- o")
							except Exception as e:
								err = e
							if expected is not None:
								self.assertTrue(_exc_matches(err, expected), f"{expected} vs {err!r}")
								self.assertEqual(
									frappe.db.get_value(REFERRAL_DOCTYPE, name, "status"), status
								)
								continue
							self.assertIsNone(err, repr(err))
							doc = frappe.get_doc(REFERRAL_DOCTYPE, name)
							to_status = ref.TRANSITIONS[action].to_status
							self.assertEqual(doc.status, to_status)
							self.assertEqual(
								(doc.status_log[-1].from_status, doc.status_log[-1].action), (status, action)
							)
							if action == "send":
								self.assertEqual(visit_state(doc.source_visit).status, REF)
								if rtype == "Internal":
									target = visit_state(doc.target_visit)
									self.assertEqual((target.status, target.referred_from), (RD, name))
									self.assertEqual(violations(target), set())
								else:
									self.assertFalse(doc.target_visit)
							if action == "record_outcome":
								self.assertEqual((doc.outcome, str(doc.outcome_date)), ("T- o", today()))
							if actor != SYSTEM and not ref.TRANSITIONS[action].is_system:
								works[actor].add(action)
				doc = frappe.get_doc(REFERRAL_DOCTYPE, name)
				for actor in self.human_actors():
					with self.subTest(rtype=rtype, status=status, offered_for=actor):
						offered = {a["action"] for a in ref.get_available_actions(doc, self.u(actor))}
						self.assertEqual(offered, works[actor] - {"save"})
		print(f"\n[S1] referral matrix: {count} combinations")

	def test_repeated_actions_refused(self):
		name = self.referral_in("Sent", "Internal")
		source = frappe.db.get_value(REFERRAL_DOCTYPE, name, "source_encounter")
		with as_user(self.users["clinician"]), self.assertRaises(IllegalReferralTransitionError):
			ref.apply_action(name, "send")
		with as_user(self.users["clinician"]), self.assertRaises(IllegalReferralTransitionError):
			ref.create_referral(source, "External", "T- second", to_facility=FACILITY, send=True)
		ref.transition(name, "receive", system=True)
		self.assertFalse(
			ref.receive_for_visit(
				frappe.get_doc(VISIT_DOCTYPE, frappe.db.get_value(REFERRAL_DOCTYPE, name, "target_visit"))
			)
		)
		with self.assertRaises(IllegalReferralTransitionError):
			ref.transition(name, "receive", system=True)
		with as_user(self.users["his"]):
			ref.apply_action(name, "record_outcome", outcome="T- seen")
			with self.assertRaises(IllegalReferralTransitionError):
				ref.apply_action(name, "record_outcome", outcome="T- seen again")
		cancelled = self.referral_in("Cancelled", "External")
		with as_user(self.users["clinician"]), self.assertRaises(IllegalReferralTransitionError):
			ref.apply_action(cancelled, "cancel")

	def test_draft_after_source_visit_closed(self):
		"""Close Visit while a referral is Draft: send is refused and leaves nothing; cancel still works."""
		name = self.draft("Internal")
		visit = frappe.db.get_value(REFERRAL_DOCTYPE, name, "source_visit")
		transition(visit, "close_visit", system=True, closure_reason="Other", comment="T- left")
		with as_user(self.users["clinician"]):
			with self.assertRaises(IllegalReferralTransitionError):
				ref.apply_action(name, "send")
			self.assertEqual(ref.apply_action(name, "cancel")["status"], "Cancelled")
		self.assertEqual(visit_state(visit).status, D)
		self.assertFalse(frappe.db.get_value(REFERRAL_DOCTYPE, name, "target_visit"))

	def test_target_visit_closed_before_seen(self):
		"""Internal referral whose patient never reaches the department: referral stays Sent, then Lost."""
		name = self.referral_in("Sent", "Internal")
		target = frappe.db.get_value(REFERRAL_DOCTYPE, name, "target_visit")
		with as_user(self.users["reception"]):
			apply_action(target, "close_visit", closure_reason="Left before being seen", comment="T- left")
		self.assertEqual(frappe.db.get_value(REFERRAL_DOCTYPE, name, "status"), "Sent")
		with as_user(self.users["mic"]):
			self.assertEqual(
				ref.apply_action(name, "mark_lost", comment="T- never seen")["status"], "Lost to Follow-up"
			)


# =====================================================================================================
# Data Access Request
# =====================================================================================================

EXPECTED_DAR = {
	"verify_identity": (("Received",), (ROLE_RECEPTIONIST, ROLE_HIS), None),
	"approve": (("Identity Verified",), (ROLE_MIC,), None),
	"reject": (("Identity Verified",), (ROLE_MIC,), None),
	"prepare_extract": (("Approved",), (ROLE_HIS,), "Access"),
	"record_correction": (("Approved",), (ROLE_HIS,), "Correction"),
	"respond": (("Response Ready", "Rejected"), (ROLE_HIS, ROLE_MIC), None),
}


class TestDataAccessRequestMachine(SMCase):
	def change_patient(self, patient: str) -> str:
		doc = frappe.get_doc("Patient", patient)
		doc.occupation = f"T-occ-{frappe.generate_hash(length=6)}"
		doc.save(ignore_permissions=True, ignore_version=False)
		return frappe.get_all(
			"Version",
			filters={"ref_doctype": "Patient", "docname": patient},
			pluck="name",
			order_by="creation desc",
			limit=1,
		)[0]

	def dar_at(self, status: str, rtype: str):
		doc = frappe.get_doc(
			{
				"doctype": DATA_ACCESS_REQUEST_DOCTYPE,
				"patient": make_patient().name,
				"request_type": rtype,
				"requester_type": "Patient",
				"requester_name": "T- Requester",
				"details": "T- my record",
			}
		).insert(ignore_permissions=True)
		version = self.change_patient(doc.patient)
		kw = self.inputs(doc.patient, version)
		steps = {
			"Received": [],
			"Identity Verified": ["verify_identity"],
			"Approved": ["verify_identity", "approve"],
			"Rejected": ["verify_identity", "reject"],
			"Response Ready": [
				"verify_identity",
				"approve",
				"prepare_extract" if rtype == "Access" else "record_correction",
			],
			"Closed": ["verify_identity", "reject", "respond"],
		}[status]
		for step in steps:
			dar.transition(doc.name, step, system=True, **kw)
		self.assertEqual(frappe.db.get_value(DATA_ACCESS_REQUEST_DOCTYPE, doc.name, "status"), status)
		return doc.name, kw

	@staticmethod
	def inputs(patient: str, version: str) -> dict:
		return {
			"identity_method": "National ID",
			"identity_evidence": "T- card",
			"decision_reason": "T- reason",
			"corrected_doctype": "Patient",
			"corrected_record": patient,
			"correction_version": version,
			"response_date": today(),
			"response_method": "In person",
		}

	def expect(self, status: str, rtype: str, action: str, actor: str):
		if action == "create":
			return IllegalTransitionError
		statuses, roles, request_type = EXPECTED_DAR[action]
		if actor not in (SYSTEM, ADMIN) and sm.ACTOR_ROLES[actor] not in roles:
			return frappe.PermissionError
		if status not in statuses:
			return IllegalTransitionError
		if request_type and rtype != request_type:
			return dar.RequestTypeMismatchError
		return None

	def test_table_matches_spec(self):
		for action, (statuses, roles, request_type) in EXPECTED_DAR.items():
			rule = dar.TRANSITIONS[action]
			self.assertEqual(
				(set(rule.from_statuses), set(rule.roles), rule.request_type),
				(set(statuses), set(roles), request_type),
			)

	def test_matrix(self):
		"""Every action x status x request type x actor; offered == works; repeated actions refused.
		prepare_extract writes a private File that a savepoint cannot undo: only 3 succeed (as test_privacy)."""
		count = 0
		for rtype in ("Access", "Correction"):
			for status in dar.STATUSES:
				name, kw = self.dar_at(status, rtype)
				works: dict[str, set] = defaultdict(set)
				for action in dar.TRANSITIONS:
					for actor in self.all_actors():
						expected = self.expect(status, rtype, action, actor)
						count += 1
						with (
							self.subTest(rtype=rtype, status=status, action=action, actor=actor),
							savepoint("sm_dar"),
						):
							err = None
							try:
								if actor == SYSTEM:
									dar.transition(name, action, system=True, **kw)
								else:
									with as_user(self.u(actor)):
										dar.apply_action(name, action, **kw)
							except Exception as e:
								err = e
							if expected is not None:
								self.assertTrue(_exc_matches(err, expected), f"{expected} vs {err!r}")
								self.assertEqual(
									frappe.db.get_value(DATA_ACCESS_REQUEST_DOCTYPE, name, "status"), status
								)
								continue
							self.assertIsNone(err, repr(err))
							doc = frappe.get_doc(DATA_ACCESS_REQUEST_DOCTYPE, name)
							self.assertEqual(doc.status, dar.TRANSITIONS[action].to_status)
							self.assertEqual((doc.log[-1].from_status, doc.log[-1].action), (status, action))
							if actor != SYSTEM:
								works[actor].add(action)
				doc = frappe.get_doc(DATA_ACCESS_REQUEST_DOCTYPE, name)
				for actor in self.human_actors():
					with self.subTest(rtype=rtype, status=status, offered_for=actor):
						offered = {a["action"] for a in dar.get_available_actions(doc, self.u(actor))}
						self.assertEqual(offered, works[actor])
		print(f"\n[S1] data access request matrix: {count} combinations")


# =====================================================================================================
# Consent Statement and Patient Consent
# =====================================================================================================


class TestConsentMachines(SMCase):
	def language(self) -> str:
		for lang in ("sw", "fr", "de", "es"):
			if frappe.db.exists("Language", lang) and not statements.get_active_statement(lang):
				return lang
		self.skipTest("no spare Language for a test statement")

	def statement(self, lang: str, version: str, text: str = "T- text"):
		return frappe.get_doc(
			{"doctype": "Consent Statement", "version": version, "language": lang, "text": text}
		).insert(ignore_permissions=True)

	def active_statement(self) -> str:
		return (
			statements.get_active_statement("en")
			or statements.activate(self.statement("en", "T-EN").name, system=True).name
		)

	def test_statement_matrix(self):
		"""activate x {Draft, Active, Retired} x actor; retire is never a user action; offered == works."""
		lang = self.language()
		draft = self.statement(lang, "T-D").name
		retired = statements.activate(self.statement(lang, "T-A1").name, system=True).name
		active = statements.activate(self.statement(lang, "T-A2").name, system=True).name  # retires T-A1
		states = {"Draft": draft, "Active": active, "Retired": retired}
		for status, name in states.items():
			self.assertEqual(frappe.db.get_value("Consent Statement", name, "status"), status)
		allowed = {ROLE_MIC, ROLE_SYSADMIN}
		for status, name in states.items():
			works = set()
			for actor in self.all_actors():
				legal_role = actor in (SYSTEM, ADMIN) or sm.ACTOR_ROLES[actor] in allowed
				with self.subTest(status=status, actor=actor), savepoint("sm_cs"):
					err = None
					try:
						if actor == SYSTEM:
							statements.activate(name, system=True)
						else:
							with as_user(self.u(actor)):
								statements.activate_statement(name)
					except Exception as e:
						err = e
					if not legal_role:
						self.assertIsInstance(err, frappe.PermissionError)
					elif status != "Draft":
						self.assertIsInstance(err, IllegalTransitionError)
					else:
						self.assertIsNone(err, repr(err))
						actives = frappe.get_all(
							"Consent Statement", filters={"language": lang, "status": "Active"}, pluck="name"
						)
						self.assertEqual(actives, [name], "exactly one Active per language")
						self.assertEqual(
							frappe.db.get_value("Consent Statement", active, "status"), "Retired"
						)
						works.add(actor)
			doc = frappe.get_doc("Consent Statement", name)
			for actor in self.human_actors():
				offered = {a["action"] for a in statements.get_available_actions(doc, self.u(actor))}
				self.assertNotIn("retire", offered)
				self.assertEqual(offered == {"activate"}, actor in works, (status, actor))
		# Text is mandatory on insert; a Draft whose text is blanked behind the form cannot be activated.
		empty = self.statement(lang, "T-E").name
		frappe.db.set_value("Consent Statement", empty, "text", "<p> </p>")
		with self.assertRaises(IllegalTransitionError):
			statements.activate(empty, system=True)

	def test_patient_consent_outcome_matrix(self):
		"""Current outcome (none / each outcome) x new outcome: Withdrawn only from Given; the latest
		submitted record is current; no cancel for anyone; only the receptionist records consent."""
		statement = self.active_statement()
		currents = [None, *consent.OUTCOMES]
		for current in currents:
			for new in consent.OUTCOMES:
				with self.subTest(current=current, new=new), savepoint("sm_pc"):
					patient = make_patient().name
					if current == consent.OUTCOME_WITHDRAWN:
						consent_doc(patient, statement, consent.OUTCOME_GIVEN)
					if current:
						consent_doc(patient, statement, current)
					legal = new != consent.OUTCOME_WITHDRAWN or current == consent.OUTCOME_GIVEN
					with as_user(self.users["reception"]):
						if legal:
							status = consent.record_consent(
								patient, statement, new, reason=None if new == "Given" else "T- reason"
							)
							self.assertEqual(status["outcome"], new)
							self.assertEqual(status["needs_attention"], new != consent.OUTCOME_GIVEN)
						else:
							with self.assertRaises(PatientConsentError):
								consent.record_consent(patient, statement, new, reason="T- reason")
							self.assertEqual(
								consent.consent_status(patient)["outcome"], current or consent.OUTCOME_NONE
							)
		patient = make_patient().name
		for actor in self.human_actors():
			allowed = actor in (ADMIN, "reception")
			with self.subTest(record_as=actor), savepoint("sm_pc"), as_user(self.u(actor)):
				if allowed:
					consent.record_consent(patient, statement, consent.OUTCOME_GIVEN)
				else:
					with self.assertRaises(frappe.PermissionError):
						consent.record_consent(patient, statement, consent.OUTCOME_GIVEN)
		given = consent_doc(patient, statement, consent.OUTCOME_GIVEN)
		for actor in (ADMIN, "reception"):
			with self.subTest(cancel_as=actor), savepoint("sm_pc"), as_user(self.u(actor)):
				with self.assertRaises(frappe.PermissionError):
					frappe.get_doc("Patient Consent", given.name).cancel()
		# A Withdrawn draft made while Given is refused at submit once a newer Withheld is current.
		draft = consent_doc(patient, statement, consent.OUTCOME_WITHDRAWN, submit=False)
		consent_doc(patient, statement, consent.OUTCOME_WITHHELD)
		with self.assertRaises(PatientConsentError):
			frappe.get_doc("Patient Consent", draft.name).submit()


def consent_doc(patient: str, statement: str, outcome: str, submit: bool = True):
	doc = frappe.get_doc(
		{
			"doctype": "Patient Consent",
			"patient": patient,
			"statement": statement,
			"outcome": outcome,
			"reason": None if outcome == "Given" else "T- reason",
		}
	).insert(ignore_permissions=True)
	if submit:
		doc.submit()
	return doc


# =====================================================================================================
# Dispensing line states
# =====================================================================================================

MARLEY_STATUS = {
	"Pending": "active-Medication Request Status",
	"Partly Dispensed": "active-Medication Request Status",
	"Dispensed": "completed-Medication Request Status",
	"Not Dispensed": "stopped-Medication Request Status",
	"Closed": "completed-Medication Request Status",
}


def line_oracle(total: float, dispensed: float, final: bool) -> str:
	"""Independent line state (SDD §5.5 / B3 table)."""
	if total > 0 and dispensed >= total:
		return "Dispensed"
	if final:
		return "Closed" if dispensed > 0 else "Not Dispensed"
	return "Partly Dispensed" if dispensed > 0 else "Pending"


class TestDispensingLineMachine(SMCase):
	def prescribed(self, lab: bool = True):
		"""Completed consultation with one PARA line (10); lab pending keeps the visit open after dispensing."""
		visit, enc = self.consulting()
		self.add_para(enc)
		if lab:
			tasks.raise_event(visit, engine.ACTION_REQUEST_LAB)
		self.complete(enc, reason="T- results pending" if lab else None)
		(mr,) = self.mrs(enc)
		return visit, enc, mr

	SETUPS: ClassVar[dict] = {
		"Pending": [],
		"Partly Dispensed": [(4, "Dispensed", 0)],
		"Dispensed": [(10, "Dispensed", 0)],
		"Not Dispensed": [(0, "Not Dispensed", 1)],
		"Closed": [(4, "Dispensed", 1)],
	}

	def record(self, mr, qty, outcome, final):
		if outcome == "Not Dispensed":
			return self.dispense(mr, outcome=outcome, not_dispensed_reason="T- out of stock")
		return self.dispense(mr, qty=qty, final=final)

	def test_line_matrix(self):
		"""Every line state x every event; the line and the visit follow the oracle."""
		self.assertEqual(set(dispensing.LINE_STATES), set(MARLEY_STATUS))
		for state, steps in self.SETUPS.items():
			visit, _enc, mr = self.prescribed()
			records = [self.record(mr, *s) for s in steps]
			dispensed = sum(q for q, o, _f in steps if o == "Dispensed")
			final = any(f for _q, _o, f in steps)
			self.assertEqual(dispensing.line_status(mr), state)
			self.assertEqual(state, line_oracle(10, dispensed, final))
			rest = 10 - dispensed
			events = {
				"submit_partial": (2, "Dispensed", 0),
				"submit_rest": (rest or 1, "Dispensed", 0),
				"submit_over": (rest + 1, "Dispensed", 0),
				"submit_final": (1, "Dispensed", 1),
				"submit_not_dispensed": (0, "Not Dispensed", 1),
				"cancel_last": None,
			}
			for event, spec in events.items():
				with self.subTest(state=state, event=event), savepoint("sm_line"):
					resolved = state in dispensing.RESOLVED_LINE_STATES
					if spec is None:
						if not records:
							continue
						with as_user(self.users["pharmacist"]):
							dispensing.cancel_dispensing(records[-1].name, "T- wrong line")
						last = steps[-1]
						new_disp = dispensed - (last[0] if last[1] == "Dispensed" else 0)
						new_final = any(f for _q, _o, f in steps[:-1])
					else:
						qty, outcome, fin = spec
						if resolved:
							with self.assertRaises(LineResolvedError):
								self.record(mr, qty, outcome, fin)
							continue
						if event == "submit_over":
							with self.assertRaises(OverDispenseError):
								self.record(mr, qty, outcome, fin)
							continue
						self.record(mr, qty, outcome, fin)
						new_disp = dispensed + (qty if outcome == "Dispensed" else 0)
						new_final = final or bool(fin)
					expected = line_oracle(10, new_disp, new_final)
					row = frappe.db.get_value(
						"Medication Request",
						mr,
						["krcs_dispense_status", "krcs_qty_dispensed", "status"],
						as_dict=True,
					)
					self.assertEqual(
						(row.krcs_dispense_status, row.krcs_qty_dispensed, row.status),
						(expected, new_disp, MARLEY_STATUS[expected]),
					)
					s = self.assert_reachable(visit)
					is_open = expected in dispensing.OPEN_LINE_STATES
					self.assertEqual((s.status, s.ph, s.lab), (AP if is_open else AL, int(is_open), 1))

	def test_dispensing_roles(self):
		visit, _enc, mr = self.prescribed()
		for actor in self.human_actors():
			if actor in (ADMIN, "pharmacist", "pharmacist2"):
				continue
			with self.subTest(actor=actor), savepoint("sm_line"), self.assertRaises(frappe.PermissionError):
				self.dispense(mr, qty=1, user=actor)
		doc = self.dispense(mr, qty=10)
		for actor in ("pharmacist2", "clinician", "mic"):
			with (
				self.subTest(cancel_as=actor),
				savepoint("sm_line"),
				self.assertRaises(frappe.PermissionError),
			):
				with as_user(self.users[actor]):
					dispensing.cancel_dispensing(doc.name, "T- reason")
		self.assertEqual(visit_state(visit).status, AL)

	# DEFECT-3: cancelling a Dispensing Record recomputes the line and writes Marley's status unconditionally
	# (`dispensing.recompute_line`), so an on-hold / stopped Medication Request is reset to active and
	# becomes dispensable again.
	def test_defect3_cancel_keeps_hold(self):
		visit, _enc, mr = self.prescribed()
		doc = self.dispense(mr, qty=4)
		from healthcare.healthcare.doctype.medication_request.medication_request import (
			set_medication_request_status,
		)

		set_medication_request_status(mr, "on-hold-Medication Request Status")
		with as_user(self.users["pharmacist"]):
			dispensing.cancel_dispensing(doc.name, "T- wrong quantity")
		self.assertEqual(
			frappe.db.get_value("Medication Request", mr, "status"), "on-hold-Medication Request Status"
		)
		with self.assertRaises(frappe.ValidationError):
			self.dispense(mr, qty=1)
		self.assertTrue(visit)


# =====================================================================================================
# Visitor Record
# =====================================================================================================


class TestVisitorMachine(SMCase):
	def new_visitor(self) -> str:
		with as_user(self.users["reception"]):
			return (
				frappe.get_doc(
					{
						"doctype": VISITOR_DOCTYPE,
						"facility": get_facility(0),
						"visitor_name": "T- Visitor",
						"relationship": "Guardian",
					}
				)
				.insert()
				.name
			)

	def test_matrix(self):
		self.assertEqual(set(visitor.TRANSITIONS), {"record_exit"})
		inside = self.new_visitor()
		exited = self.new_visitor()
		visitor.transition(exited, "record_exit")
		for status, name in (("Inside", inside), ("Exited", exited)):
			for actor in self.human_actors():
				legal_role = actor == ADMIN or sm.ACTOR_ROLES[actor] == ROLE_RECEPTIONIST
				with self.subTest(status=status, actor=actor), savepoint("sm_vis"), as_user(self.u(actor)):
					if not legal_role:
						with self.assertRaises(frappe.PermissionError):
							visitor.transition(name, "record_exit")
					elif status == "Exited":
						with self.assertRaises(IllegalVisitorTransitionError):
							visitor.transition(name, "record_exit")
					else:
						doc = visitor.transition(name, "record_exit")
						self.assertEqual((doc.status, doc.status_log[-1].action), ("Exited", "record_exit"))
						self.assertTrue(doc.exit_time)
						with self.assertRaises(IllegalVisitorTransitionError):
							visitor.transition(name, "record_exit")
		with self.assertRaises(IllegalVisitorTransitionError):
			visitor.transition(inside, "readmit")


# =====================================================================================================
# Registration: walk-in identifier states and merge
# =====================================================================================================


class TestRegistrationStates(SMCase):
	def test_walk_in_identifier_states(self):
		"""walk-in -> facility id (once) -> primary linked / conflict -> merged (frozen)."""
		walk_in = make_patient(krcs_is_walk_in=1)

		def facility_ids(patient):
			doc = frappe.get_doc("Patient", patient)
			return [r for r in doc.get(PATIENT_IDENTIFIERS_FIELD) if r.identifier_type == IDENTIFIER_FACILITY]

		(row,) = facility_ids(walk_in.name)
		frappe.get_doc("Patient", walk_in.name).save(ignore_permissions=True)
		self.assertEqual([r.identifier_value for r in facility_ids(walk_in.name)], [row.identifier_value])
		doc = frappe.get_doc("Patient", walk_in.name)
		doc.get(PATIENT_IDENTIFIERS_FIELD)[0].identifier_value = "T-CHANGED-1"
		with self.assertRaises(FacilityIdentifierError):
			doc.save(ignore_permissions=True)
		doc = frappe.get_doc("Patient", walk_in.name)
		doc.set(PATIENT_IDENTIFIERS_FIELD, [])
		with self.assertRaises(FacilityIdentifierError):
			doc.save(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			reg_api.link_identifier(walk_in.name, IDENTIFIER_FACILITY, "X-1")
		holder = make_patient(
			**{
				PATIENT_IDENTIFIERS_FIELD: [
					{"identifier_type": IDENTIFIER_POL, "identifier_value": "T-POL-S1-1"}
				]
			}
		)
		clash = reg_api.link_identifier(walk_in.name, IDENTIFIER_POL, "t-pol-s1-1")
		self.assertEqual((clash["status"], clash["merge_target"]), ("conflict", holder.name))
		with as_user(self.users["mic"]):
			reg_api.merge_patients(walk_in.name, holder.name, "T- same person")
		merged = frappe.get_doc("Patient", walk_in.name)
		self.assertEqual((merged.status, merged.get(MERGED_INTO_FIELD)), ("Disabled", holder.name))
		self.assertEqual(len(facility_ids(holder.name)), 1, "the walk-in id moved to the survivor")
		with self.assertRaises(frappe.ValidationError):
			merged.save(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			reg_api.link_identifier(walk_in.name, IDENTIFIER_POL, "T-POL-S1-2")
		with self.assertRaises(frappe.ValidationError):
			make_visit(patient=walk_in.name)
		self.assertTrue(merged.get(REGISTRATION_FACILITY_FIELD))

	def test_merge_refused_for_every_open_status(self):
		target = make_patient().name
		for status in sm.OPEN:
			visit = make_visit()
			path = {
				R: [],
				IT: ["start_triage"],
				RD: ["start_triage", "route"],
				IC: ["start_triage", "route", "start_consultation"],
				AL: ["start_triage", "route", "start_consultation", "request_lab"],
				AP: ["start_triage", "route", "start_consultation", "prescribe"],
			}[status]
			for action in path:
				kw = (
					{"department": DEPT_OUTPATIENT, "without_vitals_reason": "T-"}
					if action == "route"
					else {}
				)
				transition(visit.name, action, system=True, **kw)
			with self.subTest(status=status), savepoint("sm_merge"), as_user(self.users["mic"]):
				with self.assertRaises(frappe.ValidationError):
					reg_api.merge_patients(visit.patient, target, "T- duplicate")
				self.assertFalse(frappe.db.get_value("Patient", visit.patient, MERGED_INTO_FIELD))
			transition(visit.name, "close_visit", system=True, closure_reason="Other", comment="T-")
			with (
				self.subTest(status=status, after="close"),
				savepoint("sm_merge"),
				as_user(self.users["mic"]),
			):
				reg_api.merge_patients(visit.patient, target, "T- duplicate")
				self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit.name, "patient"), target)


# =====================================================================================================
# End-to-end Marley scenarios
# =====================================================================================================


class TestMarleyScenarios(LabTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users["pharmacist2"] = make_user("pv-pharmacist2", [ROLE_PHARMACIST])

	add_para = SMCase.add_para
	mrs = SMCase.mrs

	def setUp(self):
		super().setUp()
		if not frappe.db.exists(REFERRAL_FACILITY_DOCTYPE, FACILITY):
			frappe.get_doc(
				{"doctype": REFERRAL_FACILITY_DOCTYPE, "facility_name": FACILITY, "facility_type": "Hospital"}
			).insert(ignore_permissions=True)

	def complete(self, encounter: str, reason: str | None = None, user: str = "clinician") -> dict:
		fill_mh_note(encounter)  # D63
		with as_user(self.users[user]):
			return complete_consultation(encounter, reason=reason, no_diagnosis_reason="T- auditor")

	def dispense(self, mr: str, qty=None, user="pharmacist", **kw):
		stock_for_dispensing(mr, qty, item=dispensed_item(kw))  # Phase 6S
		with as_user(self.users[user]):
			doc = frappe.get_doc(
				{
					"doctype": DISPENSING_DOCTYPE,
					"medication_request": mr,
					"outcome": "Dispensed",
					"quantity": qty,
					**kw,
				}
			).insert()
			doc.submit()
		return doc

	def state(self, visit: str) -> sm.VState:
		s = visit_state(visit)
		self.assertEqual(violations(s), set(), str(s))
		return s

	def skipped(self, visit: str, action: str) -> bool:
		return bool(
			frappe.db.exists(
				"Comment",
				{
					"reference_doctype": VISIT_DOCTYPE,
					"reference_name": visit,
					"content": ["like", f"Skipped {action}:%"],
				},
			)
		)

	def log_actions(self, visit: str) -> list[str]:
		return [r.action for r in frappe.get_doc(VISIT_DOCTYPE, visit).status_log]

	# 1
	def test_two_orders_approved_at_different_times(self):
		visit = self.visit_in_consultation()
		enc, (mrdt, hb) = self.send_lab_orders(visit, [MRDT, HB])
		self.assertEqual(self.state(visit.name).status, AL)
		self.collect_all(enc.name)
		self.result_single(mrdt)
		self.assertEqual((self.state(visit.name).status, self.state(visit.name).lab), (AL, 1))
		self.result_single(hb, "13.2")
		self.assertEqual((self.state(visit.name).status, self.state(visit.name).lab), (IC, 0))
		self.assertEqual(self.log_actions(visit.name).count("lab_results_ready"), 1)
		self.assertEqual(len(self.notifications("clinician", "Lab result ready")), 2)

	# 2
	def test_result_rejected_and_redone(self):
		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		self.collect_all(enc.name)
		first = self.result_single(mrdt)
		self.assertEqual(self.state(visit.name).status, IC)
		self.reject(first)
		self.assertEqual((self.state(visit.name).status, self.state(visit.name).lab), (AL, 1))
		self.result_single(mrdt, "Positive")
		self.assertEqual(self.state(visit.name).status, IC)
		self.assertEqual(
			self.log_actions(visit.name)[-3:], ["lab_results_ready", "request_lab", "lab_results_ready"]
		)

	# 3
	def test_result_after_close_visit_and_after_discharge(self):
		# After Close Visit
		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		self.collect_all(enc.name)
		with as_user(self.users["clinician"]):
			transition(visit.name, "close_visit", closure_reason="Left before completion", comment="T- left")
		closed_at = self.state(visit.name).closed_at
		obs = self.result_single(mrdt)
		s = self.state(visit.name)
		self.assertEqual((s.status, s.lab, s.closed_at), (D, 0, closed_at))
		self.assertTrue(self.skipped(visit.name, "lab_results_ready"))
		self.assertEqual(frappe.db.get_value("Observation", obs, "docstatus"), 1)
		# After discharge by completion: complete with results pending, results -> Discharged, then reject + redo.
		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		self.complete(enc.name, reason="T- treat empirically")
		self.assertEqual(self.state(visit.name).status, AL)
		self.collect_all(enc.name)
		obs = self.result_single(mrdt)
		s = self.state(visit.name)
		self.assertEqual((s.status, s.lab), (D, 0))
		self.reject(obs)
		s2 = self.state(visit.name)
		self.assertEqual((s2.status, s2.lab, s2.closed_at), (D, 0, s.closed_at))
		self.assertEqual(len(self.notifications("mic", "Lab result withdrawn")), 1)
		self.result_single(mrdt, "Positive")
		self.assertEqual(self.state(visit.name).status, D)
		self.assertTrue(self.skipped(visit.name, "lab_results_ready"))

	# 4
	def test_sample_collection_cancelled(self):
		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		self.collect_all(enc.name)
		collection = frappe.get_doc("Sample Collection", self.sample_rows(enc.name)[0].parent)
		collection.submit()
		collection.cancel()
		self.assertTrue(frappe.db.exists("Diagnostic Report", {"docname": enc.name}))
		self.assertEqual(self.state(visit.name).status, AL)
		self.result_single(mrdt)
		self.assertEqual(self.state(visit.name).status, IC)

	# 5
	def test_encounter_cancel_refused_and_rolled_back(self):
		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		self.complete(enc.name, reason="T- pending")
		before = self.state(visit.name)
		with as_user(self.users["clinician"]):
			with self.assertRaises((frappe.PermissionError, EncounterGuardError)):
				frappe.get_doc(ENCOUNTER_DOCTYPE, enc.name).cancel()
		frappe.db.savepoint("sm_cancel")
		with self.assertRaises(EncounterGuardError):
			frappe.get_doc(ENCOUNTER_DOCTYPE, enc.name).cancel()
		# Observation for the report: Marley's before_cancel (orders cancelled) runs before the EMR guard;
		# only the request rollback undoes it.
		self.marley_cancelled_first = frappe.db.get_value("Service Request", mrdt, "docstatus") == 2
		frappe.db.rollback(save_point="sm_cancel")
		self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc.name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Service Request", mrdt, "docstatus"), 1)
		after = self.state(visit.name)
		self.assertEqual(
			(after.status, after.lab, after.log_len), (before.status, before.lab, before.log_len)
		)
		print(
			f"\n[S1] encounter cancel: Marley cancelled the orders before the guard: {self.marley_cancelled_first}"
		)

	# 6
	def test_encounter_amend_refused(self):
		visit = self.visit_in_consultation()
		enc, _srs = self.send_lab_orders(visit, [MRDT])
		self.complete(enc.name, reason="T- pending")
		orders = frappe.db.count("Service Request", {"order_group": enc.name})
		amended = frappe.copy_doc(frappe.get_doc(ENCOUNTER_DOCTYPE, enc.name))
		amended.amended_from = enc.name
		with self.assertRaises(frappe.ValidationError):
			amended.insert()
		self.assertEqual(frappe.db.count(ENCOUNTER_DOCTYPE, {"krcs_visit": visit.name}), 1)
		self.assertEqual(frappe.db.count("Service Request", {"order_group": enc.name}), orders)

	# 7
	def test_merge_with_open_visit_then_after_close(self):
		visit = self.visit_in_consultation()
		target = make_patient().name
		with as_user(self.users["mic"]), self.assertRaises(frappe.ValidationError):
			reg_api.merge_patients(visit.patient, target, "T- duplicate")
		with as_user(self.users["clinician"]):
			transition(visit.name, "close_visit", closure_reason="Registered in error", comment="T- wrong")
		with as_user(self.users["mic"]):
			reg_api.merge_patients(visit.patient, target, "T- duplicate")
		self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit.name, "patient"), target)
		self.assertEqual(make_visit(patient=target).status, R)

	# 8
	def test_same_day_internal_referral_lifecycle(self):
		visit = self.visit_in_consultation()
		enc = self.open_encounter(visit)
		add_diagnosis(enc.name)
		with as_user(self.users["clinician"]):
			out = ref.create_referral(
				enc.name, "Internal", "T- review", to_department=DEPT_MENTAL_HEALTH, send=True
			)
		self.assertEqual(self.state(visit.name).status, REF)
		target = out["target_visit"]
		t = self.state(target)
		self.assertEqual((t.status, t.dept, t.referred_from), (RD, DEPT_MENTAL_HEALTH, out["name"]))
		self.assertEqual(
			frappe.db.get_value(VISIT_DOCTYPE, target, "visit_date"), frappe.utils.getdate(today())
		)
		with as_user(self.users["mho"]):
			new_enc = start_consultation(target)["encounter"]
		self.assertEqual(frappe.db.get_value(REFERRAL_DOCTYPE, out["name"], "status"), "Received")
		self.complete(new_enc, user="mho")
		self.assertEqual(self.state(target).status, D)
		with as_user(self.users["his"]):
			self.assertEqual(
				ref.apply_action(out["name"], "record_outcome", outcome="T- seen")["status"], "Completed"
			)

	def test_internal_referral_retriaged_then_received_by_hook(self):
		visit = self.visit_in_consultation()
		enc = self.open_encounter(visit)
		add_diagnosis(enc.name)
		with as_user(self.users["clinician"]):
			out = ref.create_referral(
				enc.name, "Internal", "T- review", to_department=DEPT_MENTAL_HEALTH, send=True
			)
		target = out["target_visit"]
		with as_user(self.users["triage"]):
			transition(target, "return_to_triage", comment="T- unwell, re-triage")
		self.assertEqual(self.state(target).status, IT)
		make_vital_signs(visit.patient, visit=target, submit=True)
		with as_user(self.users["triage"]):
			transition(target, "route", department=DEPT_MENTAL_HEALTH)
		self.assertEqual(self.state(target).rwv, 0)
		with as_user(self.users["mho"]):
			transition(target, "start_consultation")
		self.assertEqual(frappe.db.get_value(REFERRAL_DOCTYPE, out["name"], "status"), "Received")

	# 9
	def test_referral_with_drugs_and_pending_labs(self):
		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		add_diagnosis(enc.name)
		self.add_para(enc.name)
		with as_user(self.users["clinician"]):
			out = ref.create_referral(
				enc.name, "Internal", "T- review", to_department=DEPT_MENTAL_HEALTH, send=True
			)
		s = self.state(visit.name)
		self.assertEqual((s.status, s.lab, s.ph), (REF, 1, 1))
		self.assertEqual(self.log_actions(visit.name)[-2:], ["prescribe", "refer"])
		(mr,) = self.mrs(enc.name)
		record = self.dispense(mr, qty=10)
		s2 = self.state(visit.name)
		self.assertEqual((s2.status, s2.lab, s2.ph, s2.closed_at), (REF, 1, 0, s.closed_at))
		self.collect_all(enc.name)
		self.result_single(mrdt)
		s3 = self.state(visit.name)
		self.assertEqual((s3.status, s3.lab, s3.ph, s3.closed_at), (REF, 0, 0, s.closed_at))
		self.assertEqual(self.state(out["target_visit"]).status, RD)
		with as_user(self.users["pharmacist"]), self.assertRaises(frappe.ValidationError):
			dispensing.cancel_dispensing(record.name, "T- wrong patient")  # Referred visit is closed

	# 10 + 12
	def test_close_visit_with_unsent_labs_and_pending_dispensing(self):
		visit = self.visit_in_consultation()
		enc = self.open_encounter(visit)
		add_lab_row(enc.name, MRDT)
		with as_user(self.users["clinician"]):
			transition(visit.name, "close_visit", closure_reason="Left before completion", comment="T- left")
		# L4: the unsent rows are kept, flagged withdrawn, and never sent afterwards.
		rows = frappe.get_doc(ENCOUNTER_DOCTYPE, enc.name).lab_test_prescription
		self.assertEqual(
			[(r.observation_template, r.krcs_withdrawn, r.service_request) for r in rows], [(MRDT, 1, None)]
		)
		self.assertTrue(
			frappe.db.exists(
				"Comment",
				{"reference_name": enc.name, "content": ["like", "%unsent laboratory tests withdrawn%"]},
			)
		)
		with self.assertRaises(ConsultationError):
			self.complete(enc.name)
		# Pending dispensing
		visit = self.visit_in_consultation()
		enc = self.open_encounter(visit)
		self.add_para(enc.name)
		self.complete(enc.name)
		self.assertEqual(self.state(visit.name).status, AP)
		with as_user(self.users["reception"]):
			apply_action(
				visit.name, "close_visit", closure_reason="Left before completion", comment="T- left"
			)
		self.assertEqual((self.state(visit.name).status, self.state(visit.name).ph), (D, 0))
		(mr,) = self.mrs(enc.name)
		record = self.dispense(mr, qty=10)
		self.assertEqual(dispensing.line_status(mr), "Dispensed")
		self.assertEqual(self.state(visit.name).status, D)
		self.assertTrue(self.skipped(visit.name, "dispensed"))
		with as_user(self.users["pharmacist"]), self.assertRaises(frappe.ValidationError):
			dispensing.cancel_dispensing(record.name, "T- wrong patient")
		self.assertEqual(dispensing.line_status(mr), "Dispensed")

	# 11
	def test_reconciler_catches_missed_events(self):
		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		self.collect_all(enc.name)
		frappe.db.set_value(VISIT_DOCTYPE, visit.name, "lab_open", 0)  # the event will be missed
		self.result_single(mrdt)
		self.assertEqual(visit_state(visit.name).status, AL)
		self.assertTrue(self.skipped(visit.name, "lab_results_ready"))
		frappe.db.set_value(VISIT_DOCTYPE, visit.name, "lab_open", 1)
		tasks.reconcile_open_tasks()
		s = self.state(visit.name)
		self.assertEqual((s.status, s.lab), (IC, 0))
		self.assertEqual(
			frappe.get_doc(VISIT_DOCTYPE, visit.name).status_log[-1].comment, tasks.RECONCILED_COMMENT
		)
		# Pharmacy
		self.add_para(enc.name)
		self.complete(enc.name)
		self.assertEqual(self.state(visit.name).status, AP)
		frappe.db.set_value(VISIT_DOCTYPE, visit.name, "pharmacy_open", 0)
		(mr,) = self.mrs(enc.name)
		self.dispense(mr, qty=10)
		self.assertEqual(visit_state(visit.name).status, AP)
		frappe.db.set_value(VISIT_DOCTYPE, visit.name, "pharmacy_open", 1)
		tasks.reconcile_open_tasks()
		self.assertEqual(self.state(visit.name).status, D)

	# 13
	def test_second_registration_with_stale_open_visit(self):
		old = make_visit(system=True, visit_date=add_days(today(), -3))
		with as_user(self.users["reception"]):
			with self.assertRaises(OpenVisitExistsError):
				make_visit(patient=old.patient, ignore_permissions=False)
			apply_action(
				old.name, "close_visit", closure_reason="Superseded by a new visit", comment="T- stale"
			)
			new = make_visit(patient=old.patient, ignore_permissions=False)
		self.assertEqual(new.status, R)
		self.assertEqual(self.state(old.name).closure_reason, "Superseded by a new visit")

	# 14
	def test_complete_with_results_pending_then_results(self):
		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		self.add_para(enc.name)
		with self.assertRaises(ConsultationError):
			self.complete(enc.name)  # reason required while results are pending
		self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc.name, "docstatus"), 0)
		self.complete(enc.name, reason="T- start treatment")
		s = self.state(visit.name)
		self.assertEqual((s.status, s.lab, s.ph), (AP, 1, 1))
		(mr,) = self.mrs(enc.name)
		self.dispense(mr, qty=10)
		self.assertEqual(self.state(visit.name).status, AL)
		self.collect_all(enc.name)
		self.result_single(mrdt)
		self.assertEqual(self.state(visit.name).status, D)

	# 15
	def test_concurrent_results_simulated(self):
		"""Both results committed before either handler runs (the interleaving visible in one transaction):
		exactly one lab_results_ready, the second handler is a logged no-op. The opposite interleaving (write
		skew, each sees the other pending) needs two connections with committed data; tests never commit, so
		the FOR UPDATE locking in lab.events cannot be exercised here."""
		visit = self.visit_in_consultation()
		enc, (mrdt, hb) = self.send_lab_orders(visit, [MRDT, HB])
		self.collect_all(enc.name)
		roots = [self.root(mrdt), self.root(hb)]
		with as_user(self.users["lab"]):
			record_observation_result(
				frappe.as_json(
					[
						{"observation": roots[0], "result": "Negative"},
						{"observation": roots[1], "result": "12"},
					]
				)
			)
		for name in roots:
			frappe.db.set_value("Observation", name, {"docstatus": 1, "status": "Approved"})
		for name in roots:
			lab_events.observation_on_submit(frappe.get_doc("Observation", name))
		s = self.state(visit.name)
		self.assertEqual((s.status, s.lab), (IC, 0))
		self.assertEqual(self.log_actions(visit.name).count("lab_results_ready"), 1)
		self.assertTrue(self.skipped(visit.name, "lab_results_ready"))

	# -- further edges ------------------------------------------------------------------------------------
	def test_merge_of_referred_patient_with_open_tasks(self):
		"""A Referred visit (closed) still has lab and pharmacy tasks: the merge is allowed, and the
		dispensing and the result that arrive afterwards still clear the source visit's tasks."""
		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		add_diagnosis(enc.name)
		self.add_para(enc.name)
		with as_user(self.users["clinician"]):
			ref.create_referral(enc.name, "External", "T- surgery", to_facility=FACILITY, send=True)
		s = self.state(visit.name)
		self.assertEqual((s.status, s.lab, s.ph), (REF, 1, 1))
		target = make_patient().name
		with as_user(self.users["mic"]):
			reg_api.merge_patients(visit.patient, target, "T- duplicate")
		self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit.name, "patient"), target)
		(mr,) = self.mrs(enc.name)
		self.dispense(mr, qty=10)
		self.collect_all(enc.name)
		self.result_single(mrdt)
		s2 = self.state(visit.name)
		self.assertEqual((s2.status, s2.lab, s2.ph, s2.closed_at), (REF, 0, 0, s.closed_at))

	def test_cancel_lab_order_after_completion_with_results_pending(self):
		"""Completed with results pending (Awaiting Laboratory), then the clinician withdraws the order
		before any sample: the lab task is complete and the visit is Discharged."""
		from onerc_healthcare.clinical.consultation import cancel_lab_order

		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		self.complete(enc.name, reason="T- treat empirically")
		self.assertEqual(self.state(visit.name).status, AL)
		with as_user(self.users["clinician"]):
			cancel_lab_order(mrdt, "T- patient declined the test")
		s = self.state(visit.name)
		self.assertEqual((s.status, s.lab), (D, 0))

	def test_reconciler_on_referred_visit(self):
		"""A missed lab event on a Referred visit: the reconciler clears the task, the visit stays Referred."""
		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		add_diagnosis(enc.name)
		with as_user(self.users["clinician"]):
			ref.create_referral(enc.name, "External", "T- surgery", to_facility=FACILITY, send=True)
		closed_at = self.state(visit.name).closed_at
		self.collect_all(enc.name)
		frappe.db.set_value(VISIT_DOCTYPE, visit.name, "lab_open", 0)
		self.result_single(mrdt)
		frappe.db.set_value(VISIT_DOCTYPE, visit.name, "lab_open", 1)
		self.assertEqual(tasks.reconcile_visit(visit.name), ["lab_results_ready"])
		s = self.state(visit.name)
		self.assertEqual((s.status, s.lab, s.closed_at), (REF, 0, closed_at))
