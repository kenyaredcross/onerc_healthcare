# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient Visit lifecycle, queue, triage and permissions (FR-6 to FR-9, BR-2, BR-3, NFR-10; SDD §4.4, §5.2)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import add_days, now_datetime, today

from onerc_healthcare.constants import (
	DEPARTMENTS,
	DEPT_MENTAL_HEALTH,
	DEPT_OUTPATIENT,
	DEPT_RECEPTION,
	DEPT_TRIAGE,
	VISIT_CLOSURE_REASONS,
	VISIT_DOCTYPE,
	VISIT_OPEN_STATUSES,
	VISIT_STATUS_AWAITING_LAB,
	VISIT_STATUS_AWAITING_PHARMACY,
	VISIT_STATUS_DISCHARGED,
	VISIT_STATUS_IN_CONSULTATION,
	VISIT_STATUS_IN_TRIAGE,
	VISIT_STATUS_REFERRED,
	VISIT_STATUS_REGISTERED,
	VISIT_STATUS_ROUTED,
)
from onerc_healthcare.emr_visit.doctype.patient_visit.patient_visit import (
	OpenVisitExistsError,
	VisitStatusLockedError,
)
from onerc_healthcare.security.matrix import BILLING_DOCTYPES
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import get_facility, make_patient
from onerc_healthcare.tests.visit_factories import (
	get_triage_level,
	make_submitted_encounter,
	make_visit,
	make_visit_users,
	make_vital_signs,
)
from onerc_healthcare.visit import engine, tasks, workspaces
from onerc_healthcare.visit.engine import (
	IllegalVisitTransitionError,
	PharmacyTaskOpenError,
	TriageVitalsMissingError,
	VisitGuardError,
	apply_action,
	transition,
)
from onerc_healthcare.visit.events import ImplausibleVitalSignsError, TriageIncompleteError
from onerc_healthcare.visit.patients import MERGED_INTO_FIELD, InactivePatientError
from onerc_healthcare.visit.queue import get_open_visit, get_queue, longest_wait_card, waiting_minutes
from onerc_healthcare.visit.setup import VISIT_INDEXES

FUTURE = "2099-03-01"
CLOSED_CALLS: list[str] = []
COMPLETE_TASKS: set[str] = set()


def _record_closed(visit_doc) -> None:
	"""Test VISIT_CLOSED_HANDLERS entry."""
	CLOSED_CALLS.append(visit_doc.name)


def _fake_checker(visit: str) -> bool:
	"""Test TASK_CHECKERS entry: complete when the visit is in COMPLETE_TASKS."""
	return visit in COMPLETE_TASKS


