# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""S2 state-machine audit of the Phase 3 build (R1-R4 + W0). Never commit.

Every Phase 3 lifecycle is driven through its own API, by every relevant actor, from every status, and
compared with an independent oracle written from the module docstrings (the tables are copied below as
literals; meta-tests compare them with the code's introspectable `TRANSITIONS`). Known defects are kept as
``@unittest.expectedFailure`` tests that assert the *correct* behaviour (comment ``DEFECT-P3-n``), so they
turn into unexpected successes once fixed. Matrix oracles stay lenient on the exact combinations a
DEFECT-P3 test covers, so a fix never breaks a matrix.

Machines:

1. MOH Report Run (`reporting/run.py`): create / generate / submit_run / revise / void x status x role,
   one active run per period, frozen runs, queued generation, offered buttons.
2. Visit outcome (`visit/engine.py` `resolve_outcome`): close_visit reasons x outcomes x roles (with and
   without a recorded outcome), complete_consultation, refer, "Died", the outcome triple.
3. Attendance corrections (`visit/attendance.py`) and the intake fields.
4. Service Delivered (`community/services.py`).
5. Community Activity Record (`community/activities.py`).
6. Exact / estimated date of birth (`registration/dob.py`).
7. Offered vs working: visit form capture flags, outcome choices, closure reasons, run buttons.
"""

from __future__ import annotations

import unittest
from datetime import date
from typing import ClassVar
from unittest.mock import patch

import frappe
from dateutil.relativedelta import relativedelta
from frappe.utils import add_days, cint, cstr, getdate, today

from onerc_healthcare.clinical import referral as ref
from onerc_healthcare.clinical.consultation import complete_consultation, start_consultation
from onerc_healthcare.community import activities, services
from onerc_healthcare.community import setup as community_setup
from onerc_healthcare.constants import (
	ATTENDANCE_BASIS_HIS,
	ATTENDANCE_BASIS_RECEPTION,
	ATTENDANCE_NEW,
	ATTENDANCE_REVISIT,
	COMMUNITY_ACTIVITY_DOCTYPE,
	DEPT_MENTAL_HEALTH,
	DEPT_OUTPATIENT,
	ENCOUNTER_DOCTYPE,
	MOH_REPORT_DOCTYPE,
	MOH_RUN_DOCTYPE,
	OUTCOME_ADMITTED,
	OUTCOME_DIED,
	OUTCOME_DISCHARGED,
	OUTCOME_LEFT,
	OUTCOME_NOT_APPLICABLE,
	OUTCOME_REFERRED_INTERNAL,
	OUTCOME_REFERRED_OUT,
	REFERRAL_FACILITY_DOCTYPE,
	ROLE_CLINICIAN,
	ROLE_HIS,
	ROLE_MHO,
	ROLE_MIC,
	ROLE_OUTREACH,
	ROLE_RECEPTIONIST,
	ROLE_TRIAGE,
	SERVICE_DELIVERED_DOCTYPE,
	VISIT_CLOSURE_REASONS,
	VISIT_DOCTYPE,
	VISIT_OUTCOMES,
)
from onerc_healthcare.constants import (
	PATIENT_DOB_ESTIMATED_FIELD as FLAG,
)
from onerc_healthcare.constants import (
	PATIENT_DOB_ESTIMATED_ON_FIELD as ON,
)
from onerc_healthcare.constants import (
	PATIENT_ESTIMATED_AGE_MONTHS_FIELD as MONTHS,
)
from onerc_healthcare.constants import (
	PATIENT_ESTIMATED_AGE_YEARS_FIELD as YEARS,
)
from onerc_healthcare.emr_community.doctype.service_delivered.service_delivered import (
	ServiceDeliveredLockedError,
)
from onerc_healthcare.emr_visit.doctype.patient_visit.patient_visit import VisitStatusLockedError
from onerc_healthcare.registration import dob
from onerc_healthcare.reporting import definitions, export_pdf, export_xlsx, registry
from onerc_healthcare.reporting import run as run_module
from onerc_healthcare.reporting.period import month_period, previous_period
from onerc_healthcare.reporting.run import DuplicateRunError, IllegalTransitionError, LockedFieldError
from onerc_healthcare.tests import sm_helpers as sm
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import add_diagnosis, routed_visit
from onerc_healthcare.tests.factories import get_facility, make_patient, make_user
from onerc_healthcare.tests.sm_helpers import ADMIN, AL, AP, IC, IT, RD, REF, D, R, as_user, savepoint
from onerc_healthcare.tests.test_community import ACTIVITY, SERVICE
from onerc_healthcare.tests.test_reporting_framework import FAKE_PATHS, definition
from onerc_healthcare.tests.visit_factories import make_visit
from onerc_healthcare.visit import attendance, engine, outcome, queue
from onerc_healthcare.visit.engine import IllegalVisitTransitionError, VisitOutcomeError

PARA = "Paracetamol 500 mg Tablet"
REF_FACILITY = "T-S2 Lodwar County Referral Hospital"
SETTINGS = "EMR Registration Settings"
OVERRIDE_FIELD = "allow_reception_attendance_override"
ALL_VISIT_STATUSES = (R, IT, RD, IC, AL, AP, D, REF)

# Actor key -> role (users t-pv-<key>@test.krcs from sm.actor_users, plus S2 extras).
ROLE_OF = {**sm.ACTOR_ROLES, "clinician2": ROLE_CLINICIAN, "outreach2": ROLE_OUTREACH}


def _err_ok(err: BaseException | None, expected) -> bool:
	"""`expected` is an exception class or a tuple of acceptable classes."""
	return err is not None and isinstance(err, expected)


def _try(fn, *args, **kwargs):
	try:
		return fn(*args, **kwargs), None
	except Exception as e:  # compared with the oracle by the caller
		return None, e


def _months_back(n: int) -> tuple[date, date]:
	start = previous_period("Monthly")[0] - relativedelta(months=n)
	return month_period(start.year, start.month)


class P3Case(EMRTestCase):
	"""One user per role (sm.actor_users) plus a second Clinical Officer and Outreach member."""

	users: ClassVar[dict[str, str]] = {}

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users = sm.actor_users()
		cls.users["clinician2"] = make_user("s2-clinician2", [ROLE_CLINICIAN])
		cls.users["outreach2"] = make_user("s2-outreach2", [ROLE_OUTREACH])
		frappe.set_user(ADMIN)
		community_setup.install()

	def u(self, actor: str) -> str:
		return ADMIN if actor == ADMIN else self.users[actor]

	def role(self, actor: str) -> str:
		return ADMIN if actor == ADMIN else ROLE_OF[actor]

	def act_as(self, actor: str, fn, *args, **kwargs):
		with as_user(self.u(actor)):
			return _try(fn, *args, **kwargs)

	# -- visit builders --------------------------------------------------------------------------------
	def visit_in(self, status: str, closure_reason: str = "Other", **kw) -> str:
		"""A visit in `status`, reached through the engine as the system (like S1's forged `base`)."""
		v = make_visit(**kw)
		if status == R:
			return v.name
		engine.transition(v.name, "start_triage", system=True)
		if status == IT:
			return v.name
		engine.transition(
			v.name, "route", system=True, department=DEPT_OUTPATIENT, without_vitals_reason="T- emergency"
		)
		if status == RD:
			return v.name
		engine.transition(v.name, "start_consultation", system=True)
		if status == AL:
			engine.transition(v.name, "request_lab", system=True)
		elif status == AP:
			engine.transition(v.name, "prescribe", system=True)
		elif status == D:
			engine.transition(
				v.name, "close_visit", system=True, closure_reason=closure_reason, comment="T- s2"
			)
		elif status == REF:
			engine.transition(v.name, "refer", system=True, comment="T- s2")
		return v.name

	def consulting(self, actor: str = "clinician") -> tuple[str, str]:
		"""Visit In Consultation with its draft encounter (diagnosed), through `start_consultation`."""
		from onerc_healthcare.clinical import practitioner

		with as_user(self.users[actor]):
			practitioner.get_or_create()
		visit = routed_visit(DEPT_OUTPATIENT)
		with as_user(self.users[actor]):
			enc = start_consultation(visit.name)["encounter"]
		add_diagnosis(enc)
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

	def completed_awaiting_pharmacy(self, prior: str) -> str:
		"""Visit Awaiting Pharmacy after Complete Consultation recorded `prior` (clinician)."""
		visit, enc = self.consulting()
		self.add_para(enc)
		with as_user(self.users["clinician"]):
			complete_consultation(enc, outcome=prior)
		row = frappe.db.get_value(VISIT_DOCTYPE, visit, ["status", "outcome"], as_dict=True)
		self.assertEqual((row.status, row.outcome), (AP, prior))
		return visit

	def outcome_triple(self, visit: str) -> tuple:
		row = frappe.db.get_value(VISIT_DOCTYPE, visit, ["outcome", "outcome_at", "outcome_by"], as_dict=True)
		return (row.outcome, row.outcome_at, row.outcome_by)

	def log_len(self, visit: str) -> int:
		return frappe.db.count("Patient Visit Status Log", {"parent": visit, "parenttype": VISIT_DOCTYPE})

	def last_log(self, visit: str) -> dict:
		return frappe.get_all(
			"Patient Visit Status Log",
			filters={"parent": visit, "parenttype": VISIT_DOCTYPE},
			fields=["from_status", "to_status", "action", "user", "comment"],
			order_by="idx desc",
			limit=1,
		)[0]

	def referral_facility(self) -> str:
		name = frappe.db.get_value(REFERRAL_FACILITY_DOCTYPE, {"facility_name": REF_FACILITY}, "name")
		if name:
			return name
		return (
			frappe.get_doc(
				{
					"doctype": REFERRAL_FACILITY_DOCTYPE,
					"facility_name": REF_FACILITY,
					"facility_type": "Hospital",
					"county": "Turkana",
				}
			)
			.insert(ignore_permissions=True)
			.name
		)


# =====================================================================================================
# 1. MOH Report Run
# =====================================================================================================

DRAFT, GENERATED, SUBMITTED, SUPERSEDED, VOID = "Draft", "Generated", "Submitted", "Superseded", "Void"
QUEUED, FAILED = "Queued", "Failed"

# reporting/run.py docstring table: action -> (from statuses, to status, roles, reason required)
EXPECTED_RUN: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], bool]] = {
	"create": ((), DRAFT, (ROLE_HIS,), False),
	"generate": ((DRAFT, GENERATED), GENERATED, (ROLE_HIS,), False),
	"submit_run": ((GENERATED,), SUBMITTED, (ROLE_HIS,), False),
	"revise": ((SUBMITTED,), SUPERSEDED, (ROLE_HIS,), True),
	"void": ((DRAFT, GENERATED), VOID, (ROLE_HIS,), True),
}
RUN_ACTORS = ("his", "mic", "hoo", "clinician", "reception", ADMIN)
RUN_FROZEN = (SUBMITTED, SUPERSEDED, VOID)
# State label -> (status, generation_status)
RUN_STATES = {
	"Draft": (DRAFT, None),
	"Generated": (GENERATED, None),
	"Submitted": (SUBMITTED, None),
	"Superseded": (SUPERSEDED, None),
	"Void (from Draft)": (VOID, None),
	"Void (from Generated)": (VOID, None),
	"Draft queued": (DRAFT, QUEUED),
	"Generated queued": (GENERATED, QUEUED),
	"Draft failed": (DRAFT, FAILED),
	"Generated failed": (GENERATED, FAILED),
}


