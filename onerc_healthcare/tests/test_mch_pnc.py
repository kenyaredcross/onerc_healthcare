# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MCH-A: PNC Visit (docs/MCH_CONTRACT.md §1, §3, §4, §6; BRD FR-14; D62). Never commit.

Deliveries are real Delivery Record submits (`mch_factories.make_delivery`): the admission moves to
Delivered (still open) and each live-born baby becomes a Patient with ``krcs_mother`` and a date of birth =
the delivery date. Expected values by hand: a delivery 2 days ago gives ``days_after_delivery`` = 2 for the
mother and for the baby (born that day).
"""

from __future__ import annotations

from contextlib import contextmanager

import frappe
from frappe.utils import add_days, add_years, today

from onerc_healthcare import constants as c
from onerc_healthcare.mch import common
from onerc_healthcare.mch.anc import ImplausibleANCValueError
from onerc_healthcare.mch.common import MCHRecordError
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import get_facility, make_patient
from onerc_healthcare.tests.mch_factories import (
	live_baby,
	make_child,
	make_delivery,
	make_mother,
	make_next_visit,
	mch_users,
)

PNC = c.PNC_VISIT_DOCTYPE
MOTHER, BABY = c.PNC_SUBJECT_MOTHER, c.PNC_SUBJECT_BABY


def babies_of(delivery) -> list[str]:
	return [r.newborn_patient for r in frappe.get_doc(c.DELIVERY_RECORD_DOCTYPE, delivery).babies]


class PNCCase(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.users = mch_users()
		self.mother = make_mother()
		self.dr = make_delivery(self.mother.name, user=self.users["midwife"], days_ago=2)
		self.adm = self.dr.admission
		self.baby = babies_of(self.dr.name)[0]

	@contextmanager
	def as_user(self, key: str):
		frappe.set_user(self.users[key])
		try:
			yield
		finally:
			frappe.set_user("Administrator")

	def pnc(self, user="midwife", patient=None, subject=MOTHER, visit=None, admission=None, **kw):
		patient = patient or self.mother.name
		values = {
			"doctype": PNC,
			"patient": patient,
			"subject": subject,
			"pnc_date": today(),
			"timing": "Within 48 hours",
		}
		if admission:
			values["maternity_admission"] = admission
		else:
			values["visit"] = visit or make_next_visit(patient).name
		values.update(kw)
		with self.as_user(user):
			return frappe.get_doc(values).insert()

	def submit(self, doc, user="midwife"):
		with self.as_user(user):
			doc = frappe.get_doc(PNC, doc.name)
			doc.submit()
		return doc


class TestMotherAndBaby(PNCCase):
	def test_mother_through_visit(self):
		doc = self.pnc(m_bp_systolic=118, m_bp_diastolic=76, m_temperature=36.9, b_weight_kg=3.2)
		self.assertEqual(
			(doc.mother, doc.delivery_record, doc.days_after_delivery, doc.facility),
			(self.mother.name, self.dr.name, 2, get_facility(0)),
		)
		self.assertEqual((doc.m_bp_systolic, doc.b_weight_kg), (118, 0))  # baby fields cleared
		self.assertEqual(doc.recorded_by, self.users["midwife"])
		self.assertEqual(self.submit(doc).docstatus, 1)

	def test_mother_through_admission(self):
		doc = self.pnc(admission=self.adm, m_pulse=88, m_lochia="Normal")
		adm_facility = frappe.db.get_value(c.MATERNITY_ADMISSION_DOCTYPE, self.adm, "facility")
		self.assertEqual(
			(doc.visit, doc.facility, doc.mother, doc.delivery_record, doc.days_after_delivery),
			(None, adm_facility, self.mother.name, self.dr.name, 2),
		)
		with self.assertRaises(MCHRecordError):
			self.pnc(admission=self.adm)  # one per (admission, patient)
		self.assertEqual(self.submit(doc).docstatus, 1)

	def test_baby_through_admission(self):
		doc = self.pnc(
			admission=self.adm,
			patient=self.baby,
			subject=BABY,
			b_weight_kg=3.05,
			b_cord="Clean and dry",
			m_bp_systolic=120,
			m_bp_diastolic=80,
			fp_counselled=1,
		)
		self.assertEqual(
			(doc.mother, doc.delivery_record, doc.days_after_delivery),
			(self.mother.name, self.dr.name, 2),
		)
		self.assertEqual((doc.b_weight_kg, doc.m_bp_systolic, doc.fp_counselled), (3.05, 0, 0))
		# The mother's and the baby's ward checks are separate records on the same admission.
		self.assertTrue(self.pnc(admission=self.adm).name)

	def test_baby_through_visit(self):
		doc = self.pnc(patient=self.baby, subject=BABY, b_temperature=36.8)
		self.assertEqual(
			(doc.mother, doc.delivery_record, doc.days_after_delivery), (self.mother.name, self.dr.name, 2)
		)
		# One per (visit, subject, patient).
		with self.assertRaises(MCHRecordError):
			self.pnc(patient=self.baby, subject=BABY, visit=doc.visit)

	def test_baby_links_the_right_delivery_record(self):
		# Another mother's twins on another delivery, the same day.
		other = make_mother()
		dr2 = make_delivery(
			other.name, user=self.users["midwife"], babies=[live_baby("Male"), live_baby("Female")]
		)
		twin = babies_of(dr2.name)[1]
		doc = self.pnc(patient=twin, subject=BABY)
		self.assertEqual(
			(doc.mother, doc.delivery_record, doc.days_after_delivery), (other.name, dr2.name, 0)
		)
		# A corrected (superseded) delivery: the archived record still lists the baby; the new one is linked.
		from onerc_healthcare.maternity import delivery

		with self.as_user("mic"):
			new_name = delivery.supersede(self.dr.name, "T- wrong weight")["name"]
		with self.as_user("midwife"):
			frappe.get_doc(c.DELIVERY_RECORD_DOCTYPE, new_name).submit()
		self.assertEqual(babies_of(self.dr.name)[0], babies_of(new_name)[0])
		baby_check = self.pnc(patient=self.baby, subject=BABY)
		mother_check = self.pnc()
		self.assertEqual((baby_check.delivery_record, mother_check.delivery_record), (new_name, new_name))

	def test_baby_born_elsewhere(self):
		baby = make_child(age_days=10, sex="Female")
		doc = self.pnc(patient=baby.name, subject=BABY, timing="1 to 2 weeks")
		self.assertEqual((doc.mother, doc.delivery_record, doc.days_after_delivery), (None, None, 10))

	def test_mother_without_delivery_record(self):
		other = make_mother()
		doc = self.pnc(patient=other.name)
		self.assertEqual((doc.mother, doc.delivery_record, doc.days_after_delivery), (other.name, None, 0))


class TestRules(PNCCase):
	def test_exactly_one_of_visit_or_admission(self):
		visit = make_next_visit(self.mother.name).name
		base = {"doctype": PNC, "patient": self.mother.name, "subject": MOTHER, "timing": "Other"}
		for links in (
			{"visit": visit, "maternity_admission": self.adm},  # both
			{},  # neither
		):
			with self.subTest(links=links), self.as_user("midwife"), self.assertRaises(MCHRecordError):
				frappe.get_doc({**base, **links}).insert()
		self.assertTrue(self.pnc(visit=visit).name)  # either one alone

	def test_admission_rules(self):
		stranger = make_mother()
		with self.assertRaises(MCHRecordError):
			self.pnc(admission=self.adm, patient=stranger.name)  # not the admitted mother nor her baby
		with self.assertRaises(MCHRecordError):
			self.pnc(admission=self.adm, subject=BABY)  # the mother is the Mother on her admission
		with self.assertRaises(MCHRecordError):
			self.pnc(admission=self.adm, patient=self.baby, subject=MOTHER)
		frappe.db.set_value(c.MATERNITY_ADMISSION_DOCTYPE, self.adm, "status", c.ADMISSION_DISCHARGED)
		with self.assertRaises(MCHRecordError):
			self.pnc(admission=self.adm)  # discharged: not open
		frappe.db.set_value(
			c.MATERNITY_ADMISSION_DOCTYPE, self.adm, {"status": c.ADMISSION_DELIVERED, "krcs_archived": 1}
		)
		with self.assertRaises(MCHRecordError):
			self.pnc(admission=self.adm)  # archived

	def test_closed_visit_on_insert(self):
		visit = make_next_visit(self.mother.name)
		draft = self.pnc(visit=visit.name)
		frappe.db.set_value(c.VISIT_DOCTYPE, visit.name, "status", c.VISIT_STATUS_DISCHARGED)
		with self.assertRaises(MCHRecordError):
			self.pnc(patient=self.baby, subject=BABY, visit=make_closed_visit(self.baby))
		self.assertEqual(self.submit(draft).docstatus, 1)  # a draft is still submitted after the visit closes

	def test_patient_checks(self):
		man = make_patient(sex="Male")
		with self.assertRaises(MCHRecordError):
			self.pnc(patient=man.name)  # a mother's check is for a female patient
		toddler = make_child(age_days=400)
		with self.assertRaises(MCHRecordError):
			self.pnc(patient=toddler.name, subject=BABY)  # under 1 year only
		one_year = make_child(dob=add_years(today(), -1))
		with self.assertRaises(MCHRecordError):
			self.pnc(patient=one_year.name, subject=BABY)  # the first birthday is no longer "under 1"
		with self.assertRaises(ImplausibleANCValueError):
			self.pnc(pnc_date=add_days(today(), 1))  # future
		with self.assertRaises(ImplausibleANCValueError):
			self.pnc(pnc_date=add_days(today(), -3))  # before the delivery (2 days ago)

	def test_ranges(self):
		bad = (
			{"m_temperature": 29.9},
			{"m_temperature": 43.5},
			{"m_pulse": 25},
			{"m_pulse": 230},
			{"m_bp_systolic": 260, "m_bp_diastolic": 80},
			{"m_bp_systolic": 80, "m_bp_diastolic": 80},
			{"m_bp_diastolic": 70},
		)
		for kw in bad:
			with self.subTest(kw=kw), self.assertRaises(ImplausibleANCValueError):
				self.pnc(**kw)
		for kw in ({"b_temperature": 44}, {"b_weight_kg": 0.4}, {"b_weight_kg": 10.5}):
			with self.subTest(kw=kw), self.assertRaises(ImplausibleANCValueError):
				self.pnc(patient=self.baby, subject=BABY, **kw)
		self.assertTrue(self.pnc(m_temperature=37.2, m_pulse=90).name)
		self.assertTrue(self.pnc(patient=self.baby, subject=BABY, b_temperature=36.5, b_weight_kg=0.5).name)

	def test_submit_cancel_edit_addendum(self):
		doc = self.submit(self.pnc(m_pulse=80))
		with self.as_user("clinician"):
			doc = frappe.get_doc(PNC, doc.name)
			with self.assertRaises(frappe.PermissionError):
				doc.cancel()
			doc = frappe.get_doc(PNC, doc.name)
			doc.m_pulse = 82
			with self.assertRaises(frappe.ValidationError):
				doc.save()
			out = common.add_addendum(PNC, doc.name, "T- pulse was 82")
		self.assertIn("T- pulse was 82", out["addendum"])
		with self.as_user("triage"), self.assertRaises(frappe.PermissionError):
			common.add_addendum(PNC, doc.name, "T- no")

	def test_archive_only_by_mic(self):
		doc = self.submit(self.pnc())
		for key in ("midwife", "his"):
			with self.subTest(user=key), self.as_user(key), self.assertRaises(frappe.PermissionError):
				common.archive(PNC, doc.name, "T- error")
		with self.as_user("mic"):
			common.archive(PNC, doc.name, "T- wrong mother")
		self.assertEqual(frappe.db.get_value(PNC, doc.name, "krcs_archived"), 1)
		self.assertTrue(self.pnc(visit=doc.visit).name)  # archived: the visit is free again

	def test_role_gates(self):
		for key in ("triage", "reception", "mho", "his"):
			with self.subTest(user=key), self.assertRaises(frappe.PermissionError):
				self.pnc(user=key)
		doc = self.pnc(user="mic")
		self.assertEqual(self.submit(doc, user="mic").docstatus, 1)
		for key, read, write in (("his", True, False), ("mho", True, False), ("triage", False, False)):
			with self.subTest(user=key):
				self.assertEqual(
					bool(frappe.has_permission(PNC, "read", doc.name, user=self.users[key])), read
				)
				self.assertEqual(
					bool(frappe.has_permission(PNC, "write", doc.name, user=self.users[key])), write
				)

	def test_open_writes_access_log(self):
		from frappe.desk.form.load import getdoc

		doc = self.pnc(patient=self.baby, subject=BABY)
		with self.as_user("mho"):
			getdoc(PNC, doc.name)
		rows = frappe.get_all(
			"Access Log Entry",
			filters={"reference_doctype": PNC, "reference_name": doc.name},
			fields=["action", "user", "patient"],
			ignore_permissions=True,
		)
		self.assertEqual(
			[(r.action, r.user, r.patient) for r in rows], [("Open", self.users["mho"], self.baby)]
		)


def make_closed_visit(patient: str) -> str:
	visit = make_next_visit(patient).name
	frappe.db.set_value(c.VISIT_DOCTYPE, visit, "status", c.VISIT_STATUS_DISCHARGED)
	return visit