class VisitTestCase(EMRTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users = make_visit_users()

	@contextmanager
	def as_user(self, key: str):
		frappe.set_user(self.users[key])
		try:
			yield
		finally:
			frappe.set_user("Administrator")

	def act(self, visit, action: str, user: str | None = None, **kw):
		"""Run `action` as `user` (a key of self.users) or as the system."""
		if user is None:
			return transition(visit.name if hasattr(visit, "name") else visit, action, system=True, **kw)
		with self.as_user(user):
			return transition(visit.name if hasattr(visit, "name") else visit, action, **kw)

	def submit_vitals(self, visit):
		"""Submit triage vital signs linked to `visit` (needed before routing, FR-9)."""
		name = visit.name if hasattr(visit, "name") else visit
		patient = frappe.db.get_value(VISIT_DOCTYPE, name, "patient")
		return make_vital_signs(patient, visit=name, submit=True)

	def visit_in(self, status: str, department: str = DEPT_OUTPATIENT):
		"""A visit advanced to `status` along the normal path (with submitted triage vitals)."""
		visit = make_visit()
		path = {
			VISIT_STATUS_REGISTERED: [],
			VISIT_STATUS_IN_TRIAGE: [("start_triage", "triage")],
			VISIT_STATUS_ROUTED: [("start_triage", "triage"), ("route", "triage")],
			VISIT_STATUS_IN_CONSULTATION: [
				("start_triage", "triage"),
				("route", "triage"),
				("start_consultation", "clinician"),
			],
		}[status]
		for action, user in path:
			if action == "route":
				self.submit_vitals(visit)
			visit = self.act(visit, action, user, department=department if action == "route" else None)
		self.assertEqual(visit.status, status)
		return visit


class TestVisitRegistration(VisitTestCase):
	def test_registration_defaults(self):
		"""FR-6/FR-7: a new visit is Registered at Reception with a queue number and a first log row."""
		visit = make_visit()
		self.assertEqual(visit.status, VISIT_STATUS_REGISTERED)
		self.assertEqual(visit.current_department, DEPT_RECEPTION)
		self.assertTrue(visit.queue_number >= 1)
		self.assertEqual(str(visit.visit_date), today())
		self.assertTrue(visit.arrived_at)
		self.assertTrue(visit.company)
		self.assertRegex(visit.name, r"^[A-Z0-9]{2,8}-PV-\d{4}-\d{5}$")
		self.assertEqual(len(visit.status_log), 1)
		self.assertEqual(visit.status_log[0].to_status, VISIT_STATUS_REGISTERED)
		self.assertEqual(visit.status_log[0].user, "Administrator")

	def test_receptionist_creates_visit(self):
		"""Permission matrix: the Nurse Receptionist creates visits; other roles cannot."""
		patient = make_patient().name
		with self.as_user("reception"):
			visit = make_visit(patient=patient, ignore_permissions=False)
		self.assertEqual(visit.owner, self.users["reception"])
		other = make_patient().name
		for key in ("triage", "clinician", "lab", "hoo", "sysadmin"):
			with self.as_user(key):
				self.assertFalse(frappe.has_permission(VISIT_DOCTYPE, "create"), key)
				with self.assertRaises(frappe.PermissionError, msg=key):
					make_visit(patient=other, ignore_permissions=False)

	def test_facility_must_be_a_facility_group(self):
		root = frappe.db.get_value(
			"Healthcare Service Unit", {"is_group": 1, "parent_healthcare_service_unit": ["is", "not set"]}
		)
		with self.assertRaises(frappe.ValidationError):
			make_visit(facility=root)

	def test_one_open_visit_per_patient_per_day(self):
		visit = make_visit()
		with self.assertRaises(OpenVisitExistsError):
			make_visit(patient=visit.patient)

	def test_one_open_visit_any_date(self):
		"""Decision 2026-11-13: no other open visit at any date; Close Visit frees the patient."""
		old = make_visit(system=True, visit_date=add_days(today(), -5))
		with self.assertRaises(OpenVisitExistsError) as ctx:
			make_visit(patient=old.patient)
		self.assertIn(old.name, str(ctx.exception))
		self.assertIn("Close Visit", str(ctx.exception))
		with self.as_user("reception"):
			found = get_open_visit(old.patient)
		self.assertEqual(found.name, old.name)
		self.assertTrue(found.can_close)
		self.assertEqual(found.default_closure_reason, "Superseded by a new visit")
		self.act(old, "close_visit", "reception", closure_reason="Superseded by a new visit", comment="Stale")
		self.assertIsNone(get_open_visit(old.patient))
		self.assertEqual(make_visit(patient=old.patient).status, VISIT_STATUS_REGISTERED)

	def test_disabled_patient_refused(self):
		"""L1: no visit or triage vital signs for a disabled (or merged) patient."""
		patient = make_patient().name
		frappe.db.set_value("Patient", patient, "status", "Disabled")
		with self.assertRaises(InactivePatientError):
			make_visit(patient=patient)
		with self.assertRaises(InactivePatientError):
			make_vital_signs(patient)

	def test_merged_patient_refused(self):
		if not frappe.get_meta("Patient").has_field(MERGED_INTO_FIELD):
			self.skipTest("Patient has no merge field yet")
		target = make_patient().name
		source = make_patient().name
		frappe.db.set_value("Patient", source, MERGED_INTO_FIELD, target)
		with self.assertRaises(InactivePatientError) as ctx:
			make_visit(patient=source)
		self.assertIn("merged", str(ctx.exception).lower())

	def test_patient_identifier_copied(self):
		"""FR-6: the queue shows the patient's identifier (A1 table when present)."""
		visit = make_visit()
		if frappe.get_meta("Patient").has_field("krcs_identifiers"):
			self.assertTrue(visit.patient_identifier)


class TestVisitTransitions(VisitTestCase):
	def test_full_path_to_discharge_via_dispensing(self):
		"""SDD §4.4: Registered -> In Triage -> Routed -> In Consultation -> Awaiting Pharmacy -> Discharged."""
		visit = make_visit()
		visit = self.act(visit, "start_triage", "triage")
		self.assertEqual(visit.status, VISIT_STATUS_IN_TRIAGE)
		self.assertEqual(visit.current_department, DEPT_TRIAGE)
		self.assertTrue(visit.triage_started_at)

		self.submit_vitals(visit)
		visit = self.act(visit, "route", "triage", department=DEPT_OUTPATIENT)
		self.assertEqual(visit.status, VISIT_STATUS_ROUTED)
		self.assertEqual(visit.current_department, DEPT_OUTPATIENT)
		self.assertTrue(visit.routed_at)

		visit = self.act(visit, "start_consultation", "clinician")
		self.assertEqual(visit.status, VISIT_STATUS_IN_CONSULTATION)
		self.assertTrue(visit.consultation_started_at)

		make_submitted_encounter(visit.patient, visit.name)
		visit = self.act(visit, "prescribe")
		self.assertEqual(visit.status, VISIT_STATUS_AWAITING_PHARMACY)
		self.assertTrue(visit.pharmacy_open)

		visit = self.act(visit, "dispensed")
		self.assertEqual(visit.status, VISIT_STATUS_DISCHARGED)
		self.assertFalse(visit.pharmacy_open)
		self.assertTrue(visit.closed_at)

		log = [(r.from_status, r.to_status, r.action) for r in visit.status_log]
		self.assertEqual(
			log,
			[
				("", VISIT_STATUS_REGISTERED, "register"),
				(VISIT_STATUS_REGISTERED, VISIT_STATUS_IN_TRIAGE, "start_triage"),
				(VISIT_STATUS_IN_TRIAGE, VISIT_STATUS_ROUTED, "route"),
				(VISIT_STATUS_ROUTED, VISIT_STATUS_IN_CONSULTATION, "start_consultation"),
				(VISIT_STATUS_IN_CONSULTATION, VISIT_STATUS_AWAITING_PHARMACY, "prescribe"),
				(VISIT_STATUS_AWAITING_PHARMACY, VISIT_STATUS_DISCHARGED, "dispensed"),
			],
		)
		self.assertEqual(visit.status_log[1].user, self.users["triage"])
		self.assertEqual(visit.status_log[3].user, self.users["clinician"])

	def test_lab_round_trip(self):
		"""SDD §4.4: Awaiting Laboratory returns to In Consultation once results are entered."""
		visit = self.visit_in(VISIT_STATUS_IN_CONSULTATION)
		visit = self.act(visit, "request_lab")
		self.assertEqual(visit.status, VISIT_STATUS_AWAITING_LAB)
		self.assertTrue(visit.lab_open)
		visit = self.act(visit, "lab_results_ready")
		self.assertEqual(visit.status, VISIT_STATUS_IN_CONSULTATION)
		self.assertFalse(visit.lab_open)

	def test_clinician_discharges(self):
		visit = self.visit_in(VISIT_STATUS_IN_CONSULTATION)
		visit = self.act(visit, "discharge", "clinician", comment="Well")
		self.assertEqual(visit.status, VISIT_STATUS_DISCHARGED)
		self.assertTrue(visit.closed_at)
		self.assertEqual(visit.status_log[-1].comment, "Well")

	def test_mental_health_officer_consults_and_refers(self):
		visit = self.visit_in(VISIT_STATUS_ROUTED, department=DEPT_MENTAL_HEALTH)
		visit = self.act(visit, "start_consultation", "mho")
		visit = self.act(visit, "refer", "mho", comment="To referral hospital")
		self.assertEqual(visit.status, VISIT_STATUS_REFERRED)
		self.assertTrue(visit.closed_at)

	def test_wrong_role_denied(self):
		"""Each human action is limited to its SDD §4.4 role; system events are not user actions."""
		cases = {
			VISIT_STATUS_REGISTERED: {
				"start_triage": ("reception", "clinician", "mho", "lab", "pharmacist", "his", "mic", "hoo"),
			},
			VISIT_STATUS_IN_TRIAGE: {"route": ("reception", "clinician", "mho", "lab", "mic", "hoo")},
			VISIT_STATUS_ROUTED: {"start_consultation": ("reception", "triage", "lab", "pharmacist", "mic")},
			VISIT_STATUS_IN_CONSULTATION: {
				"discharge": ("reception", "triage", "lab", "pharmacist", "his", "hoo"),
				"refer": ("reception", "triage", "lab", "pharmacist", "mic"),
				"request_lab": ("clinician", "lab"),
				"prescribe": ("clinician", "pharmacist"),
			},
		}
		for status, actions in cases.items():
			visit = self.visit_in(status)
			for action, users in actions.items():
				for user in users:
					with self.assertRaises(frappe.PermissionError, msg=f"{action} by {user}"):
						self.act(visit, action, user, department=DEPT_OUTPATIENT)
			self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit.name, "status"), status)

	def test_system_events_denied_to_users(self):
		visit = self.visit_in(VISIT_STATUS_IN_CONSULTATION)
		self.act(visit, "request_lab")
		self.act(visit, "prescribe")
		for action, user in (("lab_results_ready", "lab"), ("dispensed", "pharmacist")):
			with self.assertRaises(frappe.PermissionError):
				self.act(visit, action, user)

	def test_sysadmin_has_no_actions(self):
		visit = make_visit()
		with self.assertRaises(frappe.PermissionError):
			self.act(visit, "start_triage", "sysadmin")

	def test_illegal_from_status(self):
		"""Actions outside the SDD §4.4 table are rejected with a ValidationError."""
		illegal = {
			VISIT_STATUS_REGISTERED: [
				("route", "triage"),
				("start_consultation", "clinician"),
				("discharge", "clinician"),
				("refer", "clinician"),
				("request_lab", None),
				("prescribe", None),
			],
			VISIT_STATUS_IN_TRIAGE: [("start_triage", "triage"), ("start_consultation", "clinician")],
			VISIT_STATUS_ROUTED: [("route", "triage"), ("discharge", "clinician"), ("request_lab", None)],
			VISIT_STATUS_IN_CONSULTATION: [
				("start_triage", "triage"),
				("route", "triage"),
				("lab_results_ready", None),
				("dispensed", None),
			],
		}
		for status, actions in illegal.items():
			visit = self.visit_in(status)
			for action, user in actions:
				with self.assertRaises(frappe.ValidationError, msg=f"{action} from {status}"):
					self.act(visit, action, user, department=DEPT_OUTPATIENT)
			self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit.name, "status"), status)

	def test_closed_visit_accepts_no_actions(self):
		visit = self.visit_in(VISIT_STATUS_IN_CONSULTATION)
		self.act(visit, "discharge", "clinician")
		for action, user in (
			("start_consultation", "clinician"),
			("refer", "clinician"),
			("request_lab", None),
		):
			with self.assertRaises(IllegalVisitTransitionError):
				self.act(visit, action, user)

	def test_unknown_action(self):
		visit = make_visit()
		with self.assertRaises(frappe.ValidationError):
			self.act(visit, "teleport")

	def test_route_needs_valid_department(self):
		visit = self.visit_in(VISIT_STATUS_IN_TRIAGE)
		with self.assertRaises(frappe.ValidationError):
			self.act(visit, "route", "triage")
		with self.assertRaises(frappe.ValidationError):
			self.act(visit, "route", "triage", department="T-No Such Department")

	def test_apply_action_endpoint(self):
		"""The whitelisted POST wrapper runs as the session user."""
		visit = make_visit()
		with self.as_user("triage"):
			out = apply_action(visit.name, "start_triage")
			self.assertEqual(out["status"], VISIT_STATUS_IN_TRIAGE)
		self.submit_vitals(visit)
		with self.as_user("triage"):
			out = apply_action(visit.name, "route", department=DEPT_OUTPATIENT, comment="Fever")
			self.assertEqual(out["current_department"], DEPT_OUTPATIENT)
		with self.as_user("clinician"), self.assertRaises(frappe.PermissionError):
			apply_action(visit.name, "prescribe")

	def test_available_actions_follow_role(self):
		visit = make_visit()
		with self.as_user("triage"):
			self.assertEqual(
				sorted(a["action"] for a in engine.get_available_actions(visit)),
				["close_visit", "start_triage"],
			)
		with self.as_user("clinician"):
			self.assertEqual([a["action"] for a in engine.get_available_actions(visit)], ["close_visit"])
		with self.as_user("lab"):
			self.assertEqual(engine.get_available_actions(visit), [])
		visit = self.visit_in(VISIT_STATUS_IN_CONSULTATION)
		with self.as_user("clinician"):
			self.assertEqual(
				sorted(a["action"] for a in engine.get_available_actions(visit)),
				["close_visit", "discharge", "refer"],
			)