def expect_run(status: str, gen: str | None, action: str, role: str, reason: str | None):
	"""Expected exception class, or the new status (docstring table + bullets)."""
	if action == "create":
		return IllegalTransitionError  # never an apply_action action
	from_statuses, to_status, roles, needs_reason = EXPECTED_RUN[action]
	if role != ADMIN and role not in roles:
		return frappe.PermissionError
	if status not in from_statuses:
		return IllegalTransitionError
	if needs_reason and not reason:
		return IllegalTransitionError
	if gen == QUEUED and action in ("generate", "submit_run"):
		return IllegalTransitionError
	return to_status


class RunCase(P3Case):
	def setUp(self):
		super().setUp()
		self._patches = [
			patch.dict(registry.SOURCE_TYPES, FAKE_PATHS),
			patch.object(export_xlsx, "attach_xlsx"),  # exports are R1's own tests; keep the matrix fast
			patch.object(export_pdf, "attach_pdf"),
		]
		for p in self._patches:
			p.start()
		definitions.sync_definitions([definition(), definition(name="T-MOH TEST2")])
		self.facility = get_facility(0)
		self._period = 0

	def tearDown(self):
		for p in reversed(self._patches):
			p.stop()
		super().tearDown()

	def next_period(self) -> tuple[date, date]:
		period = _months_back(self._period)
		self._period += 1
		return period

	def new_run(self, actor="his", period=None, report="T-MOH TEST", **kw):
		f, t = period or self.next_period()
		values = {
			"doctype": MOH_RUN_DOCTYPE,
			"report": report,
			"facility": self.facility,
			"period_type": "Monthly",
			"from_date": f,
			"to_date": t,
			**kw,
		}
		with as_user(self.u(actor)):
			return frappe.get_doc(values).insert()

	def act(self, run, action, reason=None, actor="his"):
		with as_user(self.u(actor)):
			return run_module.apply_action(run, action, reason=reason)

	def run_in(self, label: str) -> str:
		name = self.new_run().name
		status, gen = RUN_STATES[label]
		if label == "Void (from Draft)":
			self.act(name, "void", "T- s2")
			return name
		if status in (GENERATED, SUBMITTED, SUPERSEDED) or label == "Void (from Generated)":
			self.act(name, "generate")
		if status in (SUBMITTED, SUPERSEDED):
			self.act(name, "submit_run")
		if status == SUPERSEDED:
			self.act(name, "revise", "T- s2 correction")
		if label == "Void (from Generated)":
			self.act(name, "void", "T- s2")
		if gen:
			frappe.db.set_value(
				MOH_RUN_DOCTYPE,
				name,
				{"generation_status": gen, "generation_error": "T-"},
				update_modified=False,
			)
		row = frappe.db.get_value(MOH_RUN_DOCTYPE, name, ["status", "generation_status"])
		self.assertEqual(tuple(row), (status, gen), label)
		return name

	def run_row(self, name: str) -> dict:
		doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
		return {"status": doc.status, "gen": doc.generation_status, "log": len(doc.log), "doc": doc}


