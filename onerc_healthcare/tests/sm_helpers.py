# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""S1 state-machine auditor helpers: independent oracles, invariants, actors and builders. Never commit.

The visit oracle (`expect_visit`) is written from the SDD §4.4 lifecycle table and the invariants in the
`visit.engine` module docstring, not from the engine's code. `EXPECTED_VISIT` is a literal copy of that table;
a meta-test compares it with `engine.TRANSITIONS`, and the matrix tests compare every outcome with the oracle.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field

import frappe
from frappe.utils import cint

from onerc_healthcare.constants import (
	DEPT_MENTAL_HEALTH,
	DEPT_OUTPATIENT,
	DEPT_TRIAGE,
	ENCOUNTER_DOCTYPE,
	ROLE_CLINICIAN,
	ROLE_MHO,
	ROLE_MIC,
	ROLE_OUTREACH,
	ROLE_RECEPTIONIST,
	ROLE_TRIAGE,
	VISIT_DOCTYPE,
	VISIT_STATUS_AWAITING_LAB,
	VISIT_STATUS_AWAITING_PHARMACY,
	VISIT_STATUS_DISCHARGED,
	VISIT_STATUS_IN_CONSULTATION,
	VISIT_STATUS_IN_TRIAGE,
	VISIT_STATUS_REFERRED,
	VISIT_STATUS_REGISTERED,
	VISIT_STATUS_ROUTED,
)
from onerc_healthcare.tests.factories import make_user
from onerc_healthcare.tests.visit_factories import VISIT_USERS, make_visit_users
from onerc_healthcare.visit import engine
from onerc_healthcare.visit.engine import (
	IllegalVisitTransitionError,
	PharmacyTaskOpenError,
	TriageVitalsMissingError,
	VisitGuardError,
)

R = VISIT_STATUS_REGISTERED
IT = VISIT_STATUS_IN_TRIAGE
RD = VISIT_STATUS_ROUTED
IC = VISIT_STATUS_IN_CONSULTATION
AL = VISIT_STATUS_AWAITING_LAB
AP = VISIT_STATUS_AWAITING_PHARMACY
D = VISIT_STATUS_DISCHARGED
REF = VISIT_STATUS_REFERRED
OPEN = (R, IT, RD, IC, AL, AP)
CLOSED = (D, REF)
PRE_CONSULT = (R, IT, RD)

SYSTEM = "system"
ADMIN = "Administrator"
CONSULT = (ROLE_CLINICIAN, ROLE_MHO)

# SDD §4.4 lifecycle (engine docstring + Phase 2 W0 decisions): action -> (from statuses, roles or SYSTEM).
EXPECTED_VISIT: dict[str, tuple[tuple[str, ...], tuple[str, ...] | str]] = {
	"start_triage": ((R,), (ROLE_TRIAGE,)),
	"route": ((IT,), (ROLE_TRIAGE,)),
	"reroute": ((RD,), (ROLE_TRIAGE,)),
	"return_to_triage": ((RD,), (ROLE_TRIAGE, *CONSULT)),
	"start_consultation": ((RD,), CONSULT),
	"request_lab": ((IC, AL, AP), SYSTEM),
	"lab_results_ready": ((AL, AP, REF), SYSTEM),
	"prescribe": ((IC, AL, AP), SYSTEM),
	"dispensed": ((AP, AL, IC, REF), SYSTEM),
	"discharge": ((IC,), CONSULT),
	"complete_consultation": ((IC, AL, AP), CONSULT),
	"refer": ((IC, AL, AP), CONSULT),
	"close_visit": (OPEN, (ROLE_RECEPTIONIST, ROLE_TRIAGE, *CONSULT, ROLE_MIC)),
	"receive_referral": ((R,), SYSTEM),
}

CLOSURE_REASON = "Other"


# --------------------------------------------------------------------------------------------- actors


def actor_users() -> dict[str, str]:
	"""{actor key: user} for one user per EMR role (visit roles + Outreach Team Member)."""
	users = make_visit_users()
	users["outreach"] = make_user("pv-outreach", [ROLE_OUTREACH])
	return users


ACTOR_ROLES = {**VISIT_USERS, "outreach": ROLE_OUTREACH}


@contextmanager
def as_user(user: str):
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(ADMIN)


@contextmanager
def savepoint(name: str):
	"""Run the block, then undo everything it wrote (a nested savepoint per combination)."""
	frappe.db.savepoint(name)
	try:
		yield
	finally:
		frappe.db.rollback(save_point=name)
		frappe.local.message_log = []