class TestRerouteAndReturnToTriage(VisitTestCase):
	"""Intake departments are not routing targets; a routed visit can be re-routed or sent back to triage."""

	def test_intake_departments_not_routable(self):
		for dept in (DEPT_RECEPTION, DEPT_TRIAGE):
			visit = self.visit_in(VISIT_STATUS_IN_TRIAGE)
			with self.assertRaises(IllegalVisitTransitionError, msg=f"route {dept}"):
				self.act(visit, "route", "triage", department=dept)
			self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit.name, "status"), VISIT_STATUS_IN_TRIAGE)
			routed = self.visit_in(VISIT_STATUS_ROUTED)
			with self.assertRaises(IllegalVisitTransitionError, msg=f"reroute {dept}"):
				self.act(routed, "reroute", "triage", department=dept)
			self.assertEqual(
				frappe.db.get_value(VISIT_DOCTYPE, routed.name, "current_department"), DEPT_OUTPATIENT
			)
		self.assertEqual(engine.get_non_routable_departments(), [DEPT_RECEPTION, DEPT_TRIAGE])

	def test_return_to_triage_allowed_roles(self):
		triage_filter = {"status": VISIT_STATUS_IN_TRIAGE}
		for user in ("triage", "clinician", "mho"):
			visit = self.visit_in(VISIT_STATUS_ROUTED)
			visit = self.act(visit, "return_to_triage", user, comment="Vitals need repeating")
			self.assertEqual(visit.status, VISIT_STATUS_IN_TRIAGE, user)
			self.assertEqual(visit.current_department, DEPT_TRIAGE, user)
			row = visit.status_log[-1]
			self.assertEqual(
				(row.from_status, row.to_status, row.action, row.user, row.department, row.comment),
				(
					VISIT_STATUS_ROUTED,
					VISIT_STATUS_IN_TRIAGE,
					"return_to_triage",
					self.users[user],
					DEPT_TRIAGE,
					"Vitals need repeating",
				),
			)
			# Shows again in the Triage "In Triage" quick list, and can be routed again.
			with self.as_user("triage"):
				self.assertIn(visit.name, frappe.get_list(VISIT_DOCTYPE, filters=triage_filter, pluck="name"))
			visit = self.act(visit, "route", "triage", department=DEPT_MENTAL_HEALTH)
			self.assertEqual(visit.status, VISIT_STATUS_ROUTED)
		ql = workspaces.department_block(DEPT_TRIAGE)["quick_lists"]
		self.assertIn(
			[[VISIT_DOCTYPE, "status", "=", VISIT_STATUS_IN_TRIAGE]],
			[json.loads(q["quick_list_filter"]) for q in ql],
		)

	def test_return_to_triage_refused(self):
		visit = self.visit_in(VISIT_STATUS_ROUTED)
		for user in ("reception", "lab", "pharmacist", "his", "mic", "hoo"):
			with self.assertRaises(frappe.PermissionError, msg=user):
				self.act(visit, "return_to_triage", user, comment="x")
		for comment in (None, "  "):
			with self.assertRaises(IllegalVisitTransitionError):
				self.act(visit, "return_to_triage", "clinician", comment=comment)
		consulting = self.visit_in(VISIT_STATUS_IN_CONSULTATION)
		with self.assertRaises(IllegalVisitTransitionError):
			self.act(consulting, "return_to_triage", "clinician", comment="too late")
		self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit.name, "status"), VISIT_STATUS_ROUTED)

	def test_reroute(self):
		visit = self.visit_in(VISIT_STATUS_ROUTED)
		first_routed_at = visit.routed_at
		visit = self.act(visit, "reroute", "triage", department=DEPT_MENTAL_HEALTH)
		self.assertEqual(visit.status, VISIT_STATUS_ROUTED)
		self.assertEqual(visit.current_department, DEPT_MENTAL_HEALTH)
		self.assertGreaterEqual(visit.routed_at, first_routed_at)
		row = visit.status_log[-1]
		self.assertEqual(
			(row.from_status, row.to_status, row.action, row.department, row.user),
			(VISIT_STATUS_ROUTED, VISIT_STATUS_ROUTED, "reroute", DEPT_MENTAL_HEALTH, self.users["triage"]),
		)
		self.assertEqual(len([r for r in visit.status_log if r.action == "reroute"]), 1)

	def test_reroute_refused(self):
		visit = self.visit_in(VISIT_STATUS_ROUTED)
		for user in ("reception", "clinician", "mho", "lab", "mic", "hoo"):
			with self.assertRaises(frappe.PermissionError, msg=user):
				self.act(visit, "reroute", user, department=DEPT_MENTAL_HEALTH)
		with self.assertRaises(IllegalVisitTransitionError):
			self.act(visit, "reroute", "triage", department=DEPT_OUTPATIENT)
		with self.assertRaises(IllegalVisitTransitionError):
			self.act(visit, "reroute", "triage")
		consulting = self.visit_in(VISIT_STATUS_IN_CONSULTATION)
		with self.assertRaises(IllegalVisitTransitionError):
			self.act(consulting, "reroute", "triage", department=DEPT_MENTAL_HEALTH)

	def test_available_actions_on_routed_visit(self):
		visit = self.visit_in(VISIT_STATUS_ROUTED)
		with self.as_user("triage"):
			actions = {a["action"]: a for a in engine.get_available_actions(visit)}
		self.assertEqual(set(actions), {"reroute", "return_to_triage", "close_visit"})
		self.assertTrue(actions["reroute"]["needs_department"])
		self.assertFalse(actions["reroute"]["needs_comment"])
		self.assertTrue(actions["return_to_triage"]["needs_comment"])
		self.assertFalse(actions["return_to_triage"]["needs_department"])
		with self.as_user("clinician"):
			self.assertEqual(
				{a["action"] for a in engine.get_available_actions(visit)},
				{"start_consultation", "return_to_triage", "close_visit"},
			)

	def test_history_stays_append_only(self):
		visit = self.visit_in(VISIT_STATUS_ROUTED)
		self.act(visit, "reroute", "triage", department=DEPT_MENTAL_HEALTH)
		self.act(visit, "return_to_triage", "triage", comment="Recheck")
		with self.as_user("triage"):
			doc = frappe.get_doc(VISIT_DOCTYPE, visit.name)
			doc.status_log[-1].comment = "edited"
			with self.assertRaises(VisitStatusLockedError):
				doc.save()


class TestRouteRequiresVitals(VisitTestCase):
	"""FR-9: routing needs submitted triage vital signs, or an emergency reason from the Triage Nurse."""

	def test_route_without_vitals_or_reason_refused(self):
		visit = self.visit_in(VISIT_STATUS_IN_TRIAGE)
		for reason in (None, "", "   "):
			with self.assertRaises(TriageVitalsMissingError, msg=repr(reason)):
				self.act(visit, "route", "triage", department=DEPT_OUTPATIENT, without_vitals_reason=reason)
		self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit.name, "status"), VISIT_STATUS_IN_TRIAGE)

	def test_route_with_draft_vitals_refused(self):
		visit = self.visit_in(VISIT_STATUS_IN_TRIAGE)
		draft = make_vital_signs(visit.patient, visit=visit.name)
		self.assertEqual(draft.docstatus, 0)
		frappe.db.set_value(
			VISIT_DOCTYPE, visit.name, "triage_vital_signs", draft.name, update_modified=False
		)
		with self.assertRaises(TriageVitalsMissingError):
			self.act(visit, "route", "triage", department=DEPT_OUTPATIENT)

	def test_route_with_vitals_of_another_visit_refused(self):
		other = self.visit_in(VISIT_STATUS_IN_TRIAGE)
		foreign = self.submit_vitals(other)
		visit = self.visit_in(VISIT_STATUS_IN_TRIAGE)
		frappe.db.set_value(
			VISIT_DOCTYPE, visit.name, "triage_vital_signs", foreign.name, update_modified=False
		)
		with self.assertRaises(TriageVitalsMissingError):
			self.act(visit, "route", "triage", department=DEPT_OUTPATIENT)

	def test_route_with_submitted_vitals(self):
		visit = self.visit_in(VISIT_STATUS_IN_TRIAGE)
		vs = self.submit_vitals(visit)
		visit = self.act(visit, "route", "triage", department=DEPT_OUTPATIENT)
		self.assertEqual(visit.status, VISIT_STATUS_ROUTED)
		self.assertEqual(visit.triage_vital_signs, vs.name)
		self.assertFalse(visit.routed_without_vitals)
		self.assertFalse(visit.status_log[-1].comment)

	def test_emergency_override(self):
		visit = self.visit_in(VISIT_STATUS_IN_TRIAGE)
		with self.as_user("triage"):
			apply_action(
				visit.name,
				"route",
				department=DEPT_OUTPATIENT,
				without_vitals_reason="  Too unwell to wait, taken straight to clinician  ",
			)
		visit.reload()
		self.assertEqual(visit.status, VISIT_STATUS_ROUTED)
		self.assertEqual(visit.routed_without_vitals, 1)
		self.assertEqual(
			visit.status_log[-1].comment,
			"Routed without vital signs: Too unwell to wait, taken straight to clinician",
		)
		self.assertIn(
			visit.name, frappe.get_list(VISIT_DOCTYPE, filters={"routed_without_vitals": 1}, pluck="name")
		)

	def test_override_reason_length(self):
		visit = self.visit_in(VISIT_STATUS_IN_TRIAGE)
		with self.assertRaises(TriageVitalsMissingError):
			self.act(visit, "route", "triage", department=DEPT_OUTPATIENT, without_vitals_reason="x" * 1001)

	def test_flag_not_editable_by_hand(self):
		visit = make_visit()
		visit.routed_without_vitals = 1
		with self.assertRaises(VisitStatusLockedError):
			visit.save(ignore_permissions=True)
		forged = make_visit(routed_without_vitals=1)
		self.assertFalse(forged.routed_without_vitals)
		self.assertEqual(frappe.get_meta(VISIT_DOCTYPE).get_field("routed_without_vitals").permlevel, 1)

	def test_return_to_triage_then_vitals_then_route(self):
		visit = self.visit_in(VISIT_STATUS_IN_TRIAGE)
		self.act(visit, "route", "triage", department=DEPT_OUTPATIENT, without_vitals_reason="Collapsed")
		self.act(visit, "return_to_triage", "clinician", comment="Stable now, please triage")
		vs = self.submit_vitals(visit)
		visit = self.act(visit, "route", "triage", department=DEPT_MENTAL_HEALTH)
		self.assertEqual(visit.status, VISIT_STATUS_ROUTED)
		self.assertEqual(visit.triage_vital_signs, vs.name)
		self.assertFalse(visit.status_log[-1].comment)
		self.assertEqual(visit.routed_without_vitals, 1)  # sticky: the earlier override stays reportable

	def test_reroute_and_return_unaffected(self):
		visit = self.visit_in(VISIT_STATUS_IN_TRIAGE)
		self.act(visit, "route", "triage", department=DEPT_OUTPATIENT, without_vitals_reason="Collapsed")
		visit = self.act(visit, "reroute", "triage", department=DEPT_MENTAL_HEALTH)
		self.assertEqual(visit.current_department, DEPT_MENTAL_HEALTH)
		visit = self.act(visit, "return_to_triage", "triage", comment="Recheck")
		self.assertEqual(visit.status, VISIT_STATUS_IN_TRIAGE)

	def test_onload_flag(self):
		visit = self.visit_in(VISIT_STATUS_IN_TRIAGE)
		visit.run_method("onload")
		self.assertFalse(visit.get_onload().get("has_triage_vitals"))
		self.submit_vitals(visit)
		visit.reload()
		visit.run_method("onload")
		self.assertTrue(visit.get_onload().get("has_triage_vitals"))