class TestRunMachine(RunCase):
	def test_transition_table_matches_docstring(self):
		self.assertEqual(set(run_module.TRANSITIONS), set(EXPECTED_RUN))
		for action, rule in run_module.TRANSITIONS.items():
			from_statuses, to_status, roles, needs_reason = EXPECTED_RUN[action]
			self.assertEqual(
				(set(rule.from_statuses), rule.to_status, set(rule.roles), "reason" in rule.required),
				(set(from_statuses), to_status, set(roles), needs_reason),
				action,
			)
		self.assertEqual(set(run_module.FROZEN_STATUSES), set(RUN_FROZEN))
		self.assertEqual(set(run_module.INACTIVE_STATUSES), {VOID, SUPERSEDED})

	def check_run_step(self, name, before, action, exp, err, actor, reason, out):
		if isinstance(exp, type):
			self.assertTrue(_err_ok(err, exp), f"expected {exp.__name__}, got {err!r}")
			after = self.run_row(name)
			self.assertEqual(
				(after["status"], after["gen"], after["log"]),
				(before["status"], before["gen"], before["log"]),
			)
			return False
		self.assertIsNone(err, f"expected success, got {err!r}")
		doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
		self.assertEqual(doc.status, exp)
		self.assertEqual(len(doc.log), before["log"] + 1)
		last = doc.log[-1]
		self.assertEqual(
			(last.from_status, last.to_status, last.action, last.user),
			(before["status"], exp, action, self.u(actor)),
		)
		if action == "generate":
			self.assertTrue(doc.snapshot)
			self.assertEqual((doc.generated_by, doc.generation_status), (self.u(actor), None))
		elif action == "submit_run":
			self.assertEqual(doc.submitted_by, self.u(actor))
			self.assertTrue(doc.submitted_at)
		elif action == "void":
			self.assertEqual(
				(doc.void_reason, doc.voided_by, doc.generation_status), (reason, self.u(actor), None)
			)
		elif action == "revise":
			new = frappe.get_doc(MOH_RUN_DOCTYPE, out["new_run"])
			self.assertEqual((new.status, new.supersedes, doc.superseded_by), (DRAFT, name, new.name))
			self.assertEqual(
				(new.from_date, new.to_date, new.facility), (doc.from_date, doc.to_date, doc.facility)
			)
			self.assertEqual([r.action for r in new.log], ["create"])
		return True

	def test_action_matrix_and_buttons(self):
		"""Every action x every status (incl. queued / failed generation) x every role, with and without a
		reason; offered buttons (`get_available_actions`) == actions that work."""
		actions = ("create", "generate", "submit_run", "revise", "void")
		count = 0
		for label in RUN_STATES:
			name = self.run_in(label)
			status, gen = RUN_STATES[label]
			works: dict[str, set] = {a: set() for a in RUN_ACTORS}
			for action in actions:
				for actor in RUN_ACTORS:
					reasons = ("T- s2 reason", None) if EXPECTED_RUN[action][3] else (None,)
					for reason in reasons:
						exp = expect_run(status, gen, action, self.role(actor), reason)
						with self.subTest(state=label, action=action, actor=actor, reason=bool(reason)):
							with savepoint("s2_run"):
								count += 1
								before = self.run_row(name)
								out, err = self.act_as(
									actor, run_module.apply_action, name, action, reason=reason
								)
								ok = self.check_run_step(name, before, action, exp, err, actor, reason, out)
							if ok and reason == reasons[0]:
								works[actor].add(action)
			doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
			for actor in RUN_ACTORS:
				with self.subTest(state=label, offered_for=actor):
					offered = run_module.get_available_actions(doc, self.u(actor))
					self.assertEqual({a["action"] for a in offered}, works[actor])
					for a in offered:
						self.assertEqual(a["requires_reason"], EXPECTED_RUN[a["action"]][3], a)
		print(f"\n[S2] run matrix: {len(RUN_STATES)} states, {count} combinations")

	def test_create_by_role(self):
		"""create: HIS Officer (and Administrator) only; client-supplied protected values are ignored."""
		for actor in RUN_ACTORS:
			with self.subTest(actor=actor), savepoint("s2_create"):
				period = self.next_period()
				doc, err = _try(
					self.new_run,
					actor=actor,
					period=period,
					status=SUBMITTED,
					generation_status=QUEUED,
					snapshot='{"t": 1}',
					void_reason="T-",
				)
				if self.role(actor) in (ROLE_HIS, ADMIN):
					self.assertIsNone(err, repr(err))
					doc.reload()
					self.assertEqual(
						(doc.status, doc.generation_status, doc.snapshot, doc.void_reason),
						(DRAFT, None, None, None),
					)
					self.assertEqual(
						[(r.from_status or None, r.to_status, r.action) for r in doc.log],
						[(None, DRAFT, "create")],
					)
				else:
					self.assertTrue(_err_ok(err, frappe.PermissionError), repr(err))

	def test_one_active_run_per_period(self):
		"""Draft, Generated and Submitted block a second run; Void frees the period; a revision's new Draft
		is the active run; voiding it frees the period again; a hand-made `supersedes` is not an escape."""
		period = self.next_period()
		other = self.next_period()
		a = self.new_run(period=period).name

		def blocked(msg):
			with self.subTest(msg), savepoint("s2_dup"):
				_doc, err = _try(self.new_run, period=period)
				self.assertTrue(_err_ok(err, DuplicateRunError), f"{msg}: {err!r}")

		blocked("Draft")
		_doc, err = _try(self.new_run, period=period, report="T-MOH TEST2")
		self.assertIsNone(err, "another report, same period")
		_doc, err = _try(self.new_run, period=other)
		self.assertIsNone(err, "same report, another period")
		self.act(a, "generate")
		blocked("Generated")
		self.act(a, "submit_run")
		blocked("Submitted")
		_doc, err = _try(self.new_run, period=period, supersedes=a)
		self.assertTrue(_err_ok(err, DuplicateRunError), f"hand-made supersedes: {err!r}")
		b = self.act(a, "revise", "T- s2 correction")["new_run"]
		self.assertEqual(frappe.db.get_value(MOH_RUN_DOCTYPE, a, "status"), SUPERSEDED)
		blocked("revision Draft")
		self.act(b, "void", "T- s2 not needed")
		c = self.new_run(period=period).name  # Superseded + Void: free
		blocked("after void, new Draft")
		self.act(c, "void", "T- s2")
		d = self.new_run(period=period).name
		self.act(d, "generate")
		self.act(d, "submit_run")
		e = self.act(d, "revise", "T- s2 again")["new_run"]
		active = frappe.get_all(
			MOH_RUN_DOCTYPE,
			filters={
				"report": "T-MOH TEST",
				"facility": self.facility,
				"from_date": period[0],
				"to_date": period[1],
				"status": ["not in", [VOID, SUPERSEDED]],
			},
			pluck="name",
		)
		self.assertEqual(active, [e])

	def edit_value(self, doc, df, other_run: str):
		"""A different, valid value for field `df` (None: not editable by hand / no alternative)."""
		ft, cur = df.fieldtype, doc.get(df.fieldname)
		if df.fieldname in ("naming_series", "compiled_by_name"):  # compiled_by_name: DEFECT-P3-9
			return None
		if ft in ("Data", "Small Text", "Text", "Long Text", "Text Editor"):
			return f"T-S2 {df.fieldname}"
		if ft == "Int":
			return cint(cur) + 7
		if ft == "Check":
			return 0 if cint(cur) else 1
		if ft == "Date":
			return add_days(cur or today(), -40)
		if ft == "Datetime":
			return "2020-01-02 03:04:05"
		if ft == "Select":
			options = [o for o in (df.options or "").split("\n") if o != (cur or "")]
			return options[0] if options else None
		if ft in ("Attach", "Attach Image"):
			return "/files/t-s2.png"
		if ft == "JSON":
			return '{"t-s2": 1}'
		if ft == "Link":
			return {
				"User": self.users["mic"] if cur != self.users["mic"] else self.users["his"],
				MOH_REPORT_DOCTYPE: "T-MOH TEST2",
				"Healthcare Service Unit": get_facility(1),
				MOH_RUN_DOCTYPE: other_run,
			}.get(df.options)
		return None

	def test_frozen_runs_refuse_direct_edits(self):
		"""Submitted, Superseded and Void runs: every field and both tables refuse a direct `doc.save()`
		(HIS Officer and Administrator): an error, and nothing changed in the database."""
		other = self.run_in("Draft")
		layout = ("Section Break", "Column Break", "Tab Break", "HTML", "Table", "Button")
		for label in ("Submitted", "Superseded", "Void (from Draft)", "Void (from Generated)"):
			name = self.run_in(label)
			meta = frappe.get_meta(MOH_RUN_DOCTYPE)
			for df in meta.fields:
				if df.fieldtype in layout:
					continue
				for actor in ("his", ADMIN):
					with self.subTest(state=label, field=df.fieldname, actor=actor), savepoint("s2_frozen"):
						doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
						value = self.edit_value(doc, df, other)
						if value is None:
							continue
						before = frappe.db.get_value(MOH_RUN_DOCTYPE, name, df.fieldname)
						doc.set(df.fieldname, value)
						_out, err = self.act_as(actor, doc.save)
						after = frappe.db.get_value(MOH_RUN_DOCTYPE, name, df.fieldname)
						self.assertEqual(
							cstr(after), cstr(before), f"{df.fieldname} changed on a {label} run ({err!r})"
						)
			for table, row in (
				("log", {"from_status": DRAFT, "to_status": VOID, "action": "void", "user": ADMIN}),
				("exceptions", {"kind": "T-", "detail": "T- forged"}),
			):
				with self.subTest(state=label, table=table, edit="append"), savepoint("s2_frozen"):
					doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
					n = len(doc.get(table))
					doc.append(table, row)
					_out, err = self.act_as("his", doc.save)
					# refused by validation, or (review M2 hook) as a permission error on a frozen run
					self.assertTrue(
						err is None or isinstance(err, (frappe.ValidationError, frappe.PermissionError)),
						repr(err),
					)
					self.assertEqual(frappe.db.count(doc.meta.get_field(table).options, {"parent": name}), n)
			with self.subTest(state=label, table="log", edit="row"), savepoint("s2_frozen"):
				doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
				doc.log[0].comment = "T- forged"
				_out, err = self.act_as("his", doc.save)
				self.assertTrue(_err_ok(err, (LockedFieldError, frappe.PermissionError)), repr(err))

	def test_open_runs_edit_rules(self):
		"""Draft / Generated: the HIS Officer edits the narrative fields; parameters stay fixed; read-only
		roles cannot save."""
		for label in ("Draft", "Generated"):
			name = self.run_in(label)
			with self.subTest(state=label, edit="narrative"), savepoint("s2_open"):
				doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
				doc.narrative = "T- s2 narrative"
				_out, err = self.act_as("his", doc.save)
				self.assertIsNone(err, repr(err))
				self.assertEqual(frappe.db.get_value(MOH_RUN_DOCTYPE, name, "narrative"), "T- s2 narrative")
			for field, value in (
				("from_date", _months_back(30)[0]),
				("facility", get_facility(1)),
				("report", "T-MOH TEST2"),
			):
				with self.subTest(state=label, edit=field), savepoint("s2_open"):
					doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
					doc.set(field, value)
					_out, err = self.act_as("his", doc.save)
					self.assertTrue(_err_ok(err, LockedFieldError), repr(err))
			for actor in ("mic", "hoo", "clinician"):
				with self.subTest(state=label, edit="narrative", actor=actor), savepoint("s2_open"):
					doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
					doc.narrative = "T- s2 narrative"
					_out, err = self.act_as(actor, doc.save)
					self.assertTrue(_err_ok(err, frappe.PermissionError), repr(err))

	def test_real_queued_generation_blocks_generate_and_submit(self):
		"""A long period is queued for real (enqueue patched): Generate and Mark Submitted are refused and
		not offered; Void is; the job finds the voided run and stops."""
		end = add_days(today(), -1)
		f = getdate(end) - relativedelta(days=120)
		name = self.new_run(period=(f, getdate(end)), period_type="Custom").name
		with patch("frappe.enqueue"):
			self.act(name, "generate")
		with as_user(self.users["his"]):
			run_module.run_generation_job(name)  # what the worker runs: Draft -> Generated
		with patch("frappe.enqueue") as enqueue:
			out = self.act(name, "generate")
		self.assertTrue(out["queued"])
		enqueue.assert_called_once()
		doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
		self.assertEqual((doc.status, doc.generation_status), (GENERATED, QUEUED))
		for action in ("generate", "submit_run"):
			with patch("frappe.enqueue"):
				_out, err = _try(self.act, name, action)
			self.assertTrue(_err_ok(err, IllegalTransitionError), f"{action}: {err!r}")
		self.assertEqual(
			{a["action"] for a in run_module.get_available_actions(doc, self.users["his"])}, {"void"}
		)
		self.act(name, "void", "T- s2")
		with as_user(self.users["his"]):
			run_module.run_generation_job(name)
		self.assertEqual(
			frappe.db.get_value(MOH_RUN_DOCTYPE, name, ["status", "generation_status"]), (VOID, None)
		)

	# DEFECT-P3-1: Generate / Revise offered on runs of a Blocked report, then refused
	def test_blocked_report_offered_vs_works(self):
		"""DEFECT-P3-1: once a report is Blocked, Generate (Draft / Generated runs) and Revise (Submitted runs)
		are still offered as buttons, but both raise ReportBlockedError."""
		draft = self.run_in("Draft")
		submitted = self.run_in("Submitted")
		frappe.db.set_value(
			MOH_REPORT_DOCTYPE, "T-MOH TEST", {"status": "Blocked", "blocked_reason": "T- s2"}
		)
		broken = []
		for name in (draft, submitted):
			doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
			for a in run_module.get_available_actions(doc, self.users["his"]):
				with savepoint("s2_blocked"):
					_out, err = _try(self.act, name, a["action"], "T- s2")
				if err is not None:
					broken.append(f"{doc.status}: {a['action']} offered but {err!r}")
		self.assertEqual(broken, [])

	# DEFECT-P3-9: compiled_by_name editable on a frozen run without compiled_by
	def test_frozen_run_compiled_by_name(self):
		"""DEFECT-P3-9: `compiled_by_name` is in none of the run's field lists (PROTECTED / PARAMETER /
		EDITABLE), so on a frozen run without `compiled_by` (voided Draft, or generated by Administrator) a
		plain save changes it (the fetch from `compiled_by` only resets it when compiled_by is set)."""
		name = self.run_in("Void (from Draft)")
		doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
		doc.compiled_by_name = "T- forged compiler"
		_out, err = self.act_as("his", doc.save)
		self.assertFalse(frappe.db.get_value(MOH_RUN_DOCTYPE, name, "compiled_by_name"), repr(err))


# =====================================================================================================
# 2. Visit outcomes
# =====================================================================================================

# W0 contract (phase3_builder_brief.md "Visit engine") as literals.
EXP_CLOSURE_OUTCOMES = {
	"Left before being seen": OUTCOME_LEFT,
	"Left before completion": OUTCOME_LEFT,
	"Superseded by a new visit": OUTCOME_NOT_APPLICABLE,
	"Registered in error": OUTCOME_NOT_APPLICABLE,
	"Died": OUTCOME_DIED,
	"Other": None,
}
EXP_CLOSE_OTHER = (OUTCOME_NOT_APPLICABLE, OUTCOME_DISCHARGED, OUTCOME_ADMITTED, OUTCOME_LEFT)
EXP_COMPLETE = (OUTCOME_DISCHARGED, OUTCOME_ADMITTED, OUTCOME_DIED)
EXP_REFER = (OUTCOME_REFERRED_OUT, OUTCOME_REFERRED_INTERNAL)
EXP_DIED_ROLES = (ROLE_CLINICIAN, ROLE_MHO, ROLE_MIC)
CLOSE_ACTORS = ("reception", "triage", "clinician", "mho", "mic", ADMIN)
OUTCOME_ARGS = (None, *VISIT_OUTCOMES)


def may_record_death(role: str) -> bool:
	return role == ADMIN or role in EXP_DIED_ROLES