# --------------------------------------------------------------------------------------------- visit state


@dataclass
class VState:
	name: str
	status: str
	lab: int
	ph: int
	enc: str  # none / draft / submitted
	vitals: bool
	rwv: int
	dept: str | None
	closed_at: object
	closure_reason: str | None
	referred_from: str | None
	log_len: int
	completion_logged: bool = False
	log_last: dict = field(default_factory=dict)

	@property
	def completed(self) -> bool:
		return self.enc == "submitted"

	@property
	def closed(self) -> bool:
		return self.status in CLOSED

	def key(self) -> tuple:
		"""Exploration key: flags that change behaviour (vitals / rwv only matter before consultation)."""
		base = (self.status, self.lab, self.ph, self.enc)
		if self.status in PRE_CONSULT:
			return (*base, self.vitals, self.rwv)
		return base


def visit_state(name: str) -> VState:
	doc = frappe.get_doc(VISIT_DOCTYPE, name)
	enc = "none"
	if doc.encounter:
		docstatus = frappe.db.get_value(ENCOUNTER_DOCTYPE, doc.encounter, "docstatus")
		enc = {0: "draft", 1: "submitted"}.get(docstatus, "cancelled")
	last = doc.status_log[-1] if doc.status_log else None
	return VState(
		name=name,
		status=doc.status,
		lab=cint(doc.lab_open),
		ph=cint(doc.pharmacy_open),
		enc=enc,
		vitals=engine.has_submitted_triage_vitals(doc),
		rwv=cint(doc.routed_without_vitals),
		dept=doc.current_department,
		closed_at=doc.closed_at,
		closure_reason=doc.closure_reason,
		referred_from=doc.referred_from,
		log_len=len(doc.status_log),
		completion_logged=any(r.action == "complete_consultation" for r in doc.status_log),
		log_last=(
			{"from": last.from_status, "to": last.to_status, "action": last.action, "comment": last.comment}
			if last
			else {}
		),
	)


def completed_status(lab: int, ph: int) -> str:
	"""Invariant 4: after completion pharmacy first, then laboratory, else Discharged."""
	if ph:
		return AP
	if lab:
		return AL
	return D


def violations(s: VState) -> set[str]:
	"""Engine docstring invariants violated by `s` (empty for every reachable state)."""
	v = set()
	if s.closed != bool(s.closed_at):
		v.add("closed_at set exactly when closed")
	if s.status == AL and not s.lab:
		v.add("Awaiting Laboratory implies lab_open")
	if s.status == AP and not s.ph:
		v.add("Awaiting Pharmacy implies pharmacy_open")
	if s.status in PRE_CONSULT and (s.lab or s.ph):
		v.add("no tasks before consultation")
	if s.status == D and (s.lab or s.ph):
		v.add("Discharged leaves no open task")
	if s.status in (IC, AL, AP):
		if s.completed:
			if s.status != completed_status(s.lab, s.ph):
				v.add("completed visit status follows its tasks (never In Consultation)")
		elif s.lab and s.status != AL:
			v.add("before completion lab_open implies Awaiting Laboratory")
	if s.log_last.get("to") != s.status:
		v.add("last log row matches status")
	if s.closure_reason and s.status != D:
		v.add("closure reason only on a Discharged visit")
	return v


# --------------------------------------------------------------------------------------------- oracle


@dataclass
class Outcome:
	exc: type | tuple | None = None
	status: str | None = None
	lab: int | None = None
	ph: int | None = None
	dept: str | None = None
	rwv: int | None = None
	closure: bool = False

	@property
	def ok(self) -> bool:
		return self.exc is None


def rule_roles(action: str):
	"""Expected roles; `discharge` may become a non-user action (DEFECT-1b fix), read from the table then."""
	roles = EXPECTED_VISIT[action][1]
	rule = engine.TRANSITIONS.get(action)
	if action == "discharge" and rule is not None and rule.is_system:
		return SYSTEM
	return roles


def is_api_only(action: str) -> bool:
	"""Engine-internal action (DEFECT-1 fix: `Transition.api_only`): `apply_action` refuses it for everyone."""
	rule = engine.TRANSITIONS.get(action)
	return bool(rule is not None and getattr(rule, "api_only", False))


def reroute_target(dept: str | None) -> str:
	return DEPT_OUTPATIENT if dept == DEPT_MENTAL_HEALTH else DEPT_MENTAL_HEALTH