class TestVisitStatusLock(VisitTestCase):
	def test_status_cannot_be_edited(self):
		"""SDD §4.4: status changes only through workflow actions, never by hand."""
		visit = make_visit()
		visit.status = VISIT_STATUS_DISCHARGED
		with self.assertRaises(VisitStatusLockedError):
			visit.save(ignore_permissions=True)

	def test_workflow_fields_cannot_be_edited(self):
		for field, value in (
			("current_department", DEPT_OUTPATIENT),
			("pharmacy_open", 1),
			("closed_at", now_datetime()),
			("arrived_at", now_datetime() - timedelta(hours=2)),
			("visit_date", add_days(today(), -1)),
			("queue_number", 999),
			("triage_level", frappe.db.get_value("Triage Level", {}, "name")),
			("triage_vital_signs", make_vital_signs(make_patient().name).name),
			("patient_identifier", "FORGED-ID"),
		):
			visit = make_visit()
			visit.set(field, value)
			with self.assertRaises(VisitStatusLockedError, msg=field):
				visit.save(ignore_permissions=True)

	def test_insert_overwrites_forged_values(self):
		"""SDD §4.4 / H3: server-owned fields sent on insert are ignored and set by the server."""
		forged_time = now_datetime() - timedelta(days=3)
		visit = make_visit(
			status=VISIT_STATUS_IN_CONSULTATION,
			visit_date=FUTURE,
			arrived_at=forged_time,
			status_changed_at=forged_time,
			queue_number=999,
			lab_open=1,
			pharmacy_open=1,
			current_department=DEPT_OUTPATIENT,
			closed_at=forged_time,
			routed_at=forged_time,
			triage_level=frappe.db.get_value("Triage Level", {}, "name"),
			patient_identifier="FORGED-ID",
			status_log=[
				{
					"from_status": VISIT_STATUS_IN_TRIAGE,
					"to_status": VISIT_STATUS_ROUTED,
					"action": "route",
					"user": self.users["reception"],
					"timestamp": forged_time,
				}
			],
		)
		self.assertEqual(visit.status, VISIT_STATUS_REGISTERED)
		self.assertEqual(str(visit.visit_date), today())
		self.assertLess(abs((now_datetime() - visit.arrived_at).total_seconds()), 120)
		self.assertEqual(visit.status_changed_at, visit.arrived_at)
		self.assertNotEqual(visit.queue_number, 999)
		self.assertFalse(visit.lab_open or visit.pharmacy_open)
		self.assertEqual(visit.current_department, DEPT_RECEPTION)
		self.assertFalse(visit.closed_at or visit.routed_at or visit.triage_level)
		self.assertNotEqual(visit.patient_identifier, "FORGED-ID")
		self.assertEqual(len(visit.status_log), 1)
		self.assertEqual(visit.status_log[0].action, "register")
		self.assertEqual(visit.status_log[0].user, "Administrator")

	def test_status_log_cannot_be_edited(self):
		visit = make_visit()
		visit.append("status_log", {"from_status": "x", "to_status": "y", "action": "hand"})
		with self.assertRaises(VisitStatusLockedError):
			visit.save(ignore_permissions=True)

	def test_triage_nurse_api_save_cannot_forge_history(self):
		"""H3: an API save by a writer that edits, removes or reorders log rows or arrival time is refused."""
		visit = self.visit_in(VISIT_STATUS_IN_TRIAGE)
		edits = {
			"edit log user": lambda d: setattr(d.status_log[0], "user", self.users["reception"]),
			"edit log timestamp": lambda d: setattr(
				d.status_log[1], "timestamp", d.status_log[1].timestamp - timedelta(minutes=30)
			),
			"edit log action": lambda d: setattr(d.status_log[1], "action", "route"),
			"remove log row": lambda d: d.set("status_log", d.status_log[1:]),
			"swap log rows": lambda d: d.set("status_log", list(reversed(d.status_log))),
			"remove and add row": lambda d: (
				d.set("status_log", d.status_log[:1]),
				d.append("status_log", {"from_status": "a", "to_status": "b", "action": "x"}),
			),
			"change arrived_at": lambda d: d.set("arrived_at", d.arrived_at - timedelta(hours=1)),
		}
		with self.as_user("triage"):
			for label, edit in edits.items():
				doc = frappe.get_doc(VISIT_DOCTYPE, visit.name)
				edit(doc)
				with self.assertRaises(VisitStatusLockedError, msg=label):
					doc.save()
		self.assertEqual(len(frappe.get_doc(VISIT_DOCTYPE, visit.name).status_log), 2)

	def test_patient_correction_rules(self):
		"""Receptionist may correct the patient while Registered; nobody may after triage starts."""
		visit = make_visit()
		other = make_patient()
		with self.as_user("triage"):
			doc = frappe.get_doc(VISIT_DOCTYPE, visit.name)
			doc.patient = other.name
			with self.assertRaises(VisitStatusLockedError):
				doc.save()
		with self.as_user("reception"):
			doc = frappe.get_doc(VISIT_DOCTYPE, visit.name)
			doc.patient = other.name
			doc.save()
		self.assertEqual(doc.patient_name, other.patient_name)
		self.act(doc, "start_triage", "triage")
		with self.as_user("reception"):
			doc = frappe.get_doc(VISIT_DOCTYPE, visit.name)
			doc.patient = make_patient().name
			with self.assertRaises(VisitStatusLockedError):
				doc.save()

	def test_other_fields_still_editable(self):
		visit = make_visit(is_outreach=1, outreach_session="T-SESSION-1")
		visit.outreach_session = "T-SESSION-2"
		visit.save(ignore_permissions=True)
		self.assertEqual(visit.status, VISIT_STATUS_REGISTERED)


class TestVisitTasks(VisitTestCase):
	def test_lab_and_pharmacy_open_together(self):
		"""SDD §4.4: laboratory and pharmacy tasks can both be open; discharge waits for dispensing."""
		visit = self.visit_in(VISIT_STATUS_IN_CONSULTATION)
		visit = self.act(visit, "request_lab")
		visit = self.act(visit, "prescribe")
		self.assertEqual(visit.status, VISIT_STATUS_AWAITING_LAB)
		self.assertTrue(visit.lab_open)
		self.assertTrue(visit.pharmacy_open)

		visit = self.act(visit, "lab_results_ready")
		self.assertEqual(visit.status, VISIT_STATUS_IN_CONSULTATION)
		self.assertFalse(visit.lab_open)
		self.assertTrue(visit.pharmacy_open)

		with self.assertRaises(PharmacyTaskOpenError):
			self.act(visit, "discharge", "clinician")

		visit = self.act(visit, "dispensed")
		self.assertEqual(visit.status, VISIT_STATUS_IN_CONSULTATION)
		self.assertFalse(visit.pharmacy_open)
		visit = self.act(visit, "discharge", "clinician")
		self.assertEqual(visit.status, VISIT_STATUS_DISCHARGED)

	def test_prescription_then_lab_request(self):
		visit = self.visit_in(VISIT_STATUS_IN_CONSULTATION)
		visit = self.act(visit, "prescribe")
		self.assertEqual(visit.status, VISIT_STATUS_AWAITING_PHARMACY)
		visit = self.act(visit, "request_lab")
		self.assertEqual(visit.status, VISIT_STATUS_AWAITING_LAB)
		self.assertTrue(visit.lab_open and visit.pharmacy_open)
		visit = self.act(visit, "dispensed")
		self.assertEqual(visit.status, VISIT_STATUS_AWAITING_LAB)
		self.assertFalse(visit.pharmacy_open)

	def test_discharge_blocked_while_pharmacy_open(self):
		visit = self.visit_in(VISIT_STATUS_IN_CONSULTATION)
		visit = self.act(visit, "prescribe")
		# Discharge is only possible from In Consultation (W0): from Awaiting Pharmacy it is illegal.
		with self.assertRaises(IllegalVisitTransitionError):
			self.act(visit, "discharge", "clinician")
		self.assertEqual(
			frappe.db.get_value(VISIT_DOCTYPE, visit.name, "status"), VISIT_STATUS_AWAITING_PHARMACY
		)

	def test_refer_only_from_consultation(self):
		"""SDD §4.4: Referred is entered from In Consultation; dispensing after referral closes the task."""
		visit = self.visit_in(VISIT_STATUS_IN_CONSULTATION)
		visit = self.act(visit, "prescribe")
		with self.assertRaises(IllegalVisitTransitionError):
			self.act(visit, "refer", "clinician")

		visit = self.visit_in(VISIT_STATUS_IN_CONSULTATION)
		visit = self.act(visit, "request_lab")
		visit = self.act(visit, "prescribe")
		visit = self.act(visit, "lab_results_ready")
		visit = self.act(visit, "refer", "clinician", comment="Transfer")
		self.assertEqual(visit.status, VISIT_STATUS_REFERRED)
		self.assertTrue(visit.pharmacy_open)
		visit = self.act(visit, "dispensed")
		self.assertEqual(visit.status, VISIT_STATUS_REFERRED)
		self.assertFalse(visit.pharmacy_open)


