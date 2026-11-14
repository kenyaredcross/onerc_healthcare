# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""R3 Service Delivered and Community Activity Record (SDD §5.12.7, gap table; MOH 711 / 717 sources).

Every lifecycle transition is reached, allowed and denied, by role. Nobody deletes (NFR-9).
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, today

from onerc_healthcare.community import activities, services
from onerc_healthcare.community import setup as community_setup
from onerc_healthcare.constants import (
	COMMUNITY_ACTIVITY_DOCTYPE,
	COMMUNITY_ACTIVITY_TYPE_DOCTYPE,
	ROLE_CLINICIAN,
	ROLE_OUTREACH,
	SERVICE_DELIVERED_DOCTYPE,
	SERVICE_TYPE_DOCTYPE,
	VISIT_DOCTYPE,
)
from onerc_healthcare.emr_community.doctype.service_delivered.service_delivered import (
	ServiceDeliveredLockedError,
)
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import as_user, users
from onerc_healthcare.tests.factories import get_facility, make_user
from onerc_healthcare.tests.visit_factories import make_visit
from onerc_healthcare.visit import engine

SERVICE = "Psycho-social counselling"
ACTIVITY = "Health Talk"
ALL_KEYS = ("reception", "triage", "clinician", "mho", "lab", "pharmacist", "his", "mic", "hoo", "sysadmin")