def expect_close(reason: str, given: str | None, role: str, prior: str | None):
	"""(acceptable exceptions or None, resolved outcome) for close_visit (W0 + resolve_outcome docstring).

	Implied reason -> that outcome only; "Other" -> CLOSE_OTHER (default Not applicable). A recorded
	(Complete Consultation) outcome is always kept unless the reason is Died; passing a different outcome
	is refused (DEFECT-P3-2 fix). Died needs a DIED_ROLES role (Administrator passes)."""
	if role != ADMIN and role not in (ROLE_RECEPTIONIST, ROLE_TRIAGE, ROLE_CLINICIAN, ROLE_MHO, ROLE_MIC):
		return frappe.PermissionError, None
	implied = EXP_CLOSURE_OUTCOMES[reason]
	allowed = (implied,) if implied else EXP_CLOSE_OTHER
	errors = []
	if prior and implied != OUTCOME_DIED:
		if given and given != prior:
			errors.append(VisitOutcomeError)
		resolved = prior
	else:
		if given and given not in allowed:
			errors.append(VisitOutcomeError)
		resolved = given or implied or OUTCOME_NOT_APPLICABLE
	if resolved == OUTCOME_DIED and not may_record_death(role) and resolved != prior:
		errors.append(frappe.PermissionError)
	if reason == "Died" and not may_record_death(role):
		errors.append(frappe.PermissionError)
	if errors:
		return tuple(set(errors)), None
	return None, resolved


class TestVisitOutcomes(P3Case):
	def test_outcome_tables_match_contract(self):
		self.assertEqual(
			{r: engine.CLOSURE_OUTCOMES.get(r) for r in VISIT_CLOSURE_REASONS}, EXP_CLOSURE_OUTCOMES
		)
		self.assertEqual(tuple(engine.CLOSE_OTHER_OUTCOMES), EXP_CLOSE_OTHER)
		self.assertEqual(tuple(engine.COMPLETE_OUTCOMES), EXP_COMPLETE)
		self.assertEqual(tuple(engine.REFER_OUTCOMES), EXP_REFER)
		from onerc_healthcare.constants import DIED_ROLES

		self.assertEqual(set(DIED_ROLES), set(EXP_DIED_ROLES))
		self.assertEqual(len(VISIT_OUTCOMES), 7)

	def close_matrix(self, visit: str, prior: str | None, actors=CLOSE_ACTORS, lenient=frozenset()):
		"""Run close_visit for every reason x outcome x actor on `visit`; returns {actor: {reason: set of
		explicit outcomes that worked (None = left empty)}}."""
		before_status = frappe.db.get_value(VISIT_DOCTYPE, visit, "status")
		prior_triple = self.outcome_triple(visit)
		works: dict = {a: {} for a in actors}
		n = 0
		for actor in actors:
			role = self.role(actor)
			for reason in VISIT_CLOSURE_REASONS:
				for given in OUTCOME_ARGS:
					exc, resolved = expect_close(reason, given, role, prior)
					with (
						self.subTest(prior=prior, actor=actor, reason=reason, outcome=given),
						savepoint("s2_close"),
					):
						n += 1
						logs = self.log_len(visit)
						_out, err = self.act_as(
							actor,
							engine.apply_action,
							visit,
							"close_visit",
							comment="T- s2",
							closure_reason=reason,
							outcome=given,
						)
						row = frappe.db.get_value(
							VISIT_DOCTYPE,
							visit,
							["status", "closure_reason", "outcome", "outcome_by", "outcome_at"],
							as_dict=True,
						)
						if (actor, reason, given) in lenient:
							continue
						if exc:
							self.assertTrue(_err_ok(err, exc), f"expected {exc}, got {err!r}")
							self.assertEqual((row.status, self.log_len(visit)), (before_status, logs))
							self.assertEqual(self.outcome_triple(visit), prior_triple)
							continue
						self.assertIsNone(err, f"expected success, got {err!r}")
						works[actor].setdefault(reason, set()).add(given)
						self.assertEqual((row.status, row.closure_reason, row.outcome), (D, reason, resolved))
						if resolved != prior:
							self.assertEqual(row.outcome_by, self.u(actor))
							self.assertTrue(row.outcome_at)
						else:
							self.assertEqual((row.outcome, row.outcome_at, row.outcome_by), prior_triple)
		return works, n

	def check_close_offers(self, visit: str, works: dict, prior: str | None):
		"""closure_reasons_for / get_outcome_choices offer exactly what close_visit accepts."""
		for actor, by_reason in works.items():
			user = self.u(actor)
			with self.subTest(prior=prior, offered_reasons=actor):
				self.assertEqual(set(engine.closure_reasons_for(user)), set(by_reason))
			for reason, outcomes in by_reason.items():
				with self.subTest(prior=prior, offered_outcomes=actor, reason=reason):
					with as_user(user):
						info = outcome.get_outcome_choices(visit, "close_visit", reason)
					self.assertIn(None, outcomes, "leaving the outcome empty always works")
					self.assertEqual(set(info["choices"]), outcomes - {None})
					_exc, resolved = expect_close(reason, None, self.role(actor), prior)
					self.assertEqual(info["current"] if info["keeps_current"] else info["default"], resolved)

	def test_close_visit_matrix_without_outcome(self):
		"""Registered visit: 6 reasons x 8 outcome arguments x 6 actors; offers match."""
		visit = self.visit_in(R)
		works, n = self.close_matrix(visit, None)
		self.check_close_offers(visit, works, None)
		print(f"\n[S2] close_visit matrix (no prior outcome): {n} combinations")

	def test_close_visit_matrix_with_consultation_outcome(self):
		"""Awaiting Pharmacy after Complete Consultation (Discharged home / Admitted / Died): the recorded
		outcome is always kept unless the reason is Died; a different outcome passed with the close is
		refused (DEFECT-P3-2 fix)."""
		n = 0
		for prior in (OUTCOME_DISCHARGED, OUTCOME_ADMITTED, OUTCOME_DIED):
			visit = self.completed_awaiting_pharmacy(prior)
			works, k = self.close_matrix(visit, prior)
			n += k
			self.check_close_offers(visit, works, prior)
		print(f"\n[S2] close_visit matrix (recorded outcome): {n} combinations")

	# DEFECT-P3-2 (fixed): reception / triage close_visit replaced a recorded Died outcome
	def test_died_not_erased_by_non_clinical_role(self):
		"""DEFECT-P3-2: a Nurse Receptionist / Triage Nurse closing a visit whose consultation recorded
		"Died" can replace it ("Other" + Admitted, or "Left before completion" + Left without completing):
		undoing a death is as clinical as recording one (DIED_ROLES)."""
		visit = self.completed_awaiting_pharmacy(OUTCOME_DIED)
		for actor, reason, given in (
			("reception", "Other", OUTCOME_ADMITTED),
			("triage", "Left before completion", OUTCOME_LEFT),
			("reception", "Registered in error", OUTCOME_NOT_APPLICABLE),
		):
			with savepoint("s2_died"):
				_out, err = self.act_as(
					actor,
					engine.apply_action,
					visit,
					"close_visit",
					comment="T-",
					closure_reason=reason,
					outcome=given,
				)
				self.assertEqual(
					frappe.db.get_value(VISIT_DOCTYPE, visit, "outcome"),
					OUTCOME_DIED,
					f"{actor} {reason} {given} {err!r}",
				)

	def test_complete_consultation_outcomes(self):
		"""complete_consultation x outcome argument x role; the dialog's choices == outcomes accepted."""
		actors = ("reception", "triage", "clinician", "mho", "mic", "his", ADMIN)
		own = {"clinician": self.consulting("clinician"), "mho": self.consulting("mho")}
		for actor in actors:
			visit, enc = own.get(actor, own["clinician"])
			role = self.role(actor)
			works = set()
			for given in OUTCOME_ARGS:
				with self.subTest(actor=actor, outcome=given), savepoint("s2_complete"):
					before = self.outcome_triple(visit)
					_out, err = self.act_as(
						actor, complete_consultation, enc, no_diagnosis_reason="T-", outcome=given
					)
					if role not in (ROLE_CLINICIAN, ROLE_MHO, ADMIN):
						self.assertTrue(_err_ok(err, frappe.PermissionError), repr(err))
						self.assertEqual(self.outcome_triple(visit), before)
						continue
					if given and given not in EXP_COMPLETE:
						self.assertTrue(_err_ok(err, VisitOutcomeError), repr(err))
						self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "docstatus"), 0)
						self.assertEqual(self.outcome_triple(visit), before)
						continue
					self.assertIsNone(err, repr(err))
					works.add(given)
					o, at, by = self.outcome_triple(visit)
					self.assertEqual((o, by), (given or OUTCOME_DISCHARGED, self.u(actor)))
					self.assertTrue(at)
			if role in (ROLE_CLINICIAN, ROLE_MHO, ADMIN):
				with self.subTest(offered_for=actor):
					with as_user(self.u(actor)):
						info = outcome.get_outcome_choices(visit, "complete_consultation")
					self.assertEqual(set(info["choices"]), works - {None})
					self.assertEqual(info["default"], OUTCOME_DISCHARGED)

	def test_refer_outcomes(self):
		"""refer: apply_action refuses it for everyone (api_only); the engine API by role x outcome; the
		Referral `send` records Referred internally / Referred out by type."""
		visit = self.visit_in(IC)
		for actor in ("reception", "triage", "clinician", "mho", "mic", "his", ADMIN):
			role = self.role(actor)
			with self.subTest(actor=actor, path="apply_action"), savepoint("s2_refer"):
				_out, err = self.act_as(actor, engine.apply_action, visit, "refer", comment="T-")
				self.assertTrue(_err_ok(err, frappe.PermissionError), repr(err))
			for given in OUTCOME_ARGS:
				with self.subTest(actor=actor, outcome=given), savepoint("s2_refer"):
					_out, err = self.act_as(
						actor, engine.transition, visit, "refer", comment="T-", outcome=given
					)
					if role not in (ROLE_CLINICIAN, ROLE_MHO, ADMIN):
						self.assertTrue(_err_ok(err, frappe.PermissionError), repr(err))
						continue
					if given and given not in EXP_REFER:
						self.assertTrue(_err_ok(err, VisitOutcomeError), repr(err))
						self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit, "status"), IC)
						continue
					self.assertIsNone(err, repr(err))
					self.assertEqual(
						tuple(frappe.db.get_value(VISIT_DOCTYPE, visit, ["status", "outcome", "outcome_by"])),
						(REF, given or OUTCOME_REFERRED_OUT, self.u(actor)),
					)
		facility = self.referral_facility()
		for rtype, values, expected in (
			("Internal", {"to_department": DEPT_MENTAL_HEALTH}, OUTCOME_REFERRED_INTERNAL),
			("External", {"to_facility": facility}, OUTCOME_REFERRED_OUT),
		):
			with self.subTest(referral=rtype):
				v, enc = self.consulting()
				with as_user(self.users["clinician"]):
					ref.create_referral(enc, rtype, "T- s2", send=True, **values)
				self.assertEqual(
					tuple(frappe.db.get_value(VISIT_DOCTYPE, v, ["status", "outcome", "outcome_by"])),
					(REF, expected, self.users["clinician"]),
				)

	def test_died_never_recordable_by_non_clinical_roles(self):
		"""Every path that can set an outcome: close_visit (matrix + here), complete_consultation (role
		refused), engine.annotate, the R3 correction endpoints (no outcome argument, extra keys dropped)."""
		actors = (
			"reception",
			"triage",
			"lab",
			"pharmacist",
			"his",
			"hoo",
			"outreach",
			"clinician",
			"mho",
			"mic",
			ADMIN,
		)
		visit = self.visit_in(R)
		for actor in actors:
			role = self.role(actor)
			with self.subTest(actor=actor, path="annotate"), savepoint("s2_died"):
				_out, err = self.act_as(
					actor, engine.annotate, visit, "correct_outcome", "T- s2", {"outcome": OUTCOME_DIED}
				)
				got = frappe.db.get_value(VISIT_DOCTYPE, visit, "outcome")
				if may_record_death(role):
					self.assertIsNone(err, repr(err))
					self.assertEqual(got, OUTCOME_DIED)
				else:
					self.assertTrue(_err_ok(err, frappe.PermissionError), repr(err))
					self.assertIsNone(got)
			for reason in ("Died", "Other"):
				with self.subTest(actor=actor, path="close_visit", reason=reason), savepoint("s2_died"):
					_out, err = self.act_as(
						actor,
						engine.apply_action,
						visit,
						"close_visit",
						comment="T-",
						closure_reason=reason,
						outcome=OUTCOME_DIED,
					)
					got = frappe.db.get_value(VISIT_DOCTYPE, visit, "outcome")
					if not may_record_death(role) or reason == "Other":
						self.assertIsNotNone(err)
						self.assertIsNone(got)
			for method, kwargs in (
				(attendance.correct_attendance, {"attendance_type": ATTENDANCE_REVISIT}),
				(attendance.correct_referred_in, {"referred_in": 1}),
			):
				with self.subTest(actor=actor, path=method.__name__), savepoint("s2_died"):
					self.act_as(
						actor,
						frappe.call,
						method,
						visit=visit,
						reason="T- s2",
						outcome=OUTCOME_DIED,
						closure_reason="Died",
						**kwargs,
					)
					self.assertEqual(self.outcome_triple(visit), (None, None, None))

	# DEFECT-P3-3 (fixed): Close previous visit offered Died to reception
	def test_close_previous_visit_offers_only_allowed_reasons(self):
		"""DEFECT-P3-3: the new-visit form's "Close previous visit" dialog (queue.get_open_visit) offers every
		closure reason, "Died" included, to the Nurse Receptionist; close_visit then refuses it."""
		visit = self.visit_in(IT)
		patient = frappe.db.get_value(VISIT_DOCTYPE, visit, "patient")
		for actor in ("reception", "triage"):
			with as_user(self.u(actor)):
				offered = queue.get_open_visit(patient)["closure_reasons"]
			for reason in offered:
				with savepoint("s2_prev"):
					_out, err = self.act_as(
						actor, engine.apply_action, visit, "close_visit", comment="T-", closure_reason=reason
					)
				self.assertIsNone(err, f"{actor}: {reason} offered but {err!r}")

	def test_outcome_changes_only_on_closing_actions_and_annotate(self):
		"""The outcome triple stays put through every non-closing action, system event, correction and
		patient correction; a non-closing action given an outcome is refused."""
		visit = self.visit_in(R)
		patient2 = make_patient().name
		engine.annotate(visit, "t_s2_seed", "T- seed", {"outcome": OUTCOME_ADMITTED})
		triple = self.outcome_triple(visit)
		self.assertEqual(triple[0], OUTCOME_ADMITTED)
		with as_user(self.users["triage"]):
			_out, err = _try(engine.apply_action, visit, "start_triage", outcome=OUTCOME_DISCHARGED)
		self.assertTrue(_err_ok(err, VisitOutcomeError), repr(err))
		self.assertEqual(self.outcome_triple(visit), triple)

		doc = frappe.get_doc(VISIT_DOCTYPE, visit)
		doc.patient = patient2
		with as_user(self.users["reception"]):
			doc.save()
		self.assertEqual(self.outcome_triple(visit), triple, "patient correction")
		with as_user(self.users["his"]):
			attendance.correct_attendance(visit, ATTENDANCE_REVISIT, "T- s2")
			attendance.correct_referred_in(visit, 1, "T- s2", self.referral_facility())
		self.assertEqual(self.outcome_triple(visit), triple, "corrections")
		steps = [
			("start_triage", {}),
			("route", {"department": DEPT_OUTPATIENT, "without_vitals_reason": "T-"}),
			("reroute", {"department": DEPT_MENTAL_HEALTH}),
			("return_to_triage", {"comment": "T-"}),
			("route", {"department": DEPT_OUTPATIENT, "without_vitals_reason": "T-"}),
			("start_consultation", {}),
			("request_lab", {}),
			("prescribe", {}),
			("lab_results_ready", {}),
			("dispensed", {}),
			("request_lab", {}),
		]
		for action, kw in steps:
			engine.transition(visit, action, system=True, **kw)
			self.assertEqual(self.outcome_triple(visit), triple, action)
		engine.transition(visit, "close_visit", system=True, closure_reason="Other", comment="T-")
		self.assertEqual(self.outcome_triple(visit), triple, "close_visit keeps a recorded outcome")
		with as_user(self.users["his"]):
			attendance.correct_attendance(visit, ATTENDANCE_NEW, "T- s2 closed")
		self.assertEqual(self.outcome_triple(visit), triple, "correction on a closed visit")
		engine.annotate(visit, "correct_outcome", "T- s2", {"outcome": OUTCOME_DISCHARGED})
		self.assertEqual(self.outcome_triple(visit)[0], OUTCOME_DISCHARGED, "annotate changes it")