class TestVisitQueue(VisitTestCase):
	def test_queue_numbers_per_facility_per_day(self):
		"""FR-6: queue numbers increment per facility per day and restart for another facility or day."""
		f0, f1 = get_facility(0), get_facility(1)
		a = make_visit(facility=f0, visit_date=FUTURE, system=True)
		b = make_visit(facility=f0, visit_date=FUTURE, system=True)
		c = make_visit(facility=f1, visit_date=FUTURE, system=True)
		d = make_visit(facility=f0, visit_date=add_days(FUTURE, 1), system=True)
		self.assertEqual(a.queue_number, 1)
		self.assertEqual(b.queue_number, 2)
		self.assertEqual(c.queue_number, 1)
		self.assertEqual(d.queue_number, 1)

	def test_waiting_time(self):
		"""FR-6: waiting time since arrival, and time in the current status."""
		visit = make_visit()
		arrived = now_datetime() - timedelta(minutes=30)
		frappe.db.set_value(
			VISIT_DOCTYPE,
			visit.name,
			{"arrived_at": arrived, "status_changed_at": arrived + timedelta(minutes=20)},
			update_modified=False,
		)
		row = next(r for r in get_queue(visit.facility) if r.name == visit.name)
		self.assertIn(row["waiting_minutes"], (30, 31))
		self.assertIn(row["minutes_in_status"], (10, 11))
		self.assertEqual(waiting_minutes(arrived, arrived + timedelta(minutes=45, seconds=59)), 45)
		self.assertEqual(waiting_minutes(None), 0)

	def test_stale_visits_flagged_and_excluded_from_longest_wait(self):
		"""L1: yesterday's open visit stays in the queue as stale and does not count toward the KPI."""
		stale = make_visit(
			system=True,
			visit_date=add_days(today(), -1),
			arrived_at=now_datetime() - timedelta(days=2),
		)
		fresh = make_visit()
		frappe.db.set_value(
			VISIT_DOCTYPE,
			fresh.name,
			"arrived_at",
			now_datetime() - timedelta(minutes=30),
			update_modified=False,
		)
		rows = {r.name: r for r in get_queue(stale.facility)}
		self.assertEqual(rows[stale.name].stale, 1)
		self.assertEqual(rows[fresh.name].stale, 0)
		value = longest_wait_card()["value"]
		self.assertGreaterEqual(value, 30)
		self.assertLess(value, 24 * 60)

	def test_get_queue_lists_open_visits_in_order(self):
		facility = get_facility(0)
		first, second, closed = make_visit(), make_visit(), self.visit_in(VISIT_STATUS_IN_CONSULTATION)
		self.act(closed, "discharge", "clinician")
		with self.as_user("reception"):
			rows = get_queue(facility)
		names = [r.name for r in rows]
		self.assertIn(first.name, names)
		self.assertIn(second.name, names)
		self.assertNotIn(closed.name, names)
		self.assertLess(names.index(first.name), names.index(second.name))
		self.assertTrue(all(r.status in VISIT_OPEN_STATUSES for r in rows))
		row = rows[names.index(first.name)]
		self.assertEqual(row.patient_name, first.patient_name)
		self.assertEqual(row.queue_number, first.queue_number)
		self.assertEqual(row.current_department, DEPT_RECEPTION)

		with self.as_user("reception"):
			registered = get_queue(facility, VISIT_STATUS_REGISTERED)
		self.assertTrue(all(r.status == VISIT_STATUS_REGISTERED for r in registered))
		with self.assertRaises(frappe.ValidationError):
			get_queue(facility, "T-Not A Status")

	def test_head_of_operations_sees_status_only(self):
		"""SDD §6.1: Head of Operations sees the queue but no patient-level fields."""
		visit = make_visit()
		with self.as_user("hoo"):
			row = next(r for r in get_queue(visit.facility) if r.name == visit.name)
			self.assertEqual(row.status, VISIT_STATUS_REGISTERED)
			self.assertNotIn("patient_name", row)
			self.assertNotIn("patient", row)
			listed = frappe.get_list(
				VISIT_DOCTYPE, filters={"name": visit.name}, fields=["name", "status", "patient_name"]
			)
			self.assertEqual(len(listed), 1)
			self.assertIsNone(listed[0].get("patient_name"))

	def test_routing_and_department_filter(self):
		"""FR-8/FR-9: routing sets the department; the department view filter finds the visit."""
		visit = self.visit_in(VISIT_STATUS_ROUTED, department=DEPT_MENTAL_HEALTH)
		self.assertEqual(visit.current_department, DEPT_MENTAL_HEALTH)
		dept_filter = {"current_department": DEPT_MENTAL_HEALTH, "status": ["in", list(VISIT_OPEN_STATUSES)]}
		with self.as_user("mho"):
			found = frappe.get_list(VISIT_DOCTYPE, filters=dept_filter, pluck="name")
		self.assertIn(visit.name, found)
		other = frappe.get_list(
			VISIT_DOCTYPE, filters={**dept_filter, "current_department": DEPT_OUTPATIENT}, pluck="name"
		)
		self.assertNotIn(visit.name, other)
		self.act(visit, "start_consultation", "mho")
		self.act(visit, "discharge", "mho")
		with self.as_user("mho"):
			self.assertNotIn(visit.name, frappe.get_list(VISIT_DOCTYPE, filters=dept_filter, pluck="name"))


class TestVisitPermissions(VisitTestCase):
	def test_read_matrix(self):
		visit = make_visit()
		for key in ("reception", "triage", "clinician", "mho", "lab", "pharmacist", "his", "mic", "hoo"):
			with self.as_user(key):
				self.assertTrue(frappe.has_permission(VISIT_DOCTYPE, "read", doc=visit.name), key)
		with self.as_user("sysadmin"):
			self.assertFalse(frappe.has_permission(VISIT_DOCTYPE, "read", doc=visit.name))
			with self.assertRaises(frappe.PermissionError):
				frappe.get_list(VISIT_DOCTYPE, filters={"name": visit.name})

	def test_write_matrix(self):
		visit = make_visit()
		for key in ("reception", "triage", "clinician", "mho"):
			with self.as_user(key):
				self.assertTrue(frappe.has_permission(VISIT_DOCTYPE, "write", doc=visit.name), key)
		for key in ("lab", "pharmacist", "his", "mic", "hoo", "sysadmin"):
			with self.as_user(key):
				self.assertFalse(frappe.has_permission(VISIT_DOCTYPE, "write", doc=visit.name), key)

	def test_no_delete_for_anyone(self):
		visit = make_visit()
		for key in self.users:
			with self.as_user(key):
				self.assertFalse(frappe.has_permission(VISIT_DOCTYPE, "delete", doc=visit.name), key)

	def test_closed_visit_is_read_only(self):
		visit = self.visit_in(VISIT_STATUS_IN_CONSULTATION)
		self.act(visit, "discharge", "clinician")
		with self.as_user("clinician"):
			self.assertTrue(frappe.has_permission(VISIT_DOCTYPE, "read", doc=visit.name))
			self.assertFalse(frappe.has_permission(VISIT_DOCTYPE, "write", doc=visit.name))

	def test_head_of_operations_cannot_probe_patient_fields(self):
		"""L3: HoO cannot filter or search visits by patient name, identifier or patient ID."""
		from frappe.client import get_count as client_get_count
		from frappe.desk.search import search_widget

		visit = make_visit()
		with self.as_user("hoo"):
			for field, value in (
				("patient_name", visit.patient_name),
				("patient", visit.patient),
				("patient_identifier", visit.patient_identifier or "x"),
			):
				with self.assertRaises(frappe.PermissionError, msg=field):
					frappe.get_list(VISIT_DOCTYPE, filters={field: value})
				with self.assertRaises(frappe.PermissionError, msg=field):
					client_get_count(VISIT_DOCTYPE, {field: value})
			with self.assertRaises(frappe.PermissionError):
				search_widget(VISIT_DOCTYPE, visit.patient_name)
		self.assertFalse(frappe.get_meta(VISIT_DOCTYPE).show_title_field_in_link)
		self.assertFalse(frappe.get_meta(VISIT_DOCTYPE).get_field("patient_name").in_global_search)

	def test_head_of_operations_has_no_clinical_permlevel(self):
		meta = frappe.get_meta(VISIT_DOCTYPE)
		with self.as_user("hoo"):
			self.assertEqual(meta.get_permlevel_access("read"), [0])
		with self.as_user("triage"):
			self.assertIn(1, meta.get_permlevel_access("read"))


