# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 4 O1: catchment areas, outreach sessions (rules, lifecycle, row access), devices and grants, and the
device auth hook (OUT-02, OUT-03, OUT-07, OUT-08, NFR-8, DR-3, ROL-22, D58; protocol §1-2)."""

from __future__ import annotations

import base64
import os
from contextlib import contextmanager
from types import SimpleNamespace

import frappe
from frappe.utils import add_days, add_to_date, get_datetime, now_datetime, random_string, today

from onerc_healthcare.constants import (
	CATCHMENT_AREA_DOCTYPE,
	DEVICE_ACTIVE,
	DEVICE_REVOKED,
	OUTREACH_AUTH_SCHEME,
	OUTREACH_DEVICE_DOCTYPE,
	OUTREACH_GRANT_DOCTYPE,
	OUTREACH_RECORD_DOCTYPE,
	OUTREACH_SESSION_DOCTYPE,
	ROLE_OUTREACH,
	ROLE_SYSADMIN,
	SESSION_ACTIVE,
	SESSION_CANCELLED,
	SESSION_CLOSED,
	SESSION_PLANNED,
	SESSION_RETURNED,
)
from onerc_healthcare.emr_outreach.doctype.emr_outreach_settings.emr_outreach_settings import get_settings
from onerc_healthcare.outreach import auth, devices, permissions
from onerc_healthcare.outreach import session as sessions
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import get_facility, make_user
from onerc_healthcare.tests.outreach_factories import (
	make_catchment,
	make_residence,
	make_session,
	outreach_users,
)

SYNC_PUSH = "/api/method/onerc_healthcare.outreach.sync.push"


@contextmanager
def as_user(user: str):
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user("Administrator")


@contextmanager
def fake_request(header: str | None, path: str = SYNC_PUSH, cmd: str | None = None, method: str = "POST"):
	"""A minimal stand-in for the werkzeug request the auth hook reads (headers, path), as Guest."""
	saved_request = getattr(frappe.local, "request", None)
	saved_form = getattr(frappe.local, "form_dict", None)
	headers = {"Authorization": header} if header is not None else {}
	frappe.local.request = SimpleNamespace(headers=headers, path=path, method=method)
	frappe.set_user("Guest")
	frappe.local.form_dict = frappe._dict({"cmd": cmd} if cmd else {})
	try:
		yield
	finally:
		frappe.local.request = saved_request
		frappe.local.form_dict = saved_form if saved_form is not None else frappe._dict()
		setattr(frappe.local, auth.GRANT_LOCAL_KEY, None)
		frappe.set_user("Administrator")


def new_key() -> str:
	return base64.b64encode(os.urandom(32)).decode()


def new_device_id() -> str:
	return f"t-dev-{random_string(12).lower()}"


class OutreachO1Case(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.users = outreach_users()
		self.area, self.leaf = make_catchment()
		self.session = make_session(
			members=[self.users["member"], self.users["member2"]], catchment_area=self.area
		)

	def enrol(
		self, user: str | None = None, device_id: str | None = None, session: str | None = None, key=None
	):
		with as_user(user or self.users["member"]):
			return devices.enrol(
				session or self.session.name, device_id or new_device_id(), key or new_key(), "T- tablet"
			)


class TestCatchmentArea(OutreachO1Case):
	"""OUT-02."""

	def new_area(self, locations, facility=None):
		return frappe.get_doc(
			{
				"doctype": CATCHMENT_AREA_DOCTYPE,
				"area_name": f"T-Area-{random_string(6)}",
				"facility": facility or get_facility(),
				"locations": [{"residence_location": loc} for loc in locations],
			}
		)

	def test_redundant_location_refused(self):
		top = make_residence()
		child = make_residence(parent=top)
		with self.assertRaisesRegex(frappe.ValidationError, "already inside"):
			self.new_area([top, child]).insert(ignore_permissions=True)

	def test_duplicate_location_refused(self):
		top = make_residence()
		with self.assertRaisesRegex(frappe.ValidationError, "more than once"):
			self.new_area([top, top]).insert(ignore_permissions=True)

	def test_siblings_accepted(self):
		a, b = make_residence(), make_residence()
		doc = self.new_area([a, b]).insert(ignore_permissions=True)
		self.assertEqual(len(doc.locations), 2)

	def test_facility_must_be_operation_registration_facility(self):
		top = make_residence()
		root_hsu = frappe.db.get_value(
			"Healthcare Service Unit", {"is_group": 1, "krcs_facility_code": ["is", "not set"]}
		)
		with self.assertRaisesRegex(frappe.ValidationError, "not a facility of this operation"):
			self.new_area([top], facility=root_hsu).insert(ignore_permissions=True)


class TestSessionRules(OutreachO1Case):
	"""OUT-03 planning rules."""

	def test_defaults_from_catchment_and_log(self):
		s = self.session
		self.assertEqual(s.status, SESSION_PLANNED)
		self.assertEqual(s.facility, frappe.db.get_value(CATCHMENT_AREA_DOCTYPE, self.area, "facility"))
		self.assertEqual(s.company, frappe.db.get_value("Healthcare Service Unit", s.facility, "company"))
		self.assertEqual([(r.action, r.to_status) for r in s.status_log], [("create", SESSION_PLANNED)])
		self.assertEqual([r.is_lead for r in s.team], [1, 0])

	def test_status_given_on_insert_is_ignored(self):
		s = make_session(members=[self.users["member"]], catchment_area=self.area, status=SESSION_CLOSED)
		self.assertEqual(s.status, SESSION_PLANNED)

	def test_facility_must_match_catchment(self):
		with self.assertRaisesRegex(frappe.ValidationError, "catchment area's facility"):
			make_session(members=[self.users["member"]], catchment_area=self.area, facility=get_facility(1))

	def test_end_before_start_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "end date"):
			make_session(members=[self.users["member"]], catchment_area=self.area, start_date=today(), days=0)

	def test_team_rules(self):
		with self.assertRaisesRegex(frappe.ValidationError, "more than once"):
			make_session(members=[self.users["member"], self.users["member"]], catchment_area=self.area)
		with self.assertRaisesRegex(frappe.ValidationError, "Outreach Team Member"):
			make_session(members=[self.users["his"]], catchment_area=self.area)
		# other roles alongside the outreach role are fine
		both = make_user("or-both", [ROLE_OUTREACH, "Clinical Officer"])
		self.assertEqual(make_session(members=[both], catchment_area=self.area).team[0].user, both)

	def test_department_rules(self):
		for dept in ("Mental Health", "Reception", "Triage", "Laboratory", "Pharmacy", "Health Information"):
			with self.assertRaisesRegex(frappe.ValidationError, "cannot be offered"):
				make_session(
					members=[self.users["member"]], catchment_area=self.area, departments=("Outpatient", dept)
				)
		with self.assertRaisesRegex(frappe.ValidationError, "at least one department"):
			make_session(members=[self.users["member"]], catchment_area=self.area, departments=())
		s = make_session(
			members=[self.users["member"]],
			catchment_area=self.area,
			departments=("Outpatient", "Maternal and Child Health"),
		)
		self.assertEqual(sessions.session_departments(s.name), ["Outpatient", "Maternal and Child Health"])

	def test_status_and_log_not_editable_by_hand(self):
		doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, self.session.name)
		doc.status = SESSION_CLOSED
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)
		doc.reload()
		doc.append(
			"status_log",
			{"to_status": "Closed", "action": "x", "user": "Administrator", "timestamp": now_datetime()},
		)
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	def test_plan_locked_after_return(self):
		sessions.transition(self.session.name, sessions.ACTION_ACTIVATE, system=True)
		doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, self.session.name)
		doc.end_date = add_days(doc.end_date, 1)  # Active: dates still editable
		doc.save(ignore_permissions=True)
		doc.catchment_area = make_catchment()[0]
		with self.assertRaisesRegex(frappe.ValidationError, "cannot change"):
			doc.save(ignore_permissions=True)
		with as_user(self.users["his"]):
			sessions.apply_action(self.session.name, sessions.ACTION_RETURN)
		doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, self.session.name)
		doc.append("team", {"user": self.users["outsider"]})
		with self.assertRaisesRegex(frappe.ValidationError, "cannot change"):
			doc.save(ignore_permissions=True)
		doc.reload()
		doc.notes = "T- back on Friday"
		doc.save(ignore_permissions=True)  # notes stay editable

	def test_disabled_catchment_refused_for_new_session(self):
		frappe.db.set_value(CATCHMENT_AREA_DOCTYPE, self.area, "disabled", 1)
		with self.assertRaisesRegex(frappe.ValidationError, "disabled"):
			make_session(members=[self.users["member"]], catchment_area=self.area)


class TestSessionLifecycle(OutreachO1Case):
	"""OUT-03 lifecycle by role: allowed and denied (TRANSITIONS)."""

	def active(self):
		sessions.transition(self.session.name, sessions.ACTION_ACTIVATE, system=True)

	def status(self):
		return frappe.db.get_value(OUTREACH_SESSION_DOCTYPE, self.session.name, "status")

	def test_transitions_table(self):
		self.assertEqual(
			{a: (r.from_statuses, r.to_status) for a, r in sessions.TRANSITIONS.items()},
			{
				"activate": ((SESSION_PLANNED,), SESSION_ACTIVE),
				"return": ((SESSION_ACTIVE,), SESSION_RETURNED),
				"close": ((SESSION_RETURNED,), SESSION_CLOSED),
				"cancel": ((SESSION_PLANNED,), SESSION_CANCELLED),
			},
		)
		self.assertTrue(sessions.TRANSITIONS["activate"].is_system)

	def test_activate_is_system_only(self):
		for key in ("his", "mic", "member"):
			with as_user(self.users[key]), self.assertRaises(frappe.ValidationError):
				sessions.apply_action(self.session.name, sessions.ACTION_ACTIVATE)
		self.assertTrue(sessions.activate_on_download(self.session.name))
		self.assertFalse(sessions.activate_on_download(self.session.name))
		self.assertEqual(self.status(), SESSION_ACTIVE)

	def test_return_by_role(self):
		self.active()
		for key in ("member2", "outsider", "reception", "clinician"):
			with as_user(self.users[key]), self.assertRaises(frappe.PermissionError):
				sessions.apply_action(self.session.name, sessions.ACTION_RETURN)
		with as_user(self.users["member"]):  # the team lead
			self.assertEqual(
				sessions.apply_action(self.session.name, sessions.ACTION_RETURN)["status"], SESSION_RETURNED
			)

	def test_return_by_managers(self):
		for key in ("his", "mic"):
			s = make_session(members=[self.users["member"]], catchment_area=self.area)
			sessions.transition(s.name, sessions.ACTION_ACTIVATE, system=True)
			with as_user(self.users[key]):
				self.assertEqual(
					sessions.apply_action(s.name, sessions.ACTION_RETURN)["status"], SESSION_RETURNED
				)

	def test_return_from_planned_refused(self):
		with as_user(self.users["his"]), self.assertRaises(sessions.IllegalSessionTransitionError):
			sessions.apply_action(self.session.name, sessions.ACTION_RETURN)

	def test_close_by_role(self):
		self.active()
		sessions.transition(self.session.name, sessions.ACTION_RETURN, system=True)
		for key in ("member", "outsider", "reception"):
			with as_user(self.users[key]), self.assertRaises(frappe.PermissionError):
				sessions.apply_action(self.session.name, sessions.ACTION_CLOSE)
		with as_user(self.users["mic"]):
			sessions.apply_action(self.session.name, sessions.ACTION_CLOSE)
		self.assertEqual(self.status(), SESSION_CLOSED)
		log = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, self.session.name).status_log
		self.assertEqual([r.action for r in log], ["create", "activate", "return", "close"])
		self.assertEqual(log[-1].user, self.users["mic"])

	def test_close_refused_while_sync_records_unresolved(self):
		if not frappe.db.table_exists(OUTREACH_RECORD_DOCTYPE):
			self.skipTest("Outreach Sync Record (O2) not installed")
		self.active()
		sessions.transition(self.session.name, sessions.ACTION_RETURN, system=True)
		uuid = f"t-{random_string(20)}"
		frappe.db.sql(
			f"""insert into `tab{OUTREACH_RECORD_DOCTYPE}`
			(name, client_uuid, record_type, status, session, user, creation, modified, owner, modified_by)
			values (%s, %s, 'visit.capture', 'Held', %s, %s, now(), now(), 'Administrator', 'Administrator')""",
			(uuid, uuid, self.session.name, self.users["member"]),
		)
		with (
			as_user(self.users["his"]),
			self.assertRaisesRegex(frappe.ValidationError, "held or in conflict"),
		):
			sessions.apply_action(self.session.name, sessions.ACTION_CLOSE)
		frappe.db.set_value(OUTREACH_RECORD_DOCTYPE, uuid, "status", "Accepted", update_modified=False)
		with as_user(self.users["his"]):
			sessions.apply_action(self.session.name, sessions.ACTION_CLOSE)
		self.assertEqual(self.status(), SESSION_CLOSED)

	def test_cancel_needs_reason_and_manager(self):
		for key in ("member", "outsider", "reception"):
			with as_user(self.users[key]), self.assertRaises(frappe.PermissionError):
				sessions.apply_action(self.session.name, sessions.ACTION_CANCEL, "T- rain")
		with as_user(self.users["his"]), self.assertRaises(sessions.IllegalSessionTransitionError):
			sessions.apply_action(self.session.name, sessions.ACTION_CANCEL, "  ")
		with as_user(self.users["his"]):
			sessions.apply_action(self.session.name, sessions.ACTION_CANCEL, "T- rain")
		doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, self.session.name)
		self.assertEqual((doc.status, doc.cancellation_reason), (SESSION_CANCELLED, "T- rain"))
		with as_user(self.users["his"]), self.assertRaises(sessions.IllegalSessionTransitionError):
			sessions.apply_action(self.session.name, sessions.ACTION_CANCEL, "again")

	def test_cancel_after_activation_refused(self):
		self.active()
		with as_user(self.users["mic"]), self.assertRaises(sessions.IllegalSessionTransitionError):
			sessions.apply_action(self.session.name, sessions.ACTION_CANCEL, "T- too late")

	def test_allowed_actions_offered(self):
		doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, self.session.name)
		self.assertEqual([a["action"] for a in sessions.allowed_actions(doc, self.users["his"])], ["cancel"])
		self.assertEqual(sessions.allowed_actions(doc, self.users["member"]), [])
		self.active()
		doc.reload()
		self.assertEqual(
			[a["action"] for a in sessions.allowed_actions(doc, self.users["member"])], ["return"]
		)
		self.assertEqual(sessions.allowed_actions(doc, self.users["member2"]), [])


class TestSessionPermissions(OutreachO1Case):
	"""ROL-22: managers see every session, a team member only theirs, others none."""

	def visible(self, user):
		with as_user(user):
			return set(frappe.get_list(OUTREACH_SESSION_DOCTYPE, pluck="name"))

	def test_row_filtering(self):
		other = make_session(members=[self.users["outsider"]], catchment_area=self.area)
		mine = {self.session.name}
		self.assertTrue(mine <= self.visible(self.users["member"]))
		self.assertNotIn(other.name, self.visible(self.users["member"]))
		self.assertNotIn(self.session.name, self.visible(self.users["outsider"]))
		self.assertIn(other.name, self.visible(self.users["outsider"]))
		for key in ("his", "mic"):
			self.assertTrue({self.session.name, other.name} <= self.visible(self.users[key]))
		with as_user(self.users["reception"]), self.assertRaises(frappe.PermissionError):
			frappe.get_list(OUTREACH_SESSION_DOCTYPE)
		self.assertEqual(permissions.session_query(self.users["reception"]), "1=0")

	def test_has_permission(self):
		doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, self.session.name)
		check = permissions.session_has_permission
		self.assertTrue(check(doc, "read", self.users["member"]))
		self.assertFalse(check(doc, "write", self.users["member"]))
		self.assertFalse(check(doc, "read", self.users["outsider"]))
		self.assertFalse(check(doc, "read", self.users["reception"]))
		self.assertTrue(check(doc, "write", self.users["his"]))
		self.assertTrue(check(doc, "write", self.users["mic"]))
		self.assertTrue(
			frappe.has_permission(OUTREACH_SESSION_DOCTYPE, "read", doc=doc, user=self.users["member"])
		)
		self.assertFalse(
			frappe.has_permission(OUTREACH_SESSION_DOCTYPE, "read", doc=doc, user=self.users["outsider"])
		)
		self.assertFalse(
			frappe.has_permission(OUTREACH_SESSION_DOCTYPE, "write", doc=doc, user=self.users["member"])
		)


class TestDevices(OutreachO1Case):
	"""OUT-07/08: enrolment, secret rotation, expiry, revocation, key escrow."""

	def test_enrol_returns_protocol_shape_and_stores_hash_only(self):
		device_id = new_device_id()
		key = new_key()
		res = self.enrol(device_id=device_id, key=key)
		self.assertEqual(set(res), {"grant", "secret", "expires_at", "user", "settings"})
		self.assertEqual(res["user"]["name"], self.users["member"])
		self.assertEqual(
			set(res["settings"]),
			{
				"offline_session_days",
				"purge_after_days",
				"pin_min_length",
				"max_pin_attempts",
				"max_batch",
				"summary_visits",
			},
		)
		grant = frappe.get_doc(OUTREACH_GRANT_DOCTYPE, res["grant"])
		self.assertNotIn(res["secret"], (grant.secret_hash, grant.secret_salt))
		self.assertEqual(grant.secret_hash, devices.hash_secret(res["secret"], grant.secret_salt))
		self.assertTrue(set(grant.data_key) == {"*"})  # encrypted at rest; the column holds a mask
		expected = add_to_date(now_datetime(), days=get_settings().offline_session_days)
		self.assertLess(abs((get_datetime(res["expires_at"]) - expected).total_seconds()), 5)
		dev = frappe.get_doc(OUTREACH_DEVICE_DOCTYPE, device_id)
		self.assertEqual(
			(dev.status, dev.first_user, dev.label), (DEVICE_ACTIVE, self.users["member"], "T- tablet")
		)
		self.assertEqual(devices.verify_grant(res["grant"], res["secret"]).user, self.users["member"])

	def test_reenrol_rotates_secret_same_grant(self):
		device_id = new_device_id()
		first = self.enrol(device_id=device_id)
		second = self.enrol(device_id=device_id)
		self.assertEqual(first["grant"], second["grant"])
		self.assertNotEqual(first["secret"], second["secret"])
		with self.assertRaises(frappe.AuthenticationError):
			devices.verify_grant(first["grant"], first["secret"])
		devices.verify_grant(second["grant"], second["secret"])
		# one grant per user per device per session; a second user gets their own
		other = self.enrol(user=self.users["member2"], device_id=device_id)
		self.assertNotEqual(other["grant"], first["grant"])
		self.assertEqual(
			frappe.db.get_value(OUTREACH_DEVICE_DOCTYPE, device_id, "first_user"), self.users["member"]
		)

	def test_enrol_denied(self):
		for key in ("outsider", "his", "reception"):
			with self.assertRaises(frappe.PermissionError):
				self.enrol(user=self.users[key])
		with as_user("Guest"), self.assertRaises(frappe.AuthenticationError):
			devices.enrol(self.session.name, new_device_id(), new_key())
		with self.assertRaises(devices.DeviceEnrolmentError):
			self.enrol(key=base64.b64encode(b"short").decode())
		with self.assertRaises(devices.DeviceEnrolmentError):
			self.enrol(device_id="bad id!")

	def test_enrol_refused_with_device_grant(self):
		res = self.enrol()
		setattr(frappe.local, auth.GRANT_LOCAL_KEY, res["grant"])
		try:
			with self.assertRaisesRegex(frappe.PermissionError, "online sign-in"):
				self.enrol()
			with as_user(self.users["member"]), self.assertRaises(frappe.PermissionError):
				devices.recover_key(res["grant"])
		finally:
			setattr(frappe.local, auth.GRANT_LOCAL_KEY, None)

	def test_assert_request_grant(self):
		device_id = new_device_id()
		res = self.enrol(device_id=device_id)
		devices.assert_request_grant("anything", "anything")  # cookie request: no grant, no opinion
		setattr(frappe.local, auth.GRANT_LOCAL_KEY, res["grant"])
		try:
			devices.assert_request_grant(self.session.name, device_id)
			with self.assertRaisesRegex(frappe.PermissionError, "another outreach session"):
				devices.assert_request_grant("KLB-OUT-0000-00000", device_id)
			with self.assertRaisesRegex(frappe.PermissionError, "another device"):
				devices.assert_request_grant(self.session.name, new_device_id())
		finally:
			setattr(frappe.local, auth.GRANT_LOCAL_KEY, None)

	def test_enrol_refused_for_closed_session(self):
		sessions.transition(self.session.name, sessions.ACTION_CANCEL, reason="T-", system=True)
		with self.assertRaises(frappe.ValidationError):
			self.enrol()

	def test_expiry(self):
		res = self.enrol()
		frappe.db.set_value(
			OUTREACH_GRANT_DOCTYPE, res["grant"], "expires_at", add_to_date(now_datetime(), minutes=-1)
		)
		with self.assertRaisesRegex(frappe.AuthenticationError, "expired"):
			devices.verify_grant(res["grant"], res["secret"])
		again = self.enrol(device_id=frappe.db.get_value(OUTREACH_GRANT_DOCTYPE, res["grant"], "device"))
		self.assertEqual(again["grant"], res["grant"])
		devices.verify_grant(again["grant"], again["secret"])

	def test_revoke_device_by_role(self):
		device_id = new_device_id()
		a = self.enrol(device_id=device_id)
		b = self.enrol(user=self.users["member2"], device_id=device_id)
		for key in ("member", "outsider", "reception", "clinician"):
			with as_user(self.users[key]), self.assertRaises(frappe.PermissionError):
				devices.revoke_device(device_id, "T- lost")
		with as_user(self.users["his"]), self.assertRaises(frappe.ValidationError):
			devices.revoke_device(device_id, " ")
		with as_user(self.users["his"]):
			res = devices.revoke_device(device_id, "T- lost")
		self.assertEqual((res["status"], res["grants_revoked"]), (DEVICE_REVOKED, 2))
		for g in (a, b):
			with self.assertRaisesRegex(frappe.AuthenticationError, "revoked"):
				devices.verify_grant(g["grant"], g["secret"])
			row = frappe.db.get_value(
				OUTREACH_GRANT_DOCTYPE, g["grant"], ["revoked", "revoked_by", "revoke_reason"], as_dict=True
			)
			self.assertEqual(
				(row.revoked, row.revoked_by, row.revoke_reason), (1, self.users["his"], "T- lost")
			)
		with self.assertRaisesRegex(frappe.PermissionError, "revoked"):
			self.enrol(device_id=device_id)

	def test_revoke_allowed_for_mic_and_sysadmin(self):
		sysadmin = make_user("or-sysadmin", [ROLE_SYSADMIN])
		for user in (self.users["mic"], sysadmin):
			device_id = new_device_id()
			self.enrol(device_id=device_id)
			with as_user(user):
				self.assertEqual(devices.revoke_device(device_id, "T- retired")["status"], DEVICE_REVOKED)

	def test_recover_key(self):
		key = new_key()
		res = self.enrol(key=key)
		with as_user(self.users["member"]):
			self.assertEqual(devices.recover_key(res["grant"]), {"data_key": key})
		for key_user in ("member2", "his", "outsider"):
			with as_user(self.users[key_user]), self.assertRaises(frappe.DoesNotExistError):
				devices.recover_key(res["grant"])
		self.assertTrue(
			frappe.db.exists(
				"Comment", {"reference_doctype": OUTREACH_GRANT_DOCTYPE, "reference_name": res["grant"]}
			)
		)
		devices.revoke_grants({"name": res["grant"]}, "T- test")
		with as_user(self.users["member"]), self.assertRaises(frappe.PermissionError):
			devices.recover_key(res["grant"])

	def test_session_and_membership_end_grant(self):
		res = self.enrol(user=self.users["member2"])
		doc = frappe.get_doc(OUTREACH_SESSION_DOCTYPE, self.session.name)
		doc.set("team", [r for r in doc.team if r.user != self.users["member2"]])
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(OUTREACH_GRANT_DOCTYPE, res["grant"], "revoked"), 1)
		res = self.enrol()
		sessions.transition(self.session.name, sessions.ACTION_CANCEL, reason="T-", system=True)
		with self.assertRaisesRegex(frappe.AuthenticationError, "closed"):
			devices.verify_grant(res["grant"], res["secret"])

	def test_device_and_grant_not_editable_by_hand(self):
		res = self.enrol()
		grant = frappe.get_doc(OUTREACH_GRANT_DOCTYPE, res["grant"])
		grant.expires_at = add_days(now_datetime(), 300)
		with self.assertRaises(frappe.PermissionError):
			grant.save(ignore_permissions=True)
		grant.reload()
		grant.data_key = new_key()
		with self.assertRaises(frappe.PermissionError):
			grant.save(ignore_permissions=True)
		grant.reload()
		grant.save(ignore_permissions=True)  # an unchanged save is fine
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{"doctype": OUTREACH_DEVICE_DOCTYPE, "device_id": new_device_id(), "label": "x"}
			).insert(ignore_permissions=True)

	def test_my_sessions(self):
		other = make_session(members=[self.users["outsider"]], catchment_area=self.area)
		cancelled = make_session(members=[self.users["member"]], catchment_area=self.area)
		sessions.transition(cancelled.name, sessions.ACTION_CANCEL, reason="T-", system=True)
		with as_user(self.users["member"]):
			rows = devices.my_sessions()
		names = [r["name"] for r in rows]
		self.assertIn(self.session.name, names)
		self.assertNotIn(other.name, names)
		self.assertNotIn(cancelled.name, names)
		row = rows[names.index(self.session.name)]
		self.assertEqual(row["departments"], ["Outpatient"])
		self.assertEqual(row["status"], SESSION_PLANNED)
		self.assertTrue(row["facility_name"])


class TestAuthHook(OutreachO1Case):
	"""protocol §1: the device grant authenticates only the four sync / package endpoints."""

	def header(self, res, secret=None):
		return f"{OUTREACH_AUTH_SCHEME} {res['grant']}:{secret or res['secret']}"

	def test_no_op_without_scheme(self):
		with fake_request(None):
			auth.authenticate_device()
			self.assertEqual(frappe.session.user, "Guest")
		with fake_request("Bearer abc"):
			auth.authenticate_device()
			self.assertEqual(frappe.session.user, "Guest")
		with fake_request("token key:secret"):
			auth.authenticate_device()
			self.assertEqual(frappe.session.user, "Guest")

	def test_accepts_the_four_endpoints(self):
		res = self.enrol()
		paths = [
			"/api/method/onerc_healthcare.outreach.sync.push",
			"/api/method/onerc_healthcare.outreach.sync.status",
			"/api/method/onerc_healthcare.outreach.package.get_reference",
			"/api/method/onerc_healthcare.outreach.package.get_patients",
			"/api/v2/method/onerc_healthcare.outreach.package.get_patients",
		]
		for path in paths:
			with fake_request(self.header(res), path=path):
				auth.authenticate_device()
				self.assertEqual(frappe.session.user, self.users["member"])
				self.assertEqual(devices.grant_from_request(), res["grant"])
		self.assertTrue(frappe.db.get_value(OUTREACH_GRANT_DOCTYPE, res["grant"], "last_used_at"))

	def test_refuses_other_endpoints(self):
		res = self.enrol()
		cases = [
			("/api/method/frappe.client.get_list", None),
			("/api/resource/Patient", None),
			("/api/v2/document/Patient", None),
			("/api/method/onerc_healthcare.outreach.devices.enrol", None),
			("/api/method/onerc_healthcare.outreach.devices.recover_key", None),
			("/app/patient", None),
			("/outreach", None),
			(SYNC_PUSH, "frappe.client.get_list"),  # cmd runs before the path
			("/", "frappe.client.get_list"),
		]
		for path, cmd in cases:
			with fake_request(self.header(res), path=path, cmd=cmd):
				with self.assertRaises(frappe.AuthenticationError, msg=f"{path} {cmd}"):
					auth.authenticate_device()
				self.assertEqual(frappe.session.user, "Guest")
				self.assertIsNone(devices.grant_from_request())
		with fake_request(self.header(res), path="/", cmd="onerc_healthcare.outreach.sync.status"):
			auth.authenticate_device()
			self.assertEqual(frappe.session.user, self.users["member"])

	def test_refuses_bad_expired_revoked(self):
		res = self.enrol()
		for header in (
			self.header(res, secret="wrong"),
			f"{OUTREACH_AUTH_SCHEME} nosuchgrant:{res['secret']}",
			f"{OUTREACH_AUTH_SCHEME} {res['grant']}",
			f"{OUTREACH_AUTH_SCHEME} ",
		):
			with fake_request(header), self.assertRaises(frappe.AuthenticationError):
				auth.authenticate_device()
		frappe.db.set_value(
			OUTREACH_GRANT_DOCTYPE, res["grant"], "expires_at", add_to_date(now_datetime(), seconds=-1)
		)
		with fake_request(self.header(res)), self.assertRaisesRegex(frappe.AuthenticationError, "expired"):
			auth.authenticate_device()
		res = self.enrol(device_id=frappe.db.get_value(OUTREACH_GRANT_DOCTYPE, res["grant"], "device"))
		with fake_request(self.header(res)):
			auth.authenticate_device()
		with as_user(self.users["his"]):
			devices.revoke_device(
				frappe.db.get_value(OUTREACH_GRANT_DOCTYPE, res["grant"], "device"), "T- lost"
			)
		with fake_request(self.header(res)), self.assertRaisesRegex(frappe.AuthenticationError, "revoked"):
			auth.authenticate_device()

	def test_refuses_grant_of_disabled_user(self):
		res = self.enrol(user=self.users["member2"])
		frappe.db.set_value("User", self.users["member2"], "enabled", 0)
		try:
			with fake_request(self.header(res)), self.assertRaises(frappe.AuthenticationError):
				auth.authenticate_device()
		finally:
			frappe.db.set_value("User", self.users["member2"], "enabled", 1)

	def test_cookie_user_must_match(self):
		res = self.enrol()
		with fake_request(self.header(res)):
			frappe.set_user(self.users["member2"])
			with self.assertRaises(frappe.AuthenticationError):
				auth.authenticate_device()
		with fake_request(self.header(res)):
			frappe.set_user(self.users["member"])
			auth.authenticate_device()
			self.assertEqual(devices.grant_from_request(), res["grant"])

	def test_form_dict_kept(self):
		res = self.enrol()
		with fake_request(
			self.header(res), path="/api/method/onerc_healthcare.outreach.package.get_reference"
		):
			frappe.local.form_dict.session = self.session.name
			auth.authenticate_device()
			self.assertEqual(frappe.local.form_dict.session, self.session.name)

	def test_closed_session_grant_syncs_only(self):
		"""Protocol §6 F1: once the session is Closed the grant still authenticates sync.push and sync.status
		(also through `cmd`), never the package endpoints; a Cancelled session's grant authenticates nothing."""
		res = self.enrol()
		for action in (sessions.ACTION_ACTIVATE, sessions.ACTION_RETURN, sessions.ACTION_CLOSE):
			sessions.transition(self.session.name, action, system=True)
		for path in (SYNC_PUSH, "/api/method/onerc_healthcare.outreach.sync.status"):
			with fake_request(self.header(res), path=path):
				auth.authenticate_device()
				self.assertEqual(frappe.session.user, self.users["member"])
		for path in (
			"/api/method/onerc_healthcare.outreach.package.get_reference",
			"/api/method/onerc_healthcare.outreach.package.get_patients",
		):
			with (
				fake_request(self.header(res), path=path),
				self.assertRaisesRegex(frappe.AuthenticationError, "is Closed"),
			):
				auth.authenticate_device()
		with fake_request(self.header(res), path="/", cmd="onerc_healthcare.outreach.package.get_patients"):
			with self.assertRaises(frappe.AuthenticationError):
				auth.authenticate_device()
		with self.assertRaises(frappe.AuthenticationError):  # no method named: open sessions only
			devices.verify_grant(res["grant"], res["secret"])

		other = make_session(members=[self.users["member"]], catchment_area=self.area)
		res2 = self.enrol(session=other.name)
		sessions.transition(other.name, sessions.ACTION_CANCEL, reason="T-", system=True)
		with (
			fake_request(self.header(res2)),
			self.assertRaisesRegex(frappe.AuthenticationError, "is Cancelled"),
		):
			auth.authenticate_device()

	def test_enrol_on_closed_session_only_renews(self):
		"""Protocol §6 F1: on a Closed session `enrol` renews the live grant of the same user and device (same
		grant, new secret, new expiry), and refuses a new device, another user's first enrolment of that device
		and a revoked grant. A Cancelled session refuses every enrolment."""
		device_id = new_device_id()
		res = self.enrol(device_id=device_id)
		for action in (sessions.ACTION_ACTIVATE, sessions.ACTION_RETURN, sessions.ACTION_CLOSE):
			sessions.transition(self.session.name, action, system=True)
		frappe.db.set_value(
			OUTREACH_GRANT_DOCTYPE, res["grant"], "expires_at", add_to_date(now_datetime(), seconds=-1)
		)
		again = self.enrol(device_id=device_id)
		self.assertEqual(again["grant"], res["grant"])
		self.assertNotEqual(again["secret"], res["secret"])
		self.assertGreater(get_datetime(again["expires_at"]), now_datetime())
		with fake_request(self.header(again)):
			auth.authenticate_device()
		for kwargs in (
			{"device_id": new_device_id()},
			{"device_id": device_id, "user": self.users["member2"]},
		):
			with (
				self.subTest(**kwargs),
				self.assertRaisesRegex(sessions.OutreachSessionClosedError, "is Closed"),
			):
				self.enrol(**kwargs)
		devices.revoke_grants({"name": res["grant"]}, "T- test")
		with self.assertRaisesRegex(sessions.OutreachSessionClosedError, "is Closed"):
			self.enrol(device_id=device_id)
		other = make_session(members=[self.users["member"]], catchment_area=self.area)
		sessions.transition(other.name, sessions.ACTION_CANCEL, reason="T-", system=True)
		with self.assertRaisesRegex(sessions.OutreachSessionClosedError, "is Cancelled"):
			self.enrol(session=other.name)

	def test_role_withdrawn_refuses_grant(self):
		"""Protocol §6 F6: a member whose Outreach Team Member role was withdrawn (still enabled and on the team)
		gets 401 for the grant; given the role back, the grant works again."""
		res = self.enrol(user=self.users["member2"])
		make_user("or-member2", [])
		with fake_request(self.header(res)), self.assertRaisesRegex(frappe.AuthenticationError, "role"):
			auth.authenticate_device()
		make_user("or-member2", [ROLE_OUTREACH])
		with fake_request(self.header(res)):
			auth.authenticate_device()
			self.assertEqual(frappe.session.user, self.users["member2"])