# =====================================================================================================
# 3. Attendance corrections and intake fields
# =====================================================================================================

# visit/attendance.py docstring table: endpoint -> (any-time roles, Registered-only roles, reception
# needs the override setting)
EXPECTED_CORRECTIONS = {
	"correct_attendance": ((ROLE_HIS,), (ROLE_RECEPTIONIST,), True),
	"correct_referred_in": ((ROLE_HIS,), (ROLE_RECEPTIONIST,), False),
}
CORRECTION_ACTORS = ("reception", "triage", "clinician", "mho", "mic", "his", "hoo", "lab", ADMIN)


def expect_correction(action: str, role: str, status: str, override_on: bool) -> bool:
	anytime, registered, needs_setting = EXPECTED_CORRECTIONS[action]
	if role == ADMIN or role in anytime:
		return True
	if role in registered and status == R:
		return override_on or not needs_setting
	return False


class TestAttendanceCorrections(P3Case):
	def test_corrections_table_matches_docstring(self):
		for action, rule in attendance.CORRECTIONS.items():
			self.assertEqual(
				(rule.anytime_roles, rule.registered_roles, rule.reception_needs_override_setting),
				EXPECTED_CORRECTIONS[action],
			)

	def test_correction_matrix(self):
		"""correct_attendance / correct_referred_in x role x every visit status x override setting; basis,
		log row (from = to) and the form's capture flags agree with what works."""
		facility = self.referral_facility()
		visits = {status: self.visit_in(status) for status in ALL_VISIT_STATUSES}
		n = 0
		for override_on in (True, False):
			frappe.db.set_single_value(SETTINGS, OVERRIDE_FIELD, 1 if override_on else 0)
			for status, visit in visits.items():
				for actor in CORRECTION_ACTORS:
					role = self.role(actor)
					works = {}
					for action in EXPECTED_CORRECTIONS:
						expected = expect_correction(action, role, status, override_on)
						with self.subTest(setting=override_on, status=status, actor=actor, action=action):
							with savepoint("s2_correct"):
								n += 1
								before = frappe.db.get_value(
									VISIT_DOCTYPE,
									visit,
									["status", "attendance_type", "referred_in"],
									as_dict=True,
								)
								logs = self.log_len(visit)
								if action == "correct_attendance":
									target = (
										ATTENDANCE_REVISIT
										if before.attendance_type == ATTENDANCE_NEW
										else ATTENDANCE_NEW
									)
									_out, err = self.act_as(
										actor, attendance.correct_attendance, visit, target, "T- s2"
									)
								else:
									_out, err = self.act_as(
										actor, attendance.correct_referred_in, visit, 1, "T- s2", facility
									)
								works[action] = err is None
								if not expected:
									self.assertTrue(_err_ok(err, frappe.PermissionError), repr(err))
									self.assertEqual(self.log_len(visit), logs)
									continue
								self.assertIsNone(err, repr(err))
								row = frappe.db.get_value(
									VISIT_DOCTYPE,
									visit,
									[
										"status",
										"attendance_type",
										"attendance_basis",
										"referred_in",
										"referred_in_from",
									],
									as_dict=True,
								)
								self.assertEqual(row.status, before.status)
								if action == "correct_attendance":
									basis = (
										ATTENDANCE_BASIS_HIS
										if role in (ROLE_HIS, ADMIN)
										else ATTENDANCE_BASIS_RECEPTION
									)
									self.assertEqual(
										(row.attendance_type, row.attendance_basis), (target, basis)
									)
								else:
									self.assertEqual((row.referred_in, row.referred_in_from), (1, facility))
								last = self.last_log(visit)
								self.assertEqual(
									(last.from_status, last.to_status, last.action, last.user),
									(status, status, action, self.u(actor)),
								)
					with self.subTest(setting=override_on, status=status, offered_for=actor):
						with as_user(self.u(actor)):
							flags, err = _try(outcome.get_visit_capture, visit)
						if err is None:
							self.assertEqual(
								(flags["can_correct_attendance"], flags["can_correct_referred_in"]),
								(works["correct_attendance"], works["correct_referred_in"]),
							)
						for action in EXPECTED_CORRECTIONS:
							self.assertEqual(
								attendance.can_correct(action, status, self.u(actor)), works[action], action
							)
		print(f"\n[S2] correction matrix: {n} combinations")

	def test_correction_needs_reason_and_change(self):
		visit = self.visit_in(R)
		for actor in ("reception", "his"):
			for reason in ("", "   ", None):
				with savepoint("s2_reason"):
					_out, err = self.act_as(
						actor, attendance.correct_attendance, visit, ATTENDANCE_REVISIT, reason
					)
					self.assertTrue(_err_ok(err, attendance.AttendanceCorrectionError), repr(err))
					__out, err = self.act_as(actor, attendance.correct_referred_in, visit, 1, reason)
					self.assertTrue(_err_ok(err, attendance.AttendanceCorrectionError), repr(err))

	def test_intake_and_engine_fields_refused_by_direct_save(self):
		"""After insert, the intake fields (and the outcome / attendance fields) never change through a
		plain save, for any role: refused (Administrator: VisitStatusLockedError) and unchanged."""
		facility = self.referral_facility()
		visit = self.visit_in(R)
		edits = {
			"seen_in_smartcare": 1,
			"referred_in": 1,
			"referred_in_from": facility,
			"outcome": OUTCOME_DIED,
			"outcome_at": "2026-01-01 10:00:00",
			"outcome_by": self.users["clinician"],
			"attendance_type": ATTENDANCE_REVISIT,
			"attendance_basis": ATTENDANCE_BASIS_HIS,
			"attendance_note": "T- forged",
		}
		for actor in (
			"reception",
			"triage",
			"clinician",
			"mho",
			"mic",
			"his",
			"hoo",
			"lab",
			"pharmacist",
			ADMIN,
		):
			for field, value in edits.items():
				with self.subTest(actor=actor, field=field), savepoint("s2_intake"):
					before = frappe.db.get_value(VISIT_DOCTYPE, visit, field)
					doc = frappe.get_doc(VISIT_DOCTYPE, visit)
					doc.set(field, value)
					with as_user(self.u(actor)):
						writable = frappe.has_permission(VISIT_DOCTYPE, "write", doc=visit) and (
							doc.has_permlevel_access_to(field, permission_type="write")
						)
					_out, err = self.act_as(actor, doc.save)
					self.assertEqual(cstr(frappe.db.get_value(VISIT_DOCTYPE, visit, field)), cstr(before))
					if writable:  # the change reaches the controller: refused, not silently dropped
						self.assertTrue(_err_ok(err, VisitStatusLockedError), repr(err))