class TestTriage(VisitTestCase):
	def test_plausibility_errors(self):
		"""NFR-10: implausible or non-numeric vital signs are rejected with a clear message."""
		patient = make_patient().name
		bad = [
			{"temperature": "abc"},
			{"temperature": "98.6"},
			{"temperature": "50"},
			{"pulse": "400"},
			{"pulse": "72.5"},
			{"respiratory_rate": "0"},
			{"bp_systolic": "80", "bp_diastolic": "120"},
			{"bp_systolic": "120", "bp_diastolic": ""},
			{"bp_systolic": "1200", "bp_diastolic": "80"},
			{"height": 165},
			{"weight": 900},
		]
		for values in bad:
			with self.assertRaises(ImplausibleVitalSignsError, msg=str(values)):
				make_vital_signs(patient, **values)

	def test_fahrenheit_message_is_clear(self):
		patient = make_patient().name
		with self.assertRaises(ImplausibleVitalSignsError) as ctx:
			make_vital_signs(patient, temperature="98.6")
		self.assertIn("Fahrenheit", str(ctx.exception))

	def test_valid_values_are_normalised(self):
		vs = make_vital_signs(
			make_patient().name,
			temperature=" 37,5 ",
			pulse="88",
			bp_systolic="118",
			bp_diastolic="76",
			height=1.7,
			weight=65,
		)
		self.assertEqual(vs.temperature, "37.5")
		self.assertEqual(vs.bp, "118/76 mmHg")

	def test_blank_vitals_allowed(self):
		vs = make_vital_signs(
			make_patient().name,
			temperature="",
			pulse="",
			respiratory_rate="",
			bp_systolic="",
			bp_diastolic="",
		)
		self.assertTrue(vs.name)

	def test_vital_signs_submit_starts_triage(self):
		"""FR-9: submitting triage vital signs moves a Registered visit to In Triage; routing stays manual."""
		visit = make_visit()
		level = get_triage_level()
		vs = make_vital_signs(
			visit.patient,
			visit=visit.name,
			krcs_triage_level=level,
			krcs_chief_complaint="Cough for 3 days",
			krcs_initial_assessment="Alert, mild distress",
		)
		self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit.name, "status"), VISIT_STATUS_REGISTERED)
		with self.as_user("triage"):
			vs.submit()
		visit.reload()
		self.assertEqual(visit.status, VISIT_STATUS_IN_TRIAGE)
		self.assertEqual(visit.triage_vital_signs, vs.name)
		self.assertEqual(visit.triage_level, level)
		self.assertEqual(visit.status_log[-1].user, self.users["triage"])

		with self.as_user("triage"):
			apply_action(visit.name, "route", department=DEPT_OUTPATIENT)
		self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit.name, "status"), VISIT_STATUS_ROUTED)

	def test_vital_signs_on_visit_already_in_triage(self):
		visit = self.visit_in(VISIT_STATUS_IN_TRIAGE)
		make_vital_signs(visit.patient, visit=visit.name, submit=True)
		visit.reload()
		self.assertEqual(visit.status, VISIT_STATUS_IN_TRIAGE)
		self.assertTrue(visit.triage_vital_signs)
		self.assertEqual(len([r for r in visit.status_log if r.action == "start_triage"]), 1)

	def test_vital_signs_visit_must_match_patient(self):
		visit = make_visit()
		with self.assertRaises(frappe.ValidationError):
			make_vital_signs(make_patient().name, visit=visit.name)

	def test_custom_fields_installed(self):
		meta = frappe.get_meta("Vital Signs")
		for field in ("krcs_visit", "krcs_triage_level", "krcs_initial_assessment", "krcs_chief_complaint"):
			self.assertTrue(meta.has_field(field), field)


class TestVisitWorkspaces(VisitTestCase):
	"""FR-8/BR-3, C-5: department queues live on Marley's Healthcare, Outpatient and Diagnostics pages."""

	def rows(self, workspace: str, child: str) -> list:
		return frappe.get_all(
			child, filters={"parenttype": "Workspace", "parent": workspace}, fields=["*"], order_by="idx"
		)

	def test_old_workspaces_removed(self):
		for name in workspaces.RETIRED_WORKSPACES:
			self.assertFalse(frappe.db.exists("Workspace", name), name)
		self.assertEqual(frappe.get_all("Workspace", filters={"module": "EMR Visit"}), [])

	def test_department_blocks_on_marley_pages(self):
		for dept, workspace in workspaces.DEPARTMENT_WORKSPACE.items():
			content = json.loads(frappe.db.get_value("Workspace", workspace, "content"))
			headers = [b["data"]["text"] for b in content if b["type"] == "header"]
			self.assertTrue(any(f"{dept} Queue" in h for h in headers), f"{dept} header on {workspace}")
			quick = {q.label: q for q in self.rows(workspace, "Workspace Quick List")}
			shortcuts = {r.label for r in self.rows(workspace, "Workspace Shortcut")}
			block = workspaces.department_block(dept)
			for ql in block["quick_lists"]:
				self.assertIn(ql["label"], quick, ql["label"])
				self.assertEqual(
					json.loads(quick[ql["label"]].quick_list_filter), json.loads(ql["quick_list_filter"])
				)
				self.assertEqual(quick[ql["label"]].document_type, VISIT_DOCTYPE)
				self.assertIn(ql["label"], [b["data"].get("quick_list_name") for b in content])
			for sc in block["shortcuts"]:
				self.assertIn(sc["label"], shortcuts)
		reception = json.loads(self.rows("Healthcare", "Workspace Quick List")[0].quick_list_filter)
		self.assertIn([VISIT_DOCTYPE, "status", "=", VISIT_STATUS_REGISTERED], reception)
		outpatient = {q.label: q for q in self.rows("Outpatient", "Workspace Quick List")}
		self.assertEqual(
			json.loads(outpatient[f"{DEPT_MENTAL_HEALTH} Queue"].quick_list_filter),
			[
				[VISIT_DOCTYPE, "current_department", "=", DEPT_MENTAL_HEALTH],
				[VISIT_DOCTYPE, "status", "in", list(VISIT_OPEN_STATUSES)],
			],
		)
		cards = {r.number_card_name for r in self.rows("Healthcare", "Workspace Number Card")}
		self.assertTrue(set(workspaces.OVERVIEW_CARDS) <= cards)

	def test_no_billing_items(self):
		"""C-5: no shortcut, link, number card or chart on these pages targets billing."""
		billing = set(BILLING_DOCTYPES)
		for workspace in ("Healthcare", "Outpatient", "Diagnostics"):
			for r in self.rows(workspace, "Workspace Shortcut"):
				self.assertNotIn(r.link_to, billing, f"{workspace}: {r.label}")
			for r in self.rows(workspace, "Workspace Link"):
				self.assertNotIn(r.link_to, billing, f"{workspace}: {r.label}")
			for r in self.rows(workspace, "Workspace Number Card"):
				card = frappe.db.get_value(
					"Number Card", r.number_card_name, ["document_type", "filters_json"], as_dict=True
				)
				self.assertNotIn(card.document_type, billing, r.label)
				self.assertFalse(workspaces.is_billing_filter(card.filters_json), r.label)
			for r in self.rows(workspace, "Workspace Chart"):
				chart = frappe.db.get_value(
					"Dashboard Chart", r.chart_name, ["document_type", "filters_json"], as_dict=True
				)
				self.assertNotIn(chart.document_type, billing, r.label)
				self.assertFalse(workspaces.is_billing_filter(chart.filters_json), r.label)
			content = json.loads(frappe.db.get_value("Workspace", workspace, "content"))
			names = [
				str(b["data"].get("shortcut_name") or b["data"].get("number_card_name")) for b in content
			]
			self.assertFalse([n for n in names if "Bill" in n or "Sales Invoice" in n], workspace)

	def test_install_is_idempotent(self):
		def snapshot():
			out = {}
			for workspace in ("Healthcare", "Outpatient", "Diagnostics"):
				out[workspace] = (
					frappe.db.get_value("Workspace", workspace, "content"),
					*(
						sorted((r.label, r.name) for r in self.rows(workspace, child))
						for child in ("Workspace Shortcut", "Workspace Quick List", "Workspace Number Card")
					),
				)
			return out

		workspaces.install()
		before = snapshot()
		workspaces.install()
		self.assertEqual(snapshot(), before)
		for workspace in ("Healthcare", "Outpatient", "Diagnostics"):
			labels = [r.label for r in self.rows(workspace, "Workspace Shortcut")]
			self.assertEqual(len(labels), len(set(labels)), workspace)

	def test_reinstall_after_marley_reload(self):
		"""A Marley update drops our rows and blocks; the next install puts them back once."""
		frappe.db.delete(
			"Workspace Quick List",
			{"parenttype": "Workspace", "parent": "Diagnostics", "label": "Laboratory Queue"},
		)
		content = [
			b
			for b in json.loads(frappe.db.get_value("Workspace", "Diagnostics", "content"))
			if not b["id"].startswith(workspaces.BLOCK_PREFIX)
		]
		frappe.db.set_value("Workspace", "Diagnostics", "content", json.dumps(content), update_modified=False)
		workspaces.install()
		labels = [r.label for r in self.rows("Diagnostics", "Workspace Quick List")]
		self.assertEqual(labels.count("Laboratory Queue"), 1)

	def test_role_users_load_pages(self):
		"""Receptionist and Laboratory Technician see their page and its queue widgets."""
		from frappe.desk.desktop import get_desktop_page, get_workspaces

		for key, workspace, quick in (
			("reception", "Healthcare", "Reception Queue"),
			("lab", "Diagnostics", "Laboratory Queue"),
		):
			with self.as_user(key):
				visible = [p["name"] for p in get_workspaces()["pages"]]
				self.assertIn(workspace, visible, key)
				page = get_desktop_page(json.dumps({"name": workspace, "title": workspace, "public": 1}))
				self.assertIn(quick, [q.label for q in page["quick_lists"]["items"]], key)
				self.assertTrue(page["shortcuts"]["items"], key)