class CommunityTestCase(EMRTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users = users()
		cls.users["clinician2"] = make_user("cap-clinician2", [ROLE_CLINICIAN])
		cls.users["outreach"] = make_user("cap-outreach", [ROLE_OUTREACH])
		frappe.set_user("Administrator")
		community_setup.install()


class TestMasters(CommunityTestCase):
	def test_seeded_lines(self):
		"""The nine MOH 711 social work lines and four activity types, marked provisional."""
		for name, _category, _line in community_setup.SERVICE_TYPES:
			row = frappe.db.get_value(
				SERVICE_TYPE_DOCTYPE, name, ["enabled", "moh_line", "category"], as_dict=True
			)
			self.assertTrue(row, name)
		self.assertEqual(len(community_setup.SERVICE_TYPES), 9)
		for name, category, _line in community_setup.ACTIVITY_TYPES:
			self.assertEqual(frappe.db.get_value(COMMUNITY_ACTIVITY_TYPE_DOCTYPE, name, "category"), category)
		self.assertEqual(
			frappe.db.get_value(SERVICE_TYPE_DOCTYPE, SERVICE, "note"), community_setup.PROVISIONAL_NOTE
		)

	def test_seed_never_overwrites(self):
		frappe.db.set_value(SERVICE_TYPE_DOCTYPE, SERVICE, {"moh_line": "T-Edited", "enabled": 0})
		community_setup.install()
		row = frappe.db.get_value(SERVICE_TYPE_DOCTYPE, SERVICE, ["moh_line", "enabled"], as_dict=True)
		self.assertEqual((row.moh_line, row.enabled), ("T-Edited", 0))

	def test_master_permissions(self):
		for doctype in (SERVICE_TYPE_DOCTYPE, COMMUNITY_ACTIVITY_TYPE_DOCTYPE):
			self.assertTrue(frappe.has_permission(doctype, "write", user=self.users["his"]))
			self.assertTrue(frappe.has_permission(doctype, "create", user=self.users["mic"]))
			self.assertTrue(frappe.has_permission(doctype, "read", user=self.users["clinician"]))
			self.assertFalse(frappe.has_permission(doctype, "write", user=self.users["clinician"]))
			self.assertFalse(any(p.get("delete") for p in frappe.get_meta(doctype).permissions))


# --------------------------------------------------------------------------- Service Delivered


class TestServiceDelivered(CommunityTestCase):
	def record(self, user_key="clinician", visit=None, **kw):
		visit = visit or make_visit().name
		values = {"doctype": SERVICE_DELIVERED_DOCTYPE, "visit": visit, "service_type": SERVICE}
		values.update(kw)
		with as_user(self.users[user_key]):
			return frappe.get_doc(values).insert()

	def test_transition_table(self):
		self.assertEqual(set(services.TRANSITIONS), {services.ACTION_RECORD, services.ACTION_VOID})
		void = services.TRANSITIONS[services.ACTION_VOID]
		self.assertEqual(
			(void.from_statuses, void.to_status), ((services.STATUS_ACTIVE,), services.STATUS_VOIDED)
		)
		self.assertTrue(void.needs_reason)

	def test_record_takes_values_from_the_visit(self):
		visit = make_visit()
		other = make_visit()  # a real but wrong patient; and the root unit as a wrong facility
		root = frappe.db.get_value(
			"Healthcare Service Unit", {"is_group": 1, "parent_healthcare_service_unit": ["is", "not set"]}
		)
		doc = self.record(
			visit=visit.name,
			patient=other.patient,
			facility=root,
			provided_by="Administrator",
			status="Voided",
		)
		self.assertEqual((doc.patient, doc.facility), (visit.patient, visit.facility))
		self.assertEqual(doc.provided_by, self.users["clinician"])
		self.assertEqual(doc.status, services.STATUS_ACTIVE)
		self.assertIn("SVC", doc.name)

	def test_mho_records_on_a_closed_visit(self):
		visit = make_visit().name
		engine.transition(visit, "close_visit", closure_reason="Other", comment="T- x", system=True)
		self.assertEqual(self.record("mho", visit=visit).status, services.STATUS_ACTIVE)

	def test_who_may_record(self):
		visit = make_visit().name
		for key in ("reception", "triage", "lab", "pharmacist", "his", "mic", "hoo", "sysadmin", "outreach"):
			with self.assertRaises(frappe.PermissionError, msg=key):
				self.record(key, visit=visit)

	def test_record_refusals(self):
		visit = make_visit().name
		with self.assertRaises(services.ServiceDeliveredError):
			self.record(visit=visit, service_date=add_days(today(), 1))
		with self.assertRaises(services.ServiceDeliveredError):
			self.record(visit=visit, service_date=add_days(today(), -1))  # before the visit date
		frappe.db.set_value(SERVICE_TYPE_DOCTYPE, SERVICE, "enabled", 0)
		with self.assertRaises(services.ServiceDeliveredError):
			self.record(visit=visit)
		frappe.db.set_value(SERVICE_TYPE_DOCTYPE, SERVICE, "enabled", 1)
		wrong = make_visit().name
		engine.transition(
			wrong, "close_visit", closure_reason="Registered in error", comment="T- x", system=True
		)
		with self.assertRaises(services.ServiceDeliveredError):
			self.record(visit=wrong)

	def test_read_permissions_by_role(self):
		"""Only CO, MHO, MIC and HIS read services; Patient Visit readers such as the Receptionist or
		the Head of Operations do not."""
		doc = self.record()
		readers = {"clinician", "mho", "mic", "his", "clinician2"}
		for key in (*ALL_KEYS, "clinician2", "outreach"):
			allowed = frappe.has_permission(
				SERVICE_DELIVERED_DOCTYPE, "read", doc=doc.name, user=self.users[key]
			)
			self.assertEqual(bool(allowed), key in readers, key)
		for key in ("hoo", "reception"):
			with as_user(self.users[key]), self.assertRaises(frappe.PermissionError):
				frappe.get_list(SERVICE_DELIVERED_DOCTYPE, filters={"visit": doc.visit})
			with as_user(self.users[key]), self.assertRaises(frappe.PermissionError):
				services.get_visit_services(doc.visit)
		with as_user(self.users["mic"]):
			out = services.get_visit_services(doc.visit)
		self.assertEqual([r.name for r in out["services"]], [doc.name])
		self.assertFalse(out["can_record"])
		with as_user(self.users["clinician2"]):
			self.assertTrue(services.get_visit_services(doc.visit)["can_record"])

	def test_permissions_in_json(self):
		perms = {(p.role, p.permlevel): p for p in frappe.get_meta(SERVICE_DELIVERED_DOCTYPE).permissions}
		self.assertFalse(any(p.get("delete") for p in perms.values()))
		self.assertTrue(perms[("Clinical Officer", 0)].create and perms[("Clinical Officer", 0)].write)
		self.assertTrue(perms[("Mental Health Officer", 0)].create)
		self.assertFalse(perms[("Medical In-Charge", 0)].create)
		self.assertTrue(perms[("Medical In-Charge", 0)].write)
		self.assertFalse(perms[("HIS Officer", 0)].write)
		self.assertEqual(
			{role for role, _lvl in perms},
			{"Clinical Officer", "Mental Health Officer", "Medical In-Charge", "HIS Officer"},
		)

	def test_void(self):
		doc = self.record()
		with as_user(self.users["clinician"]), self.assertRaises(services.IllegalServiceTransitionError):
			services.void_service(doc.name, "  ")
		for key in ("clinician2", "mho", "his", "reception"):
			with as_user(self.users[key]), self.assertRaises(frappe.PermissionError, msg=key):
				services.void_service(doc.name, "T- wrong patient")
		with as_user(self.users["clinician"]):
			out = services.void_service(doc.name, "T- wrong patient")
		self.assertEqual(out["status"], services.STATUS_VOIDED)
		row = frappe.db.get_value(
			SERVICE_DELIVERED_DOCTYPE, doc.name, ["void_reason", "voided_by", "voided_at"], as_dict=True
		)
		self.assertEqual((row.void_reason, row.voided_by), ("T- wrong patient", self.users["clinician"]))
		self.assertTrue(row.voided_at)
		with as_user(self.users["clinician"]), self.assertRaises(services.IllegalServiceTransitionError):
			services.void_service(doc.name, "T- again")

	def test_mic_voids_any(self):
		doc = self.record()
		with as_user(self.users["mic"]):
			self.assertEqual(
				services.void_service(doc.name, "T- duplicate")["status"], services.STATUS_VOIDED
			)

	def test_edit_rules(self):
		doc = self.record()
		with as_user(self.users["clinician"]):
			d = frappe.get_doc(SERVICE_DELIVERED_DOCTYPE, doc.name)
			d.notes = "T- follow-up agreed"
			d.save()
		with as_user(self.users["clinician2"]), self.assertRaises(frappe.PermissionError):
			d = frappe.get_doc(SERVICE_DELIVERED_DOCTYPE, doc.name)
			d.notes = "T- not mine"
			d.save()
		with as_user(self.users["mic"]):
			d = frappe.get_doc(SERVICE_DELIVERED_DOCTYPE, doc.name)
			d.service_type = "Social investigation"
			d.save()
		with as_user(self.users["clinician"]), self.assertRaises(ServiceDeliveredLockedError):
			d = frappe.get_doc(SERVICE_DELIVERED_DOCTYPE, doc.name)
			d.status = services.STATUS_VOIDED
			d.save()
		with as_user(self.users["clinician"]), self.assertRaises(ServiceDeliveredLockedError):
			d = frappe.get_doc(SERVICE_DELIVERED_DOCTYPE, doc.name)
			d.patient = make_visit().patient
			d.save()
		with as_user(self.users["clinician"]):
			services.void_service(doc.name, "T- error")
		with as_user(self.users["mic"]), self.assertRaises(ServiceDeliveredLockedError):
			d = frappe.get_doc(SERVICE_DELIVERED_DOCTYPE, doc.name)
			d.notes = "T- after void"
			d.save()

	def test_no_delete(self):
		doc = self.record()
		for key in ("clinician", "mic", "his"):
			with as_user(self.users[key]), self.assertRaises(frappe.PermissionError, msg=key):
				frappe.delete_doc(SERVICE_DELIVERED_DOCTYPE, doc.name)
		with self.assertRaises(frappe.PermissionError):  # the delete guard, even for Administrator
			frappe.delete_doc(SERVICE_DELIVERED_DOCTYPE, doc.name, ignore_permissions=True)
		self.assertTrue(frappe.db.exists(SERVICE_DELIVERED_DOCTYPE, doc.name))

	def test_not_a_visit_field(self):
		"""Services never travel with the visit record (no child table)."""
		meta = frappe.get_meta(VISIT_DOCTYPE)
		self.assertFalse([df for df in meta.get_table_fields() if df.options == SERVICE_DELIVERED_DOCTYPE])


# --------------------------------------------------------------------------- Community Activity Record


class TestCommunityActivity(CommunityTestCase):
	def make(self, user_key="clinician", submit=False, **kw):
		values = {
			"doctype": COMMUNITY_ACTIVITY_DOCTYPE,
			"activity_type": ACTIVITY,
			"activity_date": today(),
			"facility": get_facility(0),
			"location": "T-Village 3",
			"topic": "T- hand washing",
			"participants_male": 12,
			"participants_female": 18,
			"participants_total": 30,
		}
		values.update(kw)
		with as_user(self.users[user_key]):
			doc = frappe.get_doc(values).insert()
			if submit:
				doc.submit()
		return doc

	def test_transition_table(self):
		self.assertEqual(set(activities.TRANSITIONS), {"save", "submit", "cancel", "amend"})
		cancel = activities.TRANSITIONS["cancel"]
		self.assertEqual(
			(cancel.from_docstatus, cancel.to_docstatus, cancel.any_roles), ((1,), 2, ("Medical In-Charge",))
		)
		self.assertTrue(cancel.needs_reason)

	def test_submit_by_authors(self):
		for key in ("clinician", "mho", "outreach", "mic"):
			doc = self.make(key, submit=True)
			self.assertEqual(doc.docstatus, 1, key)
			self.assertEqual(doc.lead_staff, self.users[key])
			self.assertIn("CAR", doc.name)

	def test_other_roles_cannot_create(self):
		for key in ("reception", "triage", "lab", "pharmacist", "his", "hoo", "sysadmin"):
			with self.assertRaises(frappe.PermissionError, msg=key):
				self.make(key)

	def test_read_own_and_all(self):
		doc = self.make("clinician", submit=True)
		expected = {
			"clinician": True,
			"clinician2": False,
			"mho": False,
			"outreach": False,
			"mic": True,
			"his": True,
		}
		expected.update({k: False for k in ("reception", "triage", "lab", "pharmacist", "hoo", "sysadmin")})
		for key, allowed in expected.items():
			got = frappe.has_permission(
				COMMUNITY_ACTIVITY_DOCTYPE, "read", doc=doc.name, user=self.users[key]
			)
			self.assertEqual(bool(got), allowed, key)
		self.assertFalse(
			frappe.has_permission(COMMUNITY_ACTIVITY_DOCTYPE, "write", doc=doc.name, user=self.users["his"])
		)

	def test_validation(self):
		with self.assertRaises(activities.CommunityActivityError):
			self.make(participants_total=29)  # less than 12 + 18
		with self.assertRaises(activities.CommunityActivityError):
			self.make(activity_date=add_days(today(), 1))
		with self.assertRaises(activities.CommunityActivityError):
			self.make(participants_male=0, participants_female=0, participants_total=0)
		with self.assertRaises(activities.CommunityActivityError):
			self.make(participants_male=-1)
		root = frappe.db.get_value(
			"Healthcare Service Unit", {"is_group": 1, "parent_healthcare_service_unit": ["is", "not set"]}
		)
		with self.assertRaises(activities.CommunityActivityError):
			self.make(facility=root)
		frappe.db.set_value(COMMUNITY_ACTIVITY_TYPE_DOCTYPE, ACTIVITY, "enabled", 0)
		with self.assertRaises(activities.CommunityActivityError):
			self.make()
		frappe.db.set_value(COMMUNITY_ACTIVITY_TYPE_DOCTYPE, ACTIVITY, "enabled", 1)
		self.assertEqual(self.make(participants_total=0).participants_total, 30)  # filled from male + female
		self.assertEqual(self.make(participants_total=35).participants_total, 35)  # 5 not counted by sex

	def test_cancel_needs_mic_and_reason(self):
		doc = self.make("clinician", submit=True)
		for key in ("clinician", "his", "outreach"):
			with as_user(self.users[key]), self.assertRaises(frappe.PermissionError, msg=key):
				activities.cancel_activity(doc.name, "T- duplicate entry")
		with as_user(self.users["mic"]), self.assertRaises(activities.CommunityActivityError):
			activities.cancel_activity(doc.name, " ")
		with as_user(self.users["mic"]), self.assertRaises(activities.CommunityActivityError):
			frappe.get_doc(COMMUNITY_ACTIVITY_DOCTYPE, doc.name).cancel()  # desk cancel without a reason
		with as_user(self.users["mic"]):
			out = activities.cancel_activity(doc.name, "T- duplicate entry")
		self.assertEqual(out["docstatus"], 2)
		row = frappe.db.get_value(
			COMMUNITY_ACTIVITY_DOCTYPE,
			doc.name,
			["cancel_reason", "cancelled_by", "cancelled_at"],
			as_dict=True,
		)
		self.assertEqual((row.cancel_reason, row.cancelled_by), ("T- duplicate entry", self.users["mic"]))
		self.assertTrue(row.cancelled_at)
		with as_user(self.users["mic"]), self.assertRaises(activities.CommunityActivityError):
			activities.cancel_activity(doc.name, "T- again")
		draft = self.make("clinician")
		with as_user(self.users["mic"]), self.assertRaises(activities.CommunityActivityError):
			activities.cancel_activity(draft.name, "T- draft")

	def test_amend_after_cancel(self):
		doc = self.make("clinician", submit=True)
		with as_user(self.users["mic"]):
			activities.cancel_activity(doc.name, "T- wrong count")
		with as_user(self.users["clinician"]):
			amended = frappe.copy_doc(frappe.get_doc(COMMUNITY_ACTIVITY_DOCTYPE, doc.name))
			amended.amended_from = doc.name
			amended.docstatus = 0
			amended.participants_total = 31
			amended.insert()
			amended.submit()
		self.assertEqual(amended.docstatus, 1)
		self.assertFalse(amended.cancel_reason)
		self.assertTrue(amended.name.startswith(doc.name))

	def test_no_delete(self):
		doc = self.make("clinician")
		self.assertFalse(
			any(p.get("delete") for p in frappe.get_meta(COMMUNITY_ACTIVITY_DOCTYPE).permissions)
		)
		for key in ("clinician", "mic"):
			with as_user(self.users[key]), self.assertRaises(frappe.PermissionError, msg=key):
				frappe.delete_doc(COMMUNITY_ACTIVITY_DOCTYPE, doc.name)
		self.assertTrue(frappe.db.exists(COMMUNITY_ACTIVITY_DOCTYPE, doc.name))