# =====================================================================================================
# 4. Service Delivered
# =====================================================================================================

SERVICE_RECORD_ROLES = (ROLE_CLINICIAN, ROLE_MHO)
SERVICE_ANY_VOID_ROLES = (ROLE_MIC,)
EXCLUDED_CLOSURES = ("Registered in error", "Superseded by a new visit")
SERVICE_ACTORS = ("reception", "triage", "clinician", "mho", "mic", "his", "outreach", "lab", ADMIN)


class TestServiceDelivered(P3Case):
	def record(self, actor, visit, **kw):
		values = {"doctype": SERVICE_DELIVERED_DOCTYPE, "visit": visit, "service_type": SERVICE, **kw}
		with as_user(self.u(actor)):
			return frappe.get_doc(values).insert()

	def test_transition_table_matches_docstring(self):
		self.assertEqual(set(services.TRANSITIONS), {"record", "void"})
		rec, void = services.TRANSITIONS["record"], services.TRANSITIONS["void"]
		self.assertEqual(
			(set(rec.own_roles), set(rec.any_roles), rec.to_status),
			(set(SERVICE_RECORD_ROLES), set(), "Active"),
		)
		self.assertEqual(
			(void.from_statuses, void.to_status, set(void.own_roles), set(void.any_roles), void.needs_reason),
			(("Active",), "Voided", set(SERVICE_RECORD_ROLES), set(SERVICE_ANY_VOID_ROLES), True),
		)

	def visit_states(self) -> dict[str, tuple[str, str | None]]:
		out = {status: (self.visit_in(status), None) for status in ALL_VISIT_STATUSES}
		for reason in (*EXCLUDED_CLOSURES, "Died", "Left before being seen"):
			out[f"Discharged ({reason})"] = (self.visit_in(D, closure_reason=reason), reason)
		return out

	def test_record_matrix(self):
		"""record x role x visit status (open, closed, and closed as not-an-attendance); the form's flags
		(`get_visit_capture`, `get_visit_services`) for the roles that pass the role check."""
		n = 0
		for label, (visit, closure) in self.visit_states().items():
			for actor in SERVICE_ACTORS:
				role = self.role(actor)
				with self.subTest(visit=label, actor=actor), savepoint("s2_svc"):
					n += 1
					doc, err = _try(self.record, actor, visit)
					if role not in (*SERVICE_RECORD_ROLES, ADMIN):
						self.assertTrue(_err_ok(err, frappe.PermissionError), repr(err))
						continue
					if closure in EXCLUDED_CLOSURES:
						self.assertTrue(_err_ok(err, services.ServiceDeliveredError), repr(err))
						continue
					self.assertIsNone(err, repr(err))
					row = frappe.db.get_value(VISIT_DOCTYPE, visit, ["patient", "facility"], as_dict=True)
					self.assertEqual(
						(doc.status, doc.provided_by, doc.patient, doc.facility),
						("Active", self.u(actor), row.patient, row.facility),
					)
		print(f"\n[S2] service record matrix: {n} combinations")

	# DEFECT-P3-4: Record Service offered on Registered-in-error / Superseded visits
	def test_record_offered_vs_works_on_excluded_visits(self):
		"""DEFECT-P3-4: on a visit closed as "Registered in error" / "Superseded by a new visit" the visit
		form still offers Record Service (`get_visit_capture.can_record_services`,
		`get_visit_services.can_record`), but the insert is refused (ServiceDeliveredError)."""
		for reason in EXCLUDED_CLOSURES:
			visit = self.visit_in(D, closure_reason=reason)
			with as_user(self.users["clinician"]):
				offered = (
					outcome.get_visit_capture(visit)["can_record_services"],
					services.get_visit_services(visit)["can_record"],
				)
			with savepoint("s2_svc"):
				_doc, err = _try(self.record, "clinician", visit)
			self.assertEqual(
				offered, (err is None, err is None), f"{reason}: offered {offered}, insert {err!r}"
			)

	def test_record_offered_vs_works_on_attendances(self):
		"""Open and normally closed visits: the Services flags match the insert, per role."""
		for status in (R, IC, D):
			visit = self.visit_in(status)
			for actor in ("reception", "triage", "clinician", "mho", "mic", "his", ADMIN):
				with self.subTest(status=status, actor=actor):
					with savepoint("s2_svc"):
						_doc, err = _try(self.record, actor, visit)
					with as_user(self.u(actor)):
						flags, ferr = _try(outcome.get_visit_capture, visit)
					if ferr is None:
						self.assertEqual(flags["can_record_services"], err is None)

	def test_void_matrix(self):
		"""void x role x (recorder / other clinician) x reason x record status x visit closed later."""
		v1, v2 = self.visit_in(IC), self.visit_in(RD)
		recs = {
			"by clinician": self.record("clinician", v1).name,
			"by mho": self.record("mho", v2).name,
			"by clinician, voided": self.record("clinician", v1).name,
		}
		with as_user(self.users["clinician"]):
			services.void_service(recs["by clinician, voided"], "T- s2")
		v3 = self.visit_in(R)
		recs["by clinician, visit later Registered in error"] = self.record("clinician", v3).name
		engine.transition(v3, "close_visit", system=True, closure_reason="Registered in error", comment="T-")
		recorder = {k: ("mho" if "mho" in k else "clinician") for k in recs}
		n = 0
		for label, name in recs.items():
			for actor in ("clinician", "clinician2", "mho", "mic", "his", "reception", "outreach", ADMIN):
				role = self.role(actor)
				for reason in ("T- s2 wrong patient", None, "  "):
					with self.subTest(record=label, actor=actor, reason=reason), savepoint("s2_void"):
						n += 1
						before = frappe.db.get_value(SERVICE_DELIVERED_DOCTYPE, name, "status")
						_out, err = self.act_as(actor, services.void_service, name, reason)
						allowed = role in (ADMIN, *SERVICE_ANY_VOID_ROLES) or (
							role in SERVICE_RECORD_ROLES and actor == recorder[label]
						)
						if not allowed:
							exp = frappe.PermissionError
						elif before != "Active" or not (reason or "").strip():
							exp = services.IllegalServiceTransitionError
						else:
							exp = None
						row = frappe.db.get_value(
							SERVICE_DELIVERED_DOCTYPE,
							name,
							["status", "void_reason", "voided_by"],
							as_dict=True,
						)
						if exp:
							self.assertTrue(_err_ok(err, exp), f"expected {exp.__name__}, got {err!r}")
							self.assertEqual(row.status, before)
							continue
						self.assertIsNone(err, repr(err))
						self.assertEqual(
							(row.status, row.void_reason, row.voided_by), ("Voided", reason, self.u(actor))
						)
		print(f"\n[S2] service void matrix: {n} combinations")

	def test_voided_record_frozen(self):
		name = self.record("clinician", self.visit_in(IC)).name
		with as_user(self.users["clinician"]):
			services.void_service(name, "T- s2")
		for actor in ("clinician", "mic", ADMIN):
			for field, value in (
				("notes", "T- s2"),
				("service_date", add_days(today(), -1)),
				("status", "Active"),
				("void_reason", "T- other"),
			):
				with self.subTest(actor=actor, field=field), savepoint("s2_voided"):
					doc = frappe.get_doc(SERVICE_DELIVERED_DOCTYPE, name)
					before = cstr(doc.get(field))
					doc.set(field, value)
					_out, err = self.act_as(actor, doc.save)
					self.assertTrue(
						_err_ok(err, (ServiceDeliveredLockedError, frappe.PermissionError)), repr(err)
					)
					self.assertEqual(
						cstr(frappe.db.get_value(SERVICE_DELIVERED_DOCTYPE, name, field)), before
					)


# =====================================================================================================
# 5. Community Activity Record
# =====================================================================================================

CAR_AUTHOR_ROLES = (ROLE_CLINICIAN, ROLE_MHO, ROLE_OUTREACH)
CAR_ACTORS = ("clinician", "clinician2", "mho", "outreach", "mic", "his", "reception", ADMIN)