class TestVisitValidation(VisitTestCase):
	"""NFR-10: server-side validation of user-entered visit and triage values, with clear messages."""

	def test_outreach_visit_needs_session(self):
		with self.assertRaises(frappe.MandatoryError):
			make_visit(is_outreach=1)
		with self.assertRaises(frappe.ValidationError):
			make_visit(is_outreach=1, outreach_session="S" * 141)
		visit = make_visit(is_outreach=1, outreach_session="  T-SESSION-9  ")
		self.assertEqual(visit.outreach_session, "T-SESSION-9")
		facility_visit = make_visit(outreach_session="T-IGNORED")
		self.assertIsNone(facility_visit.outreach_session)

	def test_mandatory_fields(self):
		with self.assertRaises(frappe.MandatoryError):
			frappe.get_doc({"doctype": VISIT_DOCTYPE, "facility": get_facility(0)}).insert(
				ignore_permissions=True
			)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": VISIT_DOCTYPE, "patient": make_patient().name}).insert(
				ignore_permissions=True
			)

	def test_referral_needs_reason(self):
		visit = self.visit_in(VISIT_STATUS_IN_CONSULTATION)
		for comment in (None, "", "   "):
			with self.assertRaises(IllegalVisitTransitionError, msg=repr(comment)):
				self.act(visit, "refer", "clinician", comment=comment)
		self.assertEqual(
			frappe.db.get_value(VISIT_DOCTYPE, visit.name, "status"), VISIT_STATUS_IN_CONSULTATION
		)

	def test_comment_length_limited(self):
		visit = self.visit_in(VISIT_STATUS_IN_TRIAGE)
		with self.assertRaises(IllegalVisitTransitionError):
			self.act(visit, "route", "triage", department=DEPT_OUTPATIENT, comment="x" * 1001)
		self.submit_vitals(visit)
		visit = self.act(visit, "route", "triage", department=DEPT_OUTPATIENT, comment="  Fever  ")
		self.assertEqual(visit.status_log[-1].comment, "Fever")

	def test_triage_submit_requires_findings(self):
		"""FR-9: triage vitals for a visit need a triage level, a chief complaint and at least one vital."""
		for missing in ({"krcs_triage_level": None}, {"krcs_chief_complaint": "  "}):
			visit = make_visit()
			vs = make_vital_signs(visit.patient, visit=visit.name, **missing)  # draft save is allowed
			with self.assertRaises(TriageIncompleteError, msg=str(missing)):
				vs.submit()
		visit = make_visit()
		vs = make_vital_signs(
			visit.patient,
			visit=visit.name,
			temperature="",
			pulse="",
			respiratory_rate="",
			bp_systolic="",
			bp_diastolic="",
		)
		with self.assertRaises(TriageIncompleteError):
			vs.submit()

	def test_triage_text_length(self):
		with self.assertRaises(TriageIncompleteError):
			make_vital_signs(make_patient().name, krcs_chief_complaint="c" * 501)
		with self.assertRaises(TriageIncompleteError):
			make_vital_signs(make_patient().name, krcs_initial_assessment="a" * 5001)

	def test_vitals_not_in_future(self):
		with self.assertRaises(ImplausibleVitalSignsError):
			make_vital_signs(make_patient().name, signs_date=add_days(today(), 1))


class TestVisitIndexes(VisitTestCase):
	def test_indexes_exist(self):
		"""NFR-01: queue, workspace and KPI queries are backed by indexes."""
		table = f"tab{VISIT_DOCTYPE}"
		for name, columns in VISIT_INDEXES.items():
			rows = frappe.db.sql(f"SHOW INDEX FROM `{table}` WHERE Key_name=%s", (name,), as_dict=True)
			self.assertEqual(
				[r.Column_name for r in sorted(rows, key=lambda r: r.Seq_in_index)], list(columns)
			)
		for column in ("patient", "facility", "visit_date", "status", "current_department"):
			self.assertTrue(frappe.db.get_column_index(table, column, unique=False), column)
		self.assertTrue(frappe.db.get_column_index("tabVital Signs", "krcs_visit", unique=False))


class TestTriageFieldsMarkedMandatory(EMRTestCase):
	def test_triage_fields_starred_when_linked_to_visit(self):
		"""FR-9: the form marks Triage Level and Chief Complaint mandatory when vitals belong to a visit."""
		meta = frappe.get_meta("Vital Signs")
		for fieldname in ("krcs_triage_level", "krcs_chief_complaint"):
			self.assertEqual(meta.get_field(fieldname).mandatory_depends_on, "eval:doc.krcs_visit", fieldname)