def visit_inputs(s: VState, action: str, referral: str | None, with_reason: bool = True) -> dict:
	"""The inputs the UI / caller supplies for `action` (the form asks for them through its flags)."""
	kw: dict = {}
	if action == "route":
		kw["department"] = DEPT_OUTPATIENT
		if with_reason and not s.vitals:
			kw["without_vitals_reason"] = "T- emergency"
	elif action == "reroute":
		kw["department"] = reroute_target(s.dept)
	elif action in ("return_to_triage", "refer"):
		kw["comment"] = "T- reason"
	elif action == "close_visit":
		kw["closure_reason"] = CLOSURE_REASON
		kw["comment"] = "T- left"
	elif action == "complete_consultation" and s.lab and with_reason:
		kw["comment"] = "T- results pending"
	elif action == "receive_referral":
		kw["department"] = DEPT_MENTAL_HEALTH
		kw["referral"] = referral
	return kw


def expect_visit(s: VState, action: str, actor: str, kw: dict, via_api: bool = False) -> Outcome:
	"""Expected result of `action` on state `s` by `actor` with inputs `kw` (SDD §4.4 + docstring)."""
	statuses, roles = EXPECTED_VISIT[action][0], rule_roles(action)
	if actor not in (SYSTEM, ADMIN):
		if roles == SYSTEM or ACTOR_ROLES[actor] not in roles:
			return Outcome(exc=frappe.PermissionError)
	if via_api and is_api_only(action):
		return Outcome(exc=(frappe.PermissionError, IllegalVisitTransitionError))
	if s.status not in statuses:
		return Outcome(exc=IllegalVisitTransitionError)
	# Guards
	if action == "lab_results_ready" and not s.lab:
		return Outcome(exc=VisitGuardError)
	if action == "dispensed" and not s.ph:
		return Outcome(exc=VisitGuardError)
	if action == "discharge":
		if s.ph:
			return Outcome(exc=PharmacyTaskOpenError)
		if s.lab or s.enc == "draft":
			return Outcome(exc=VisitGuardError)
	if action == "complete_consultation":
		if s.completion_logged:  # DEFECT-2 fix: a consultation is completed once
			return Outcome(exc=VisitGuardError)
		if not s.completed:
			return Outcome(exc=VisitGuardError)
		if s.lab and not kw.get("comment"):
			return Outcome(exc=IllegalVisitTransitionError)
	if action == "route" and not s.vitals and not kw.get("without_vitals_reason"):
		return Outcome(exc=TriageVitalsMissingError)
	if action == "receive_referral" and not kw.get("referral"):
		return Outcome(exc=IllegalVisitTransitionError)

	lab, ph = s.lab, s.ph
	out = Outcome()
	if action == "start_triage":
		out.status, out.dept = IT, DEPT_TRIAGE
	elif action == "route":
		out.status, out.dept = RD, DEPT_OUTPATIENT
		out.rwv = 1 if not s.vitals else s.rwv
	elif action == "reroute":
		out.status, out.dept = RD, reroute_target(s.dept)
	elif action == "return_to_triage":
		out.status, out.dept = IT, DEPT_TRIAGE
	elif action == "start_consultation":
		out.status = IC
	elif action == "receive_referral":
		out.status, out.dept = RD, DEPT_MENTAL_HEALTH
	elif action == "request_lab":
		lab = 1
		out.status = completed_status(lab, ph) if s.completed else AL
	elif action == "lab_results_ready":
		lab = 0
		if s.status == REF:
			out.status = REF
		elif s.completed:
			out.status = completed_status(lab, ph)
		else:
			out.status = IC  # results bring the patient back to the clinician
	elif action == "prescribe":
		ph = 1
		out.status = AP if s.completed else (AL if lab else AP)
	elif action == "dispensed":
		ph = 0
		if s.status == REF:
			out.status = REF
		elif s.completed:
			out.status = completed_status(lab, ph)
		else:
			out.status = AL if lab else IC
	elif action == "discharge":
		out.status = D
	elif action == "complete_consultation":
		out.status = completed_status(lab, ph)
	elif action == "refer":
		out.status = REF
	elif action == "close_visit":
		out.status, out.closure = D, True
		lab = ph = 0
	out.lab, out.ph = lab, ph
	return out


def human_offered(doc, user: str) -> dict[str, dict]:
	return {a["action"]: a for a in engine.get_available_actions(doc, user=user)}
