# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Patient Visit lifecycle, queue, triage and permissions (FR-6 to FR-9, BR-2, BR-3, NFR-10; SDD §4.4, §5.2)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

import frappe
from frappe.utils import add_days, now_datetime, today

from onerc_healthcare.constants import (
	DEPARTMENTS,
	DEPT_MENTAL_HEALTH,
	DEPT_OUTPATIENT,
	DEPT_RECEPTION,
	DEPT_TRIAGE,
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
from onerc_healthcare.emr_visit.doctype.patient_visit.patient_visit import VisitStatusLockedError
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import get_facility, make_patient
from onerc_healthcare.tests.visit_factories import make_visit, make_visit_users, make_vital_signs
from onerc_healthcare.visit import engine
from onerc_healthcare.visit.engine import (
	IllegalVisitTransitionError,
	PharmacyTaskOpenError,
	apply_action,
	transition,
)
from onerc_healthcare.visit.events import ImplausibleVitalSignsError
from onerc_healthcare.visit.patients import MERGED_INTO_FIELD, InactivePatientError
from onerc_healthcare.visit.queue import get_queue, longest_wait_card, waiting_minutes

FUTURE = "2099-03-01"


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

	def visit_in(self, status: str, department: str = DEPT_OUTPATIENT):
		"""A visit advanced to `status` along the normal path."""
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
		self.assertTrue(visit.name.startswith("PV-"))
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
		with self.assertRaises(frappe.ValidationError):
			make_visit(patient=visit.patient)

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

		visit = self.act(visit, "route", "triage", department=DEPT_OUTPATIENT)
		self.assertEqual(visit.status, VISIT_STATUS_ROUTED)
		self.assertEqual(visit.current_department, DEPT_OUTPATIENT)
		self.assertTrue(visit.routed_at)

		visit = self.act(visit, "start_consultation", "clinician")
		self.assertEqual(visit.status, VISIT_STATUS_IN_CONSULTATION)
		self.assertTrue(visit.consultation_started_at)

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
			out = apply_action(visit.name, "route", department=DEPT_OUTPATIENT, comment="Fever")
			self.assertEqual(out["current_department"], DEPT_OUTPATIENT)
		with self.as_user("clinician"), self.assertRaises(frappe.PermissionError):
			apply_action(visit.name, "prescribe")

	def test_available_actions_follow_role(self):
		visit = make_visit()
		with self.as_user("triage"):
			self.assertEqual([a["action"] for a in engine.get_available_actions(visit)], ["start_triage"])
		with self.as_user("clinician"):
			self.assertEqual(engine.get_available_actions(visit), [])
		visit = self.visit_in(VISIT_STATUS_IN_CONSULTATION)
		with self.as_user("clinician"):
			self.assertEqual(
				sorted(a["action"] for a in engine.get_available_actions(visit)), ["discharge", "refer"]
			)


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
		visit = make_visit(is_outreach=1)
		visit.outreach_session = "T-SESSION-1"
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
		with self.assertRaises(PharmacyTaskOpenError):
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
		level = frappe.db.get_value("Triage Level", {}, "name")
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
		if level:
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
	def test_department_workspaces(self):
		"""FR-8/BR-3: one workspace per department, restricted to its roles, plus the manager overview."""
		for dept in DEPARTMENTS:
			name = f"{dept} Queue"
			self.assertTrue(frappe.db.exists("Workspace", name), name)
			ws = frappe.get_doc("Workspace", name)
			self.assertTrue(ws.roles, name)
			self.assertTrue(ws.shortcuts, name)
		overview = frappe.get_doc("Workspace", "Visit Overview")
		self.assertTrue(overview.number_cards)
		self.assertTrue(frappe.db.exists("Number Card", "Longest Current Wait"))