class TestPhase2Engine(VisitTestCase):
	"""Phase 2 W0: guards, complete_consultation, close_visit, receive_referral, raise_event, reconciler."""

	def consulting_visit(self, user: str = "clinician"):
		return self.visit_in(VISIT_STATUS_IN_CONSULTATION)

	# -- complete_consultation -------------------------------------------------------------
	def test_complete_needs_submitted_encounter(self):
		visit = self.consulting_visit()
		with self.as_user("clinician"):
			self.assertNotIn(
				"complete_consultation", [a["action"] for a in engine.get_available_actions(visit)]
			)
		with self.assertRaises(VisitGuardError):
			self.act(visit, "complete_consultation", "clinician")
		make_submitted_encounter(visit.patient, visit.name, submit=False)
		with self.assertRaises(VisitGuardError):
			self.act(visit, "complete_consultation", "clinician")

	def test_complete_without_tasks_discharges(self):
		visit = self.consulting_visit()
		make_submitted_encounter(visit.patient, visit.name)
		visit.reload()
		with self.as_user("mho"):
			self.assertIn("complete_consultation", [a["action"] for a in engine.get_available_actions(visit)])
		visit = self.act(visit, "complete_consultation", "mho")
		self.assertEqual(visit.status, VISIT_STATUS_DISCHARGED)
		self.assertTrue(visit.closed_at)

	def test_complete_roles(self):
		visit = self.consulting_visit()
		make_submitted_encounter(visit.patient, visit.name)
		for user in ("reception", "triage", "lab", "pharmacist", "his", "mic", "hoo"):
			with self.assertRaises(frappe.PermissionError, msg=user):
				self.act(visit, "complete_consultation", user)

	def test_complete_with_pharmacy_pending(self):
		visit = self.consulting_visit()
		self.act(visit, "request_lab")
		self.act(visit, "prescribe")
		visit = self.act(visit, "lab_results_ready")
		self.assertEqual((visit.status, visit.pharmacy_open), (VISIT_STATUS_IN_CONSULTATION, 1))
		make_submitted_encounter(visit.patient, visit.name)
		visit = self.act(visit, "complete_consultation", "clinician")
		self.assertEqual(visit.status, VISIT_STATUS_AWAITING_PHARMACY)
		self.assertFalse(visit.closed_at)
		visit = self.act(visit, "dispensed")
		self.assertEqual(visit.status, VISIT_STATUS_DISCHARGED)
		self.assertTrue(visit.closed_at)

	def test_complete_with_results_pending_needs_reason(self):
		visit = self.consulting_visit()
		visit = self.act(visit, "request_lab")
		make_submitted_encounter(visit.patient, visit.name)
		visit.reload()
		with self.as_user("clinician"):
			action = next(
				a for a in engine.get_available_actions(visit) if a["action"] == "complete_consultation"
			)
		self.assertTrue(action["needs_comment"])
		with self.assertRaises(IllegalVisitTransitionError):
			self.act(visit, "complete_consultation", "clinician")
		visit = self.act(visit, "complete_consultation", "clinician", comment="Malaria RDT pending; treated")
		self.assertEqual((visit.status, visit.lab_open), (VISIT_STATUS_AWAITING_LAB, 1))
		visit = self.act(visit, "lab_results_ready")
		self.assertEqual(visit.status, VISIT_STATUS_DISCHARGED)
		self.assertFalse(visit.lab_open)

	def test_complete_with_lab_and_pharmacy_pending(self):
		visit = self.consulting_visit()
		self.act(visit, "request_lab")
		visit = self.act(visit, "prescribe")
		self.assertEqual(visit.status, VISIT_STATUS_AWAITING_LAB)
		make_submitted_encounter(visit.patient, visit.name)
		visit = self.act(visit, "complete_consultation", "clinician", comment="Results to follow")
		self.assertEqual(visit.status, VISIT_STATUS_AWAITING_PHARMACY)  # pharmacy first after completion
		self.assertTrue(visit.lab_open and visit.pharmacy_open)
		visit = self.act(visit, "dispensed")
		self.assertEqual(
			(visit.status, visit.lab_open, visit.pharmacy_open), (VISIT_STATUS_AWAITING_LAB, 1, 0)
		)
		visit = self.act(visit, "lab_results_ready")
		self.assertEqual(visit.status, VISIT_STATUS_DISCHARGED)

	def test_dispensed_before_completion_returns_to_consultation(self):
		visit = self.consulting_visit()
		visit = self.act(visit, "prescribe")
		self.assertEqual(visit.status, VISIT_STATUS_AWAITING_PHARMACY)
		visit = self.act(visit, "dispensed")
		self.assertEqual(visit.status, VISIT_STATUS_IN_CONSULTATION)
		self.assertFalse(visit.closed_at)

	def test_discharge_only_from_consultation_with_no_tasks(self):
		visit = self.consulting_visit()
		self.act(visit, "request_lab")
		self.act(visit, "prescribe")
		visit = self.act(visit, "lab_results_ready")
		with self.as_user("clinician"):
			self.assertNotIn("discharge", [a["action"] for a in engine.get_available_actions(visit)])
		with self.assertRaises(PharmacyTaskOpenError):
			self.act(visit, "discharge", "clinician")

	# -- close_visit -----------------------------------------------------------------------
	def test_close_visit_from_every_open_status(self):
		reason = VISIT_CLOSURE_REASONS[0]
		cases = [
			(VISIT_STATUS_REGISTERED, "reception"),
			(VISIT_STATUS_IN_TRIAGE, "triage"),
			(VISIT_STATUS_ROUTED, "mic"),
			(VISIT_STATUS_IN_CONSULTATION, "mho"),
		]
		for status, user in cases:
			visit = self.visit_in(status)
			visit = self.act(visit, "close_visit", user, closure_reason=reason, comment="Gone home")
			self.assertEqual(visit.status, VISIT_STATUS_DISCHARGED, status)
			self.assertEqual(visit.closure_reason, reason)
			self.assertTrue(visit.closed_at)
			self.assertEqual(visit.status_log[-1].comment, f"Closed: {reason} — Gone home")
		for flags in (("request_lab",), ("prescribe",)):
			visit = self.consulting_visit()
			for action in flags:
				self.act(visit, action)
			visit = self.act(visit, "close_visit", "clinician", closure_reason=reason, comment="Absconded")
			self.assertEqual(
				(visit.status, visit.lab_open, visit.pharmacy_open), (VISIT_STATUS_DISCHARGED, 0, 0)
			)

	def test_close_visit_refused(self):
		visit = make_visit()
		reason = VISIT_CLOSURE_REASONS[-1]
		for user in ("lab", "pharmacist", "his", "hoo", "sysadmin"):
			with self.assertRaises(frappe.PermissionError, msg=user):
				self.act(visit, "close_visit", user, closure_reason=reason, comment="x")
		with self.assertRaises(IllegalVisitTransitionError):
			self.act(visit, "close_visit", "reception", closure_reason="Bored", comment="x")
		with self.assertRaises(IllegalVisitTransitionError):
			self.act(visit, "close_visit", "reception", closure_reason=reason)
		self.act(visit, "close_visit", "reception", closure_reason=reason, comment="Error")
		with self.assertRaises(IllegalVisitTransitionError):
			self.act(visit, "close_visit", "reception", closure_reason=reason, comment="again")
		with self.as_user("reception"):
			self.assertEqual(engine.get_available_actions(frappe.get_doc(VISIT_DOCTYPE, visit.name)), [])

	def test_close_visit_calls_registered_handlers(self):
		CLOSED_CALLS.clear()
		visit = make_visit()
		with patch.object(
			tasks, "VISIT_CLOSED_HANDLERS", ["onerc_healthcare.tests.test_visit._record_closed"]
		):
			with self.as_user("reception"):
				apply_action(
					visit.name,
					"close_visit",
					comment="Wrong patient",
					closure_reason=VISIT_CLOSURE_REASONS[3],
				)
		self.assertEqual(CLOSED_CALLS, [visit.name])

	# -- receive_referral ------------------------------------------------------------------
	def test_receive_referral(self):
		visit = make_visit()
		kwargs = {}
		if frappe.db.table_exists("Referral"):
			with self.assertRaises(IllegalVisitTransitionError):
				self.act(visit, "receive_referral", department=DEPT_MENTAL_HEALTH)
			self.skipTest("Referral DocType exists; B1's tests cover a real referral")
		with self.assertRaises(frappe.PermissionError):
			self.act(visit, "receive_referral", "triage", department=DEPT_MENTAL_HEALTH)
		for dept in (DEPT_TRIAGE, DEPT_RECEPTION, None):
			with self.assertRaises(IllegalVisitTransitionError, msg=str(dept)):
				self.act(visit, "receive_referral", department=dept)
		visit = self.act(visit, "receive_referral", department=DEPT_MENTAL_HEALTH, **kwargs)
		self.assertEqual(visit.status, VISIT_STATUS_ROUTED)  # no triage vitals needed
		self.assertEqual(visit.current_department, DEPT_MENTAL_HEALTH)
		self.assertFalse(visit.routed_without_vitals)
		self.assertTrue(visit.routed_at)
		self.assertEqual(visit.status_log[-1].action, "receive_referral")

	# -- guards == available actions ---------------------------------------------------------
	def test_available_actions_agree_with_transition(self):
		visits = [self.visit_in(s) for s in (VISIT_STATUS_REGISTERED, VISIT_STATUS_ROUTED)]
		c = self.consulting_visit()
		visits.append(c)
		lab = self.consulting_visit()
		self.act(lab, "request_lab")
		visits.append(lab)
		done = self.consulting_visit()
		make_submitted_encounter(done.patient, done.name)
		self.act(done, "prescribe")
		visits.append(done)
		for visit in visits:
			visit = frappe.get_doc(VISIT_DOCTYPE, visit.name)
			offered = {a["action"] for a in engine.get_available_actions(visit, user="Administrator")}
			for action, rule in engine.TRANSITIONS.items():
				if rule.is_system:
					continue
				legal = tasks.blocked_reason(visit, action) is None
				self.assertEqual(action in offered, legal, f"{action} on {visit.status}")

	# -- raise_event / reconcile -------------------------------------------------------------
	def test_raise_event_skips_illegal_events(self):
		visit = self.consulting_visit()
		self.act(visit, "request_lab")
		self.act(visit, "close_visit", "clinician", closure_reason=VISIT_CLOSURE_REASONS[1], comment="Left")
		self.assertFalse(tasks.raise_event(visit.name, "lab_results_ready", comment="Result entered"))
		self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit.name, "status"), VISIT_STATUS_DISCHARGED)
		self.assertTrue(
			frappe.db.exists(
				"Comment",
				{
					"reference_doctype": VISIT_DOCTYPE,
					"reference_name": visit.name,
					"content": ["like", "Skipped lab_results_ready:%"],
				},
			)
		)
		other = self.consulting_visit()
		self.assertTrue(tasks.raise_event(other.name, "request_lab"))
		self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, other.name, "status"), VISIT_STATUS_AWAITING_LAB)
		with self.assertRaises(ValueError):
			tasks.raise_event(other.name, "teleport")
		with self.assertRaises(frappe.DoesNotExistError):
			tasks.raise_event("PV-NO-SUCH-VISIT", "request_lab")

	def test_reconciler_with_fake_checkers(self):
		visit = self.consulting_visit()
		self.act(visit, "request_lab")
		self.act(visit, "prescribe")
		checkers = {
			"lab": "onerc_healthcare.tests.test_visit._fake_checker",
			"pharmacy": "onerc_healthcare.tests.test_visit._fake_checker",
		}
		COMPLETE_TASKS.clear()
		with patch.object(tasks, "TASK_CHECKERS", checkers):
			tasks.reconcile_open_tasks()
			self.assertEqual(
				frappe.db.get_value(VISIT_DOCTYPE, visit.name, "status"), VISIT_STATUS_AWAITING_LAB
			)
			COMPLETE_TASKS.add(visit.name)
			tasks.reconcile_open_tasks()
			doc = frappe.get_doc(VISIT_DOCTYPE, visit.name)
			self.assertEqual((doc.lab_open, doc.pharmacy_open), (0, 0))
			self.assertEqual(doc.status, VISIT_STATUS_IN_CONSULTATION)
			self.assertEqual(
				[r.comment for r in doc.status_log if r.action in ("lab_results_ready", "dispensed")],
				["Reconciled", "Reconciled"],
			)
			other = self.consulting_visit()
			self.act(other, "request_lab")
			COMPLETE_TASKS.add(other.name)
			with self.as_user("clinician"), self.assertRaises(frappe.PermissionError):
				tasks.recheck_tasks(other.name)
			with self.as_user("mic"):
				out = tasks.recheck_tasks(other.name)
			self.assertEqual(out["raised"], ["lab_results_ready"])
			self.assertEqual(out["status"], VISIT_STATUS_IN_CONSULTATION)
		COMPLETE_TASKS.clear()

	def test_reconcile_without_checkers_is_noop(self):
		with patch.object(tasks, "TASK_CHECKERS", {}):
			self.assertEqual(tasks.reconcile_open_tasks(), 0)

	# -- encounter link and protected fields ----------------------------------------------------
	def test_link_encounter(self):
		visit = self.consulting_visit()
		enc = make_submitted_encounter(visit.patient, visit.name)
		self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit.name, "encounter"), enc.name)
		self.assertEqual(tasks.visit_for_encounter(enc.name), visit.name)
		other = self.consulting_visit()
		with self.assertRaises(IllegalVisitTransitionError):
			engine.link_encounter(other.name, enc.name)

	def test_new_fields_protected(self):
		visit = self.consulting_visit()
		enc = make_submitted_encounter(visit.patient)
		for field, value in (
			("encounter", enc.name),
			("closure_reason", VISIT_CLOSURE_REASONS[0]),
		):
			doc = frappe.get_doc(VISIT_DOCTYPE, visit.name)
			doc.set(field, value)
			with self.assertRaises(VisitStatusLockedError, msg=field):
				doc.save(ignore_permissions=True)
		forged = make_visit(encounter=enc.name, closure_reason=VISIT_CLOSURE_REASONS[0])
		self.assertFalse(forged.encounter or forged.closure_reason or forged.referred_from)
		meta = frappe.get_meta(VISIT_DOCTYPE)
		self.assertEqual(meta.get_field("encounter").permlevel, 1)
		self.assertEqual(meta.get_field("referred_from").permlevel, 1)
		self.assertEqual(meta.get_field("closure_reason").permlevel, 0)