def car_may(action: str, role: str, is_owner: bool) -> bool:
	"""activities.py docstring table (authors own; Medical In-Charge any; cancel MIC only)."""
	if role == ADMIN:
		return True
	if action == "cancel":
		return role == ROLE_MIC
	if action == "create":
		return role in (*CAR_AUTHOR_ROLES, ROLE_MIC)
	return role == ROLE_MIC or (role in CAR_AUTHOR_ROLES and is_owner)


class TestCommunityActivity(P3Case):
	def make(self, actor="clinician", submit=False, **kw):
		values = {
			"doctype": COMMUNITY_ACTIVITY_DOCTYPE,
			"activity_type": ACTIVITY,
			"activity_date": today(),
			"facility": get_facility(0),
			"location": "T-Village 3",
			"participants_male": 2,
			"participants_female": 3,
			"participants_total": 5,
			**kw,
		}
		with as_user(self.u(actor)):
			doc = frappe.get_doc(values).insert()
			if submit:
				doc.submit()
		return doc.name

	def cancelled(self, actor="clinician") -> str:
		name = self.make(actor, submit=True)
		with as_user(self.users["mic"]):
			activities.cancel_activity(name, "T- s2")
		return name

	def amend(self, original: str):
		new = frappe.copy_doc(frappe.get_doc(COMMUNITY_ACTIVITY_DOCTYPE, original))
		new.amended_from = original
		new.docstatus = 0
		new.insert()
		return new

	def test_transition_table_matches_docstring(self):
		t = activities.TRANSITIONS
		self.assertEqual(set(t), {"save", "submit", "cancel", "amend"})
		self.assertEqual((t["save"].from_docstatus, t["save"].to_docstatus), ((0,), 0))
		self.assertEqual((t["submit"].from_docstatus, t["submit"].to_docstatus), ((0,), 1))
		self.assertEqual(
			(t["cancel"].from_docstatus, t["cancel"].to_docstatus, t["cancel"].needs_reason), ((1,), 2, True)
		)
		self.assertEqual((t["amend"].from_docstatus, t["amend"].to_docstatus), ((2,), 0))
		for action in ("save", "submit", "amend"):
			self.assertEqual(
				(set(t[action].own_roles), set(t[action].any_roles)), (set(CAR_AUTHOR_ROLES), {ROLE_MIC})
			)
		self.assertEqual((set(t["cancel"].own_roles), set(t["cancel"].any_roles)), (set(), {ROLE_MIC}))

	def test_lifecycle_matrix(self):
		"""create / save (Draft edit) / submit / cancel_activity (with and without a reason) / doc.cancel()
		without a reason / amend (owner) x role, on a Clinical Officer's and an Outreach member's records."""
		n = 0
		for owner in ("clinician", "outreach"):
			draft = self.make(owner)
			submitted = self.make(owner, submit=True)
			cancelled = self.cancelled(owner)
			for actor in (*CAR_ACTORS, "outreach2"):
				role = self.role(actor)
				is_owner = actor == owner
				cases = [
					("create", None),
					("save", draft),
					("submit", draft),
					("cancel_activity", submitted),
					("cancel_activity without reason", submitted),
					("doc.cancel without reason", submitted),
				]
				if is_owner or role in (ROLE_MIC, ADMIN, ROLE_HIS, ROLE_RECEPTIONIST):
					cases.append(("amend", cancelled))  # non-owner authors: DEFECT-P3-5
				for case, name in cases:
					with self.subTest(owner=owner, actor=actor, case=case), savepoint("s2_car"):
						n += 1
						if case == "create":
							_out, err = _try(self.make, actor)
							ok = car_may("create", role, True)
							exp_err = frappe.PermissionError
						elif case == "save":
							doc = frappe.get_doc(COMMUNITY_ACTIVITY_DOCTYPE, name)
							doc.topic = "T- s2 edit"
							_out, err = self.act_as(actor, doc.save)
							ok, exp_err = car_may("save", role, is_owner), frappe.PermissionError
						elif case == "submit":
							doc = frappe.get_doc(COMMUNITY_ACTIVITY_DOCTYPE, name)
							_out, err = self.act_as(actor, doc.submit)
							ok, exp_err = car_may("submit", role, is_owner), frappe.PermissionError
						elif case == "cancel_activity":
							_out, err = self.act_as(
								actor, activities.cancel_activity, name, "T- s2 duplicate"
							)
							ok, exp_err = car_may("cancel", role, is_owner), frappe.PermissionError
						elif case == "cancel_activity without reason":
							_out, err = self.act_as(actor, activities.cancel_activity, name, " ")
							ok, exp_err = False, (activities.CommunityActivityError, frappe.PermissionError)
						elif case == "doc.cancel without reason":
							doc = frappe.get_doc(COMMUNITY_ACTIVITY_DOCTYPE, name)
							_out, err = self.act_as(actor, doc.cancel)
							ok = False
							exp_err = (
								activities.CommunityActivityError
								if car_may("cancel", role, is_owner)
								else frappe.PermissionError
							)
						else:  # amend
							out, err = self.act_as(actor, self.amend, name)
							ok, exp_err = car_may("amend", role, is_owner), frappe.PermissionError
						after_ds = (
							frappe.db.get_value(COMMUNITY_ACTIVITY_DOCTYPE, name, "docstatus")
							if name
							else None
						)
						if not ok:
							self.assertTrue(_err_ok(err, exp_err), f"expected {exp_err}, got {err!r}")
							continue
						self.assertIsNone(err, repr(err))
						if case == "save":
							self.assertEqual(
								frappe.db.get_value(COMMUNITY_ACTIVITY_DOCTYPE, name, "topic"), "T- s2 edit"
							)
						elif case == "submit":
							self.assertEqual(after_ds, 1)
						elif case == "cancel_activity":
							row = frappe.db.get_value(
								COMMUNITY_ACTIVITY_DOCTYPE,
								name,
								["docstatus", "cancel_reason", "cancelled_by"],
								as_dict=True,
							)
							self.assertEqual(
								(row.docstatus, row.cancel_reason, row.cancelled_by),
								(2, "T- s2 duplicate", self.u(actor)),
							)
						elif case == "amend":
							self.assertEqual(
								(out.docstatus, out.amended_from, out.cancel_reason, out.cancelled_by),
								(0, name, None, None),
							)
			# Cancelled and submitted states refuse the wrong-state actions.
			with self.subTest(owner=owner, case="wrong state"):
				for name, fn in (
					(draft, lambda n: activities.cancel_activity(n, "T-")),
					(cancelled, lambda n: activities.cancel_activity(n, "T-")),
				):
					with savepoint("s2_car"):
						_out, err = self.act_as("mic", fn, name)
						self.assertTrue(_err_ok(err, activities.CommunityActivityError), repr(err))
				with savepoint("s2_car"):
					_out, err = self.act_as("mic", self.amend, submitted)
					self.assertTrue(_err_ok(err, frappe.ValidationError), repr(err))
		print(f"\n[S2] community activity matrix: {n} combinations")

	# DEFECT-P3-5: a non-owner author amends another author's cancelled activity
	def test_amend_by_non_owner_author(self):
		"""DEFECT-P3-5: another Clinical Officer / MHO / Outreach member can amend a cancelled record they
		do not own (the docstring: the author or the Medical In-Charge). Frappe checks only `create` on
		the amendment, whose owner is the new user, so `if_owner` never applies."""
		name = self.cancelled("clinician")
		for actor in ("clinician2", "mho", "outreach"):
			with savepoint("s2_amend"):
				_out, err = self.act_as(actor, self.amend, name)
			self.assertTrue(_err_ok(err, frappe.PermissionError), f"{actor} amended: {err!r}")

	# DEFECT-P3-6: cancel audit fields writable after submit (allow_on_submit)
	def test_cancel_audit_fields(self):
		"""DEFECT-P3-6a: the cancel fields are `allow_on_submit`, so the author (submit permission, if_owner)
		writes cancel_reason / cancelled_by / cancelled_at on a Submitted, not cancelled record."""
		name = self.make("clinician", submit=True)
		with savepoint("s2_audit"):
			doc = frappe.get_doc(COMMUNITY_ACTIVITY_DOCTYPE, name)
			doc.cancelled_by = self.users["mic"]
			doc.cancel_reason = "T- forged"
			self.act_as("clinician", doc.save)
			row = frappe.db.get_value(
				COMMUNITY_ACTIVITY_DOCTYPE, name, ["cancel_reason", "cancelled_by"], as_dict=True
			)
			self.assertEqual(
				(row.cancel_reason, row.cancelled_by), (None, None), "(a) forged on a Submitted record"
			)

	# DEFECT-P3-6: desk doc.cancel() leaves cancelled_by / cancelled_at empty
	def test_desk_cancel_records_who_and_when(self):
		"""DEFECT-P3-6b: a desk `doc.cancel()` by the Medical In-Charge with the (allow_on_submit) reason
		pre-filled passes `before_cancel` and cancels without cancelled_by / cancelled_at."""
		name = self.make("clinician", submit=True)
		with savepoint("s2_audit"):
			doc = frappe.get_doc(COMMUNITY_ACTIVITY_DOCTYPE, name)
			doc.cancel_reason = "T- desk"
			_out, err = self.act_as("mic", doc.cancel)
			row = frappe.db.get_value(
				COMMUNITY_ACTIVITY_DOCTYPE, name, ["docstatus", "cancelled_by", "cancelled_at"], as_dict=True
			)
			self.assertIsNone(err, repr(err))
			self.assertEqual(row.docstatus, 2)
			self.assertEqual(row.cancelled_by, self.users["mic"], "cancelled without cancelled_by")
			self.assertTrue(row.cancelled_at)

	def test_submitted_record_frozen(self):
		"""Counted fields of a Submitted record cannot change (update after submit refused)."""
		name = self.make("clinician", submit=True)
		for actor in ("clinician", "mic"):
			for field, value in (
				("participants_total", 50),
				("activity_date", add_days(today(), -3)),
				("facility", get_facility(1)),
			):
				with self.subTest(actor=actor, field=field), savepoint("s2_frozen_car"):
					before = cstr(frappe.db.get_value(COMMUNITY_ACTIVITY_DOCTYPE, name, field))
					doc = frappe.get_doc(COMMUNITY_ACTIVITY_DOCTYPE, name)
					doc.set(field, value)
					_out, err = self.act_as(actor, doc.save)
					self.assertIsNotNone(err)
					self.assertEqual(
						cstr(frappe.db.get_value(COMMUNITY_ACTIVITY_DOCTYPE, name, field)), before
					)


