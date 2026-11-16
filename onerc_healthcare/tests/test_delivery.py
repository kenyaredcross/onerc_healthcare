# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""M3: Delivery Record and newborn registration (SDD §5.12.3, INP-16, OI-23, D60; contract §6.3). Never commit.

Admissions come from `maternity_factories.make_admission` (Admitted, admitted now, no visit, no bed). Every
expected value is written out by hand from the contract: one Patient per live-born baby, none for a
stillbirth, twins without a duplicate refusal, a second call creating nothing.
"""

from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.utils import add_to_date, get_datetime, getdate, now_datetime

from onerc_healthcare import constants as c
from onerc_healthcare.maternity import delivery
from onerc_healthcare.maternity.delivery import DeliveryRecordError
from onerc_healthcare.registration.identifiers import (
	OVERRIDE_REASON_FIELD,
	POPULATION_GROUP_FIELD,
	REGISTRATION_FACILITY_FIELD,
	RESIDENCE_FIELD,
)
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.maternity_factories import make_admission, make_maternity_patient, maternity_users
from onerc_healthcare.tests.outreach_factories import make_residence

LIVE, FRESH, MACERATED = c.BABY_LIVE_BIRTH, c.BABY_FRESH_STILLBIRTH, c.BABY_MACERATED_STILLBIRTH


def baby(outcome=LIVE, sex="Female", **kw) -> dict:
	row = {"outcome": outcome, "sex": sex if outcome == LIVE else None}
	row.update({"weight_kg": 3.1, "apgar_1": 8, "apgar_5": 9})
	row.update(kw)
	return row


def newborns_of(mother: str) -> list[str]:
	return frappe.get_all(
		"Patient", filters={c.PATIENT_MOTHER_FIELD: mother}, pluck="name", order_by="creation"
	)


class DeliveryCase(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.users = maternity_users()
		residence = make_residence(is_group=0)
		self.mother = make_maternity_patient(**{RESIDENCE_FIELD: residence})
		self.adm = make_admission(self.mother.name)

	def new_delivery(self, babies=None, admission=None, **kw):
		values = {
			"doctype": c.DELIVERY_RECORD_DOCTYPE,
			"admission": admission or self.adm.name,
			"delivered_at": now_datetime(),
			"delivery_mode": c.DELIVERY_SVD,
			"babies": babies if babies is not None else [baby()],
		}
		values.update(kw)
		return frappe.get_doc(values)

	def deliver(self, babies=None, user="midwife", **kw):
		"""Insert and submit as `user` (a key of maternity_users)."""
		frappe.set_user(self.users[user])
		try:
			doc = self.new_delivery(babies, **kw).insert()
			doc.submit()
		finally:
			frappe.set_user("Administrator")
		return doc


class TestNewborns(DeliveryCase):
	def test_single_live_birth(self):
		doc = self.deliver([baby(sex="Male", weight_kg=3.4)])
		self.assertEqual(doc.docstatus, 1)
		# M1: the admission moves to Delivered with a record_delivery log row.
		adm = frappe.get_doc(c.MATERNITY_ADMISSION_DOCTYPE, self.adm.name)
		self.assertEqual(adm.status, c.ADMISSION_DELIVERED)
		self.assertEqual(adm.status_log[-1].action, "record_delivery")

		names = newborns_of(self.mother.name)
		self.assertEqual(len(names), 1)
		self.assertEqual(
			frappe.db.get_value(c.DELIVERY_BABY_DOCTYPE, doc.babies[0].name, "newborn_patient"), names[0]
		)
		self.assertEqual(doc.babies[0].newborn_patient, names[0])  # in memory too (the form shows it)

		p = frappe.get_doc("Patient", names[0])
		self.assertEqual(p.first_name, "Baby")
		self.assertEqual(p.last_name, self.mother.last_name)
		self.assertEqual(p.sex, "Male")
		self.assertEqual(getdate(p.dob), getdate(doc.delivered_at))
		self.assertEqual(p.get(c.PATIENT_WALK_IN_FIELD), 1)
		self.assertEqual(p.get(REGISTRATION_FACILITY_FIELD), self.mother.get(REGISTRATION_FACILITY_FIELD))
		self.assertEqual(p.get(POPULATION_GROUP_FIELD), self.mother.get(POPULATION_GROUP_FIELD))
		self.assertTrue(p.get(POPULATION_GROUP_FIELD))
		self.assertEqual(p.get(RESIDENCE_FIELD), self.mother.get(RESIDENCE_FIELD))
		self.assertEqual(p.get(c.PATIENT_MOTHER_FIELD), self.mother.name)
		self.assertEqual(p.get(OVERRIDE_REASON_FIELD), c.NEWBORN_OVERRIDE_REASON)
		self.assertEqual(
			[(r.relation, r.patient) for r in p.patient_relation], [("Mother", self.mother.name)]
		)
		# A facility-generated (walk-in) ID at the mother's registration facility, e.g. NTK-2026-00012.
		code = frappe.db.get_value(
			"Healthcare Service Unit", self.mother.get(REGISTRATION_FACILITY_FIELD), "krcs_facility_code"
		)
		ids = [r for r in p.krcs_identifiers if r.identifier_type == c.IDENTIFIER_FACILITY]
		self.assertEqual(len(ids), 1)
		self.assertTrue(ids[0].identifier_value.startswith(f"{code.upper()}-"), ids[0].identifier_value)

	def test_twins_two_patients_no_duplicate_refusal(self):
		# Same name ("Baby" + mother's surname), same DOB, same sex: a strong duplicate without the reason.
		doc = self.deliver([baby(sex="Female", weight_kg=2.4), baby(sex="Female", weight_kg=2.2)])
		names = newborns_of(self.mother.name)
		self.assertEqual(len(names), 2)
		self.assertEqual([r.newborn_patient for r in doc.babies], names)
		for name in names:
			self.assertEqual(frappe.db.get_value("Patient", name, "first_name"), "Baby")
		ids = {
			frappe.db.get_value(
				"Patient Identifier",
				{"parent": n, "identifier_type": c.IDENTIFIER_FACILITY},
				"identifier_value",
			)
			for n in names
		}
		self.assertEqual(len(ids), 2)  # two different walk-in IDs
		# Control: without the newborn reason the same registration is a strong duplicate (FR-2).
		from onerc_healthcare.registration.identifiers import PossibleDuplicatePatientError

		with self.assertRaises(PossibleDuplicatePatientError):
			frappe.get_doc(
				{
					"doctype": "Patient",
					"first_name": "Baby",
					"last_name": self.mother.last_name,
					"sex": "Female",
					"dob": getdate(doc.delivered_at),
					c.PATIENT_WALK_IN_FIELD: 1,
				}
			).insert(ignore_permissions=True)

	def test_stillbirth_gets_no_patient(self):
		doc = self.deliver([baby(FRESH, weight_kg=2.9, apgar_1=0, apgar_5=0)])
		self.assertEqual(newborns_of(self.mother.name), [])
		self.assertIsNone(frappe.db.get_value(c.DELIVERY_BABY_DOCTYPE, doc.babies[0].name, "newborn_patient"))
		self.assertEqual(
			frappe.db.get_value(c.MATERNITY_ADMISSION_DOCTYPE, self.adm.name, "status"), c.ADMISSION_DELIVERED
		)

	def test_twin_live_and_macerated(self):
		doc = self.deliver([baby(MACERATED, apgar_1=0, apgar_5=0), baby(sex="Male")])
		names = newborns_of(self.mother.name)
		self.assertEqual(len(names), 1)
		self.assertEqual([r.newborn_patient for r in doc.babies], [None, names[0]])

	def test_idempotent(self):
		doc = self.deliver([baby(), baby(FRESH, apgar_1=0, apgar_5=0), baby(sex="Male")])
		first = newborns_of(self.mother.name)
		self.assertEqual(len(first), 2)
		frappe.set_user(self.users["midwife"])
		# Again on the same object, on a fresh copy, and on a copy whose rows lost the link in memory.
		self.assertEqual(delivery.register_newborns(doc), first)
		self.assertEqual(
			delivery.register_newborns(frappe.get_doc(c.DELIVERY_RECORD_DOCTYPE, doc.name)), first
		)
		stale = frappe.get_doc(c.DELIVERY_RECORD_DOCTYPE, doc.name)
		for row in stale.babies:
			row.newborn_patient = None
		self.assertEqual(delivery.register_newborns(stale), first)
		frappe.set_user("Administrator")
		self.assertEqual(newborns_of(self.mother.name), first)

	def test_mother_without_last_name(self):
		mother = make_maternity_patient(last_name=None, first_name="T-Amina")
		adm = make_admission(mother.name)
		self.deliver([baby()], admission=adm.name)
		names = newborns_of(mother.name)
		self.assertEqual(frappe.db.get_value("Patient", names[0], "last_name"), "T-Amina")

	def test_client_cannot_preset_newborn_patient(self):
		other = make_maternity_patient().name
		frappe.set_user(self.users["midwife"])
		doc = self.new_delivery([baby(newborn_patient=other)]).insert()
		self.assertIsNone(doc.babies[0].newborn_patient)
		doc.babies[0].newborn_patient = other
		doc.submit()
		frappe.set_user("Administrator")
		self.assertNotEqual(doc.babies[0].newborn_patient, other)
		self.assertEqual(newborns_of(self.mother.name), [doc.babies[0].newborn_patient])


class TestValidation(DeliveryCase):
	def refused(self, exc=DeliveryRecordError, **kw):
		frappe.set_user(self.users["midwife"])
		try:
			with self.assertRaises(exc):
				self.new_delivery(**kw).insert()
		finally:
			frappe.set_user("Administrator")

	def test_refusals(self):
		now = now_datetime()
		cases = {
			"no baby": {"babies": []},
			"future": {"delivered_at": now + timedelta(hours=1)},
			"before admission": {"delivered_at": get_datetime(self.adm.admitted_at) - timedelta(hours=2)},
			"no mode": {"delivery_mode": ""},
			"unknown mode": {"delivery_mode": "Water birth"},
			"apgar 11": {"babies": [baby(apgar_1=11)]},
			"apgar -1": {"babies": [baby(apgar_5=-1)]},
			"too light": {"babies": [baby(weight_kg=0.29)]},
			"too heavy": {"babies": [baby(weight_kg=6.6)]},
			"live without sex": {"babies": [baby(sex=None)]},
		}
		for label, kw in cases.items():
			with self.subTest(label):
				self.refused(**kw)

	def test_limits_accepted(self):
		frappe.set_user(self.users["midwife"])
		doc = self.new_delivery(
			[baby(weight_kg=0.3, apgar_1=0, apgar_5=10), baby(sex="Male", weight_kg=6.5)],
			delivered_at=now_datetime() + timedelta(minutes=2),  # clock skew tolerated
		).insert()
		self.assertEqual(len(doc.babies), 2)
		self.assertEqual(doc.patient, self.mother.name)
		self.assertEqual(doc.facility, self.adm.facility)
		self.assertEqual(doc.delivered_by, self.users["midwife"])

	def test_stillbirth_needs_no_sex(self):
		frappe.set_user(self.users["midwife"])
		doc = self.new_delivery([baby(MACERATED, sex=None)]).insert()
		self.assertEqual(doc.babies[0].outcome, MACERATED)

	def test_born_before_arrival(self):
		# BBA: delivered before the admission, with a mode of delivery.
		frappe.set_user(self.users["midwife"])
		doc = self.new_delivery(
			delivered_at=add_to_date(self.adm.admitted_at, hours=-3), born_before_arrival=1
		).insert()
		doc.submit()
		self.assertEqual(doc.docstatus, 1)
		self.refused(born_before_arrival=1, delivery_mode="")

	def test_admission_must_be_admitted_or_in_labour(self):
		in_labour = make_admission(make_maternity_patient().name, status=c.ADMISSION_IN_LABOUR)
		frappe.set_user(self.users["midwife"])
		self.new_delivery(admission=in_labour.name).insert()
		frappe.set_user("Administrator")
		for status in (c.ADMISSION_DELIVERED, c.ADMISSION_DISCHARGED):
			with self.subTest(status):
				adm = make_admission(make_maternity_patient().name, status=status)
				self.refused(admission=adm.name)
		archived = make_admission(make_maternity_patient().name)
		frappe.db.set_value(c.MATERNITY_ADMISSION_DOCTYPE, archived.name, "krcs_archived", 1)
		self.refused(admission=archived.name)

	def test_one_submitted_delivery_per_admission(self):
		frappe.set_user(self.users["midwife"])
		first = self.new_delivery().insert()
		second = self.new_delivery().insert()  # two drafts are allowed
		first.submit()
		with self.assertRaises(DeliveryRecordError):
			second.reload()
			second.submit()
		frappe.set_user("Administrator")
		self.assertEqual(
			frappe.db.count(c.DELIVERY_RECORD_DOCTYPE, {"admission": self.adm.name, "docstatus": 1}), 1
		)
		self.assertEqual(len(newborns_of(self.mother.name)), 1)
		self.refused()  # a new draft on a Delivered admission

	def test_patient_comes_from_admission(self):
		frappe.set_user(self.users["midwife"])
		doc = self.new_delivery(patient=make_maternity_patient().name).insert()
		self.assertEqual(doc.patient, self.mother.name)


class TestRolesAndImmutability(DeliveryCase):
	def test_clinical_officer_submits(self):
		self.deliver([baby()], user="clinician")
		self.assertEqual(len(newborns_of(self.mother.name)), 1)

	def test_mic_edits_draft_but_cannot_create_or_submit(self):
		frappe.set_user(self.users["mic"])
		with self.assertRaises(frappe.PermissionError):
			self.new_delivery().insert()
		frappe.set_user(self.users["midwife"])
		draft = self.new_delivery().insert()
		frappe.set_user(self.users["mic"])
		draft = frappe.get_doc(c.DELIVERY_RECORD_DOCTYPE, draft.name)
		draft.remarks = "T- corrected by the MIC"
		draft.save()
		with self.assertRaises(frappe.PermissionError):
			draft.submit()
		frappe.set_user("Administrator")
		self.assertEqual(newborns_of(self.mother.name), [])

	def test_other_roles_cannot_create(self):
		for key in ("his", "reception", "mho"):
			with self.subTest(key):
				frappe.set_user(self.users[key])
				with self.assertRaises(frappe.PermissionError):
					self.new_delivery().insert()
		frappe.set_user("Administrator")

	def test_register_newborns_role_gate(self):
		doc = self.deliver([baby(FRESH, apgar_1=0, apgar_5=0)])
		for key in ("mic", "reception", "his"):
			with self.subTest(key):
				frappe.set_user(self.users[key])
				with self.assertRaises(frappe.PermissionError):
					delivery.register_newborns(doc)
		frappe.set_user("Administrator")

	def test_cancel_refused(self):
		doc = self.deliver()
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(c.DELIVERY_RECORD_DOCTYPE, doc.name).cancel()  # even the Administrator
		self.assertEqual(frappe.db.get_value(c.DELIVERY_RECORD_DOCTYPE, doc.name, "docstatus"), 1)

	def test_submitted_record_is_not_edited(self):
		doc = self.deliver()
		frappe.set_user(self.users["midwife"])
		doc = frappe.get_doc(c.DELIVERY_RECORD_DOCTYPE, doc.name)
		doc.delivery_mode = c.DELIVERY_CAESAREAN
		with self.assertRaises(frappe.UpdateAfterSubmitError):
			doc.save()
		doc.reload()
		doc.addendum = "T- typed by hand"
		with self.assertRaises(DeliveryRecordError):
			doc.save()

	def test_addendum(self):
		doc = self.deliver()
		frappe.set_user(self.users["midwife"])
		out = delivery.add_addendum(doc.name, "T- blood loss was 450 mL, not 350")
		self.assertIn("T- blood loss was 450 mL, not 350", out["addendum"])
		frappe.set_user(self.users["mic"])  # write right, no submit right
		delivery.add_addendum(doc.name, "T- checked by the MIC")
		text = frappe.db.get_value(c.DELIVERY_RECORD_DOCTYPE, doc.name, "addendum")
		self.assertLess(text.index("450 mL"), text.index("checked by the MIC"))
		for key in ("his", "reception"):
			with self.subTest(key):
				frappe.set_user(self.users[key])
				with self.assertRaises(frappe.PermissionError):
					delivery.add_addendum(doc.name, "T- no")
		frappe.set_user(self.users["midwife"])
		with self.assertRaises(DeliveryRecordError):
			delivery.add_addendum(doc.name, "  ")
		draft = make_admission(make_maternity_patient().name)
		draft_doc = self.new_delivery(admission=draft.name).insert()
		with self.assertRaises(DeliveryRecordError):
			delivery.add_addendum(draft_doc.name, "T- draft")
		frappe.set_user("Administrator")