# =====================================================================================================
# 6. Exact / estimated date of birth
# =====================================================================================================

REF_ON = date(2025, 6, 30)


def expect_dob(before: dict | None, after: dict, now: date):
	"""Oracle from the registration/dob.py docstring table. Returns an exception class or the expected
	(dob, flag, years, months, estimated_on) after the save. `after` holds the values being saved."""
	y, m = cint(after.get(YEARS)), cint(after.get(MONTHS))
	if not (0 <= y <= 120) or not (0 <= m <= 11):
		return dob.EstimatedAgeError
	d = getdate(after["dob"]) if after.get("dob") else None
	age = bool(y or m)
	if before is not None:
		bd = getdate(before["dob"]) if before.get("dob") else None
		dob_changed = d != bd
		age_changed = (y, m) != (cint(before.get(YEARS)), cint(before.get(MONTHS)))
	if not age:
		mode = "exact" if d else "missing"
	elif not d:
		mode = "estimated"
	elif cint(after.get(FLAG)):
		mode = "exact" if before is not None and dob_changed and not age_changed else "estimated"
	elif before is None:
		return dob.EstimatedAgeError  # both on a new record
	elif age_changed and not dob_changed:
		mode = "estimated"
	elif dob_changed and not age_changed:
		mode = "exact"
	elif not dob_changed and not age_changed:
		mode = "estimated" if cint(before.get(FLAG)) else "exact"
	else:
		return dob.EstimatedAgeError
	if mode == "estimated":
		if before is not None and cint(before.get(FLAG)) and before.get(ON):
			on = getdate(before[ON])
		elif before is None and after.get(ON) and getdate(after[ON]) <= now:
			on = getdate(after[ON])
		else:
			on = now
		return (on - relativedelta(years=y, months=m), 1, y, m, on)
	if mode == "missing":
		changed = before is None or (
			d != (getdate(before["dob"]) if before.get("dob") else None)
			or (y, m) != (cint(before.get(YEARS)), cint(before.get(MONTHS)))
			or cint(after.get(FLAG)) != cint(before.get(FLAG))
		)
		if changed:
			return dob.DateOfBirthRequiredError
		return (None, 0, 0, 0, None)
	return (d, 0, 0, 0, None)


DOB_FIELDS = ("dob", FLAG, YEARS, MONTHS, ON)


class TestDobEstimate(P3Case):
	def state(self, name: str) -> dict:
		row = frappe.db.get_value("Patient", name, list(DOB_FIELDS), as_dict=True)
		return {k: row.get(k) for k in DOB_FIELDS}

	def as_tuple(self, s: dict) -> tuple:
		return (
			getdate(s["dob"]) if s.get("dob") else None,
			cint(s.get(FLAG)),
			cint(s.get(YEARS)),
			cint(s.get(MONTHS)),
			getdate(s[ON]) if s.get(ON) else None,
		)

	def test_insert_matrix(self):
		now = getdate(today())
		cases = {
			"exact": {"dob": "1990-01-01"},
			"age": {"dob": None, YEARS: 30},
			"infant months": {"dob": None, MONTHS: 5},
			"age, back-entered date": {"dob": None, YEARS: 1, MONTHS: 4, ON: "2025-06-30"},
			"age, future date": {"dob": None, YEARS: 1, ON: str(add_days(today(), 30))},
			"age + dob, no flag": {"dob": "1990-01-01", YEARS: 30},
			"age + dob + flag": {"dob": "1990-01-01", YEARS: 30, FLAG: 1},
			"flag + dob, no age": {"dob": "1990-01-01", FLAG: 1},
			"nothing": {"dob": None},
			"flag only": {"dob": None, FLAG: 1},
			"age 121": {"dob": None, YEARS: 121},
			"months 12": {"dob": None, MONTHS: 12},
			"negative": {"dob": None, YEARS: -1},
		}
		for label, values in cases.items():
			exp = expect_dob(None, values, now)
			with self.subTest(case=label), savepoint("s2_dob"):
				p, err = _try(make_patient, **values)
				if isinstance(exp, type):
					self.assertTrue(_err_ok(err, exp), f"expected {exp.__name__}, got {err!r}")
					continue
				self.assertIsNone(err, repr(err))
				self.assertEqual(self.as_tuple(self.state(p.name)), exp)

	def start(self, kind: str) -> str:
		if kind == "exact":
			return make_patient(dob="1990-01-01").name
		if kind == "estimated":
			return make_patient(dob=None, **{YEARS: 30, ON: str(REF_ON)}).name
		p = make_patient(dob="1990-01-01")  # legacy: registered before the rule, no DOB
		frappe.db.set_value("Patient", p.name, "dob", None, update_modified=False)
		return p.name

	EDITS: ClassVar[dict[str, dict]] = {
		"unrelated save": {},
		"age typed (form ticks flag)": {YEARS: 20, FLAG: 1},
		"age typed, no flag": {YEARS: 20},
		"months typed (form ticks flag)": {MONTHS: 6, FLAG: 1},
		"dob changed": {"dob": "1991-02-03"},
		"dob changed, flag unticked": {"dob": "1991-02-03", FLAG: 0},
		"age changed": {YEARS: 31},
		"dob cleared": {"dob": None},
		"dob + age + flag cleared": {"dob": None, YEARS: 0, MONTHS: 0, FLAG: 0},
		"flag unticked only": {FLAG: 0},
		"flag ticked only": {FLAG: 1},
		"dob and age changed, flag ticked": {"dob": "1991-02-03", YEARS: 25, FLAG: 1},
		"dob and age changed, no flag": {"dob": "1991-02-03", YEARS: 25, FLAG: 0},
		"age out of range": {YEARS: 130, FLAG: 1},
	}
	# Estimated record whose age is cleared while its DOB stays: DEFECT-P3-7 (tested on its own).
	DEFECT_EDITS: ClassVar[dict[str, dict]] = {
		"age cleared, flag kept": {YEARS: 0, MONTHS: 0},
		"age cleared, flag unticked": {YEARS: 0, MONTHS: 0, FLAG: 0},
	}

	def test_update_matrix(self):
		"""Exact / estimated / legacy-missing records x every DOB edit: the docstring table."""
		now = getdate(today())
		n = 0
		for kind in ("exact", "estimated", "legacy"):
			name = self.start(kind)
			edits = dict(self.EDITS)
			if kind != "estimated":
				edits.update(self.DEFECT_EDITS)
			if kind == "legacy":
				edits.pop("flag ticked only")  # DEFECT-P3-8
			for label, edit in edits.items():
				with self.subTest(start=kind, edit=label), savepoint("s2_dob"):
					n += 1
					before = self.state(name)
					after_values = {**before, **edit}
					exp = expect_dob(before, after_values, now)
					doc = frappe.get_doc("Patient", name)
					for k, v in edit.items():
						doc.set(k, v)
					_out, err = _try(doc.save, ignore_permissions=True)
					if isinstance(exp, type):
						self.assertTrue(_err_ok(err, exp), f"expected {exp.__name__}, got {err!r}")
						self.assertEqual(self.state(name), before)
						continue
					self.assertIsNone(err, repr(err))
					self.assertEqual(self.as_tuple(self.state(name)), exp)
		print(f"\n[S2] dob update matrix: {n} combinations")

	# DEFECT-P3-7: clearing the estimated age turns the estimate into an exact DOB
	def test_clearing_estimated_age_does_not_make_the_dob_exact(self):
		"""DEFECT-P3-7: on an estimated record, clearing the age (years and months 0) while leaving the
		computed DOB turns the estimate into an "exact" DOB nobody entered (flag and estimated-on cleared).
		The form clears the DOB in that case ("Age removed: ... the date of birth must be entered"), the
		server should refuse it too (DateOfBirthRequiredError), as it keeps the estimate when only the flag
		is unticked."""
		name = self.start("estimated")
		for label, edit in self.DEFECT_EDITS.items():
			with savepoint("s2_dob"):
				before = self.state(name)
				doc = frappe.get_doc("Patient", name)
				for k, v in edit.items():
					doc.set(k, v)
				_out, err = _try(doc.save, ignore_permissions=True)
				after = self.state(name)
				self.assertTrue(err is not None or cint(after[FLAG]) == 1, f"{label}: {before} -> {after}")

	# DEFECT-P3-8: DOB-estimated tick alone on a patient without DOB is silently dropped
	def test_flag_alone_on_legacy_patient_needs_dob(self):
		"""DEFECT-P3-8: the docstring requires a DOB when the DOB fields are edited, "age or flag edited"
		included. Ticking "DOB Estimated" alone on a patient without a DOB saves silently: apply_dob_rules
		clears the flag *before* `_dob_fields_changed` compares it with the saved record."""
		name = self.start("legacy")
		doc = frappe.get_doc("Patient", name)
		doc.set(FLAG, 1)
		_out, err = _try(doc.save, ignore_permissions=True)
		self.assertTrue(_err_ok(err, dob.DateOfBirthRequiredError), repr(err))


# =====================================================================================================
# 7. Offered vs working (the run buttons, close reasons and outcome choices are checked in the matrices)
# =====================================================================================================


class TestOfferedVsWorking(P3Case):
	def test_visit_onload_closure_reasons(self):
		"""The visit form's Close Visit dialog (onload `closure_reasons`) == reasons close_visit accepts."""
		visit = self.visit_in(IT)
		for actor in (*CLOSE_ACTORS, "his", "lab"):
			with as_user(self.u(actor)):
				doc = frappe.get_doc(VISIT_DOCTYPE, visit)
				doc.run_method("onload")
				offered = set(doc.get_onload().get("closure_reasons") or [])
				can_close = "close_visit" in {
					a["action"] for a in doc.get_onload().get("available_actions") or []
				}
			works = set()
			for reason in VISIT_CLOSURE_REASONS:
				with savepoint("s2_onload"):
					_out, err = self.act_as(
						actor, engine.apply_action, visit, "close_visit", comment="T-", closure_reason=reason
					)
				if err is None:
					works.add(reason)
				elif not isinstance(err, (frappe.PermissionError, IllegalVisitTransitionError)):
					raise err
			with self.subTest(actor=actor):
				self.assertEqual(can_close, bool(works))
				if can_close:
					self.assertEqual(offered, works)
