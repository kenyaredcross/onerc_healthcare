# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MCH-A: ANC Pregnancy and ANC Contact (docs/MCH_CONTRACT.md §1, §3-§5; BRD FR-14; D62). Never commit.

Expected values are worked out by hand and stated next to each assertion: an LMP 140 days ago gives
EDD = LMP + 280 and a gestation of 20+0 weeks today (140 = 20 x 7); an EDD 100 days ahead gives
280 - 100 = 180 days = 25+5 weeks; a contact 14 days ago on an LMP 140 days ago is 126 days = 18+0 weeks.
Permissions are tested as real users (`mch_users`), never as Administrator.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import add_days, getdate, today

from onerc_healthcare import constants as c
from onerc_healthcare.maternity.admission import ImplausibleAdmissionValueError
from onerc_healthcare.mch import anc, common
from onerc_healthcare.mch.anc import ImplausibleANCValueError
from onerc_healthcare.mch.common import MCHRecordError
from onerc_healthcare.privacy.lifecycle import LockedFieldError
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import get_facility, make_patient
from onerc_healthcare.tests.mch_factories import (
	make_delivery,
	make_mother,
	make_next_visit,
	make_pregnancy,
	mch_users,
)

PREG, CONTACT = c.ANC_PREGNANCY_DOCTYPE, c.ANC_CONTACT_DOCTYPE


class ANCCase(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.users = mch_users()
		self.mother = make_mother()

	@contextmanager
	def as_user(self, key: str):
		frappe.set_user(self.users[key])
		try:
			yield
		finally:
			frappe.set_user("Administrator")

	def book(self, user: str = "midwife", patient: str | None = None, **kw):
		values = {
			"doctype": PREG,
			"patient": patient or self.mother.name,
			"facility": get_facility(0),
			"booking_date": today(),
			"lmp": add_days(today(), -140),
			"gravida": 2,
			"para": "1",
		}
		values.update(kw)
		with self.as_user(user):
			return frappe.get_doc(values).insert()

	def contact(self, user: str = "midwife", patient: str | None = None, visit: str | None = None, **kw):
		patient = patient or self.mother.name
		values = {
			"doctype": CONTACT,
			"patient": patient,
			"visit": visit or make_next_visit(patient).name,
			"contact_date": today(),
		}
		values.update(kw)
		with self.as_user(user):
			return frappe.get_doc(values).insert()

	def submit(self, doc, user: str = "midwife"):
		with self.as_user(user):
			doc = frappe.get_doc(doc.doctype, doc.name)
			doc.submit()
		return doc


# ------------------------------------------------------------------------------------------ pregnancy


class TestPregnancy(ANCCase):
	def test_booking_sets_edd_and_gestation(self):
		preg = self.book(edd=add_days(today(), 3))  # a typed EDD is replaced: basis LMP
		lmp = getdate(add_days(today(), -140))
		self.assertEqual(getdate(preg.edd), lmp + timedelta(days=280))
		self.assertEqual(preg.gestation, "20+0 weeks")  # 140 days = 20 weeks
		self.assertEqual((preg.status, preg.edd_basis), (c.PREGNANCY_OPEN, "LMP"))
		self.assertEqual(preg.recorded_by, self.users["midwife"])
		self.assertEqual(anc.open_pregnancy(self.mother.name), preg.name)

	def test_ultrasound_edd(self):
		with self.assertRaises(ImplausibleANCValueError):
			self.book(edd_basis="Ultrasound", lmp=None)  # basis Ultrasound needs the typed EDD
		preg = self.book(edd_basis="Ultrasound", lmp=None, edd=add_days(today(), 100))
		self.assertEqual(getdate(preg.edd), getdate(add_days(today(), 100)))
		self.assertEqual(preg.gestation, "25+5 weeks")  # 280 - 100 = 180 days = 25 weeks 5 days

	def test_dating_refusals(self):
		for kw in (
			{"lmp": add_days(today(), 1)},  # LMP in the future
			{"lmp": add_days(today(), -316)},  # 45 weeks + 1 day before booking
			{"edd_basis": "Clinical estimate", "lmp": None, "edd": add_days(today(), -36)},  # 316 days
			{"edd_basis": "Ultrasound", "edd": add_days(today(), -150)},  # EDD before the LMP (-140)
			{"booking_date": add_days(today(), 1)},  # booked in the future
		):
			with self.subTest(kw=kw), self.assertRaises(ImplausibleANCValueError):
				self.book(**kw)
		# An EDD 35 days ago is 280 + 35 = 315 days (45+0 weeks) at booking: the limit, accepted.
		preg = self.book(edd_basis="Clinical estimate", lmp=None, edd=add_days(today(), -35))
		self.assertEqual(preg.gestation, "45+0 weeks")

	def test_gravida_para(self):
		with self.assertRaises(ImplausibleAdmissionValueError):
			self.book(gravida=3, para="2+1")  # 3 earlier pregnancies, gravida 3 leaves 2
		with self.assertRaises(ImplausibleAdmissionValueError):
			self.book(gravida=2, para="one")
		preg = self.book(gravida=4, para="2 + 1")
		self.assertEqual(preg.para, "2+1")

	def test_ranges(self):
		for kw in ({"haemoglobin": 1.5}, {"haemoglobin": 21}, {"height_cm": 90}, {"muac_cm": 55}):
			with self.subTest(kw=kw), self.assertRaises(ImplausibleANCValueError):
				self.book(**kw)
		self.assertTrue(self.book(haemoglobin=11.2, height_cm=158, muac_cm=24).name)

	def test_male_refused(self):
		man = make_patient(sex="Male")
		with self.assertRaises(MCHRecordError):
			self.book(patient=man.name)

	def test_one_open_pregnancy_per_mother(self):
		first = self.book()
		with self.assertRaises(MCHRecordError):
			self.book()
		# Closed: a new pregnancy can be booked.
		with self.as_user("midwife"):
			anc.close_pregnancy(first.name, c.PREGNANCY_ENDED, "T- miscarriage at 12 weeks")
		second = self.book()
		# Archived (booked in error): frees the mother too.
		with self.as_user("mic"):
			common.archive(PREG, second.name, "T- wrong mother")
		third = self.book()
		self.assertEqual(anc.open_pregnancy(self.mother.name), third.name)
		# The factory pregnancy obeys the same rule.
		with self.assertRaises(MCHRecordError):
			make_pregnancy(self.mother.name)

	def test_server_fields(self):
		# A new pregnancy is Open whatever the form sends.
		preg = self.book(status=c.PREGNANCY_CLOSED, closed_reason=c.PREGNANCY_DELIVERED, closure_note="T-")
		self.assertEqual((preg.status, preg.closed_reason, preg.closure_note), (c.PREGNANCY_OPEN, None, None))
		for field, value in (("status", c.PREGNANCY_CLOSED), ("closed_on", today()), ("closure_note", "T-")):
			with self.subTest(field=field), self.as_user("midwife"):
				doc = frappe.get_doc(PREG, preg.name)
				doc.set(field, value)
				with self.assertRaises(LockedFieldError):
					doc.save()
		with self.as_user("midwife"):  # ordinary fields stay editable while Open
			doc = frappe.get_doc(PREG, preg.name)
			doc.remarks = "T- booked late"
			doc.save()

	def test_close_pregnancy(self):
		preg = self.book()
		for key in ("triage", "his", "reception"):
			with self.subTest(user=key), self.as_user(key), self.assertRaises(frappe.PermissionError):
				anc.close_pregnancy(preg.name, c.PREGNANCY_ENDED)
		with self.as_user("midwife"), self.assertRaises(MCHRecordError):
			anc.close_pregnancy(preg.name, c.PREGNANCY_DELIVERED)  # only a Delivery Record submit
		with self.as_user("clinician"):
			out = anc.close_pregnancy(preg.name, c.PREGNANCY_TRANSFERRED, "  T- moved to Kakuma  ")
		self.assertEqual(out["status"], c.PREGNANCY_CLOSED)
		preg.reload()
		self.assertEqual(
			(preg.closed_reason, getdate(preg.closed_on), preg.closed_by, preg.closure_note),
			(c.PREGNANCY_TRANSFERRED, getdate(today()), self.users["clinician"], "T- moved to Kakuma"),
		)
		with self.as_user("midwife"), self.assertRaises(MCHRecordError):
			anc.close_pregnancy(preg.name, c.PREGNANCY_ENDED)  # already closed
		# A closed pregnancy is read-only; the Medical In-Charge may still archive it.
		with self.as_user("midwife"):
			doc = frappe.get_doc(PREG, preg.name)
			doc.remarks = "T- late note"
			with self.assertRaises(MCHRecordError):
				doc.save()
		with self.as_user("midwife"), self.assertRaises(frappe.PermissionError):
			common.archive(PREG, preg.name, "T- error")
		with self.as_user("mic"):
			common.archive(PREG, preg.name, "T- error")
		self.assertEqual(frappe.db.get_value(PREG, preg.name, "krcs_archived"), 1)

	def test_admit_prefill(self):
		self.assertEqual(anc.admit_prefill(self.mother.name), {})  # no pregnancy
		preg = self.book(gravida=3, para="1+1", blood_group="O", rhesus="Negative")
		with self.as_user("midwife"):
			out = anc.admit_prefill(self.mother.name)
		lmp = getdate(add_days(today(), -140))
		self.assertEqual(
			out,
			{
				"lmp": lmp,
				"edd_basis": "LMP",
				"edd": lmp + timedelta(days=280),
				"gravida": 3,
				"para": "1+1",
				"blood_group": "O",
				"rhesus": "Negative",
				"pregnancy": preg.name,
			},
		)
		for key in ("triage", "reception"):  # cannot read ANC Pregnancy
			with self.subTest(user=key), self.as_user(key):
				self.assertEqual(anc.admit_prefill(self.mother.name), {})

	def test_role_gates(self):
		for key in ("triage", "reception", "mho", "his"):
			with self.subTest(user=key), self.assertRaises(frappe.PermissionError):
				self.book(user=key)
		preg = self.book(user="mic")  # Medical In-Charge records
		self.assertTrue(preg.name)
		for key, read, write in (
			("his", True, False),
			("mho", True, False),
			("triage", False, False),
			("reception", False, False),
			("clinician", True, True),
		):
			with self.subTest(user=key):
				self.assertEqual(
					bool(frappe.has_permission(PREG, "read", preg.name, user=self.users[key])), read
				)
				self.assertEqual(
					bool(frappe.has_permission(PREG, "write", preg.name, user=self.users[key])), write
				)
		with self.as_user("his"):
			doc = frappe.get_doc(PREG, preg.name)
			doc.remarks = "T- HIS edit"
			with self.assertRaises(frappe.PermissionError):
				doc.save()

	def test_open_writes_access_log(self):
		from frappe.desk.form.load import getdoc

		preg = self.book()
		with self.as_user("his"):
			getdoc(PREG, preg.name)
		rows = frappe.get_all(
			"Access Log Entry",
			filters={"reference_doctype": PREG, "reference_name": preg.name, "user": self.users["his"]},
			fields=["action", "patient"],
			ignore_permissions=True,
		)
		self.assertEqual([(r.action, r.patient) for r in rows], [("Open", self.mother.name)])


# ------------------------------------------------------------------------------------------ contact


class TestContact(ANCCase):
	def test_needs_open_pregnancy(self):
		with self.assertRaises(MCHRecordError) as cm:
			self.contact()
		self.assertIn("Book the pregnancy first", str(cm.exception))

	def test_male_refused(self):
		man = make_patient(sex="Male")
		with self.assertRaises(MCHRecordError):
			self.contact(patient=man.name)

	def test_numbering_and_gestation(self):
		preg = self.book(booking_date=add_days(today(), -14))  # LMP 140 days ago, booked 2 weeks ago
		c1 = self.contact(contact_date=add_days(today(), -14))
		self.assertEqual((c1.pregnancy, c1.contact_number, c1.gestation), (preg.name, 1, "18+0 weeks"))
		self.assertEqual(c1.facility, get_facility(0))
		self.assertEqual(c1.recorded_by, self.users["midwife"])
		self.submit(c1)
		c2 = self.contact(contact_date=add_days(today(), -3))  # 137 days = 19+4
		self.assertEqual((c2.contact_number, c2.gestation), (2, "19+4 weeks"))
		# The client cannot move a contact to another pregnancy or renumber it.
		with self.as_user("midwife"):
			doc = frappe.get_doc(CONTACT, c2.name)
			doc.contact_number, doc.pregnancy = 7, None
			doc.save()
			self.assertEqual((doc.pregnancy, doc.contact_number), (preg.name, 2))
		# An archived contact frees its number: the next contact is number 2 again.
		with self.as_user("mic"):
			common.archive(CONTACT, c2.name, "T- wrong visit")
		c3 = self.contact()
		self.assertEqual((c3.contact_number, c3.gestation), (2, "20+0 weeks"))
		self.submit(c3)
		self.assertEqual(self.contact().contact_number, 3)

	def test_numbers_per_pregnancy(self):
		first = self.book()
		self.contact()
		with self.as_user("midwife"):
			anc.close_pregnancy(first.name, c.PREGNANCY_ENDED)
		second = self.book(lmp=add_days(today(), -60))
		c1 = self.contact()
		self.assertEqual((c1.pregnancy, c1.contact_number, c1.gestation), (second.name, 1, "8+4 weeks"))

	def test_dates(self):
		self.book(booking_date=add_days(today(), -7))
		with self.assertRaises(ImplausibleANCValueError):
			self.contact(contact_date=add_days(today(), 1))  # future
		with self.assertRaises(ImplausibleANCValueError):
			self.contact(contact_date=add_days(today(), -8))  # before booking
		self.assertTrue(self.contact(contact_date=add_days(today(), -7)).name)

	def test_ranges(self):
		self.book()
		bad = (
			{"bp_systolic": 260, "bp_diastolic": 90},
			{"bp_systolic": 120, "bp_diastolic": 25},
			{"bp_systolic": 90, "bp_diastolic": 90},  # systolic must be above diastolic
			{"bp_systolic": 120},  # both or neither
			{"foetal_heart_rate": 55},
			{"foetal_heart_rate": 230},
			{"weight_kg": 24},
			{"weight_kg": 201},
			{"fundal_height_cm": 4},
			{"fundal_height_cm": 51},
			{"haemoglobin": 1.9},
			{"haemoglobin": 20.5},
		)
		for kw in bad:
			with self.subTest(kw=kw), self.assertRaises(ImplausibleANCValueError):
				self.contact(**kw)
		ok = self.contact(
			bp_systolic=250,
			bp_diastolic=150,
			foetal_heart_rate=140,
			weight_kg=62.5,
			fundal_height_cm=20,
			haemoglobin=11,
		)
		self.assertTrue(ok.name)
		zero = self.contact(bp_systolic=0, bp_diastolic=0, weight_kg=0)  # 0 = not recorded
		self.assertTrue(zero.name)

	def test_visit_rules(self):
		self.book()
		visit = make_next_visit(self.mother.name)
		first = self.contact(visit=visit.name)
		with self.assertRaises(MCHRecordError):
			self.contact(visit=visit.name)  # one per visit
		other = make_next_visit(make_mother().name)
		with self.assertRaises(MCHRecordError):
			self.contact(visit=other.name)  # another patient's visit
		closed = make_next_visit(self.mother.name)
		frappe.db.set_value(c.VISIT_DOCTYPE, closed.name, "status", c.VISIT_STATUS_DISCHARGED)
		with self.assertRaises(MCHRecordError):
			self.contact(visit=closed.name)  # a closed visit on insert
		# A draft made during the visit may be submitted after the visit closes.
		frappe.db.set_value(c.VISIT_DOCTYPE, visit.name, "status", c.VISIT_STATUS_DISCHARGED)
		self.assertEqual(self.submit(first).docstatus, 1)

	def test_submit_cancel_edit_addendum(self):
		self.book()
		doc = self.submit(self.contact(weight_kg=60))
		with self.as_user("midwife"):
			doc = frappe.get_doc(CONTACT, doc.name)
			with self.assertRaises(frappe.PermissionError):
				doc.cancel()
			doc = frappe.get_doc(CONTACT, doc.name)
			doc.weight_kg = 61
			with self.assertRaises(frappe.ValidationError):
				doc.save()
			doc = frappe.get_doc(CONTACT, doc.name)
			doc.addendum = "T- typed"
			with self.assertRaises(frappe.ValidationError):
				doc.save()  # the addendum only grows through add_addendum
			out = common.add_addendum(CONTACT, doc.name, "T- weight was 61 kg")
		self.assertIn("T- weight was 61 kg", out["addendum"])
		for key in ("his", "triage"):
			with self.subTest(user=key), self.as_user(key), self.assertRaises(frappe.PermissionError):
				common.add_addendum(CONTACT, doc.name, "T- no")

	def test_archive_only_by_mic(self):
		self.book()
		doc = self.submit(self.contact())
		for key in ("midwife", "clinician", "his"):
			with self.subTest(user=key), self.as_user(key), self.assertRaises(frappe.PermissionError):
				common.archive(CONTACT, doc.name, "T- error")
		with self.as_user("mic"):
			common.archive(CONTACT, doc.name, "T- wrong patient")
		self.assertEqual(frappe.db.get_value(CONTACT, doc.name, "krcs_archived"), 1)
		# An archived draft cannot be submitted.
		draft = self.contact()
		with self.as_user("mic"):
			common.archive(CONTACT, draft.name, "T- duplicate")
		with self.assertRaises(MCHRecordError):
			self.submit(draft)

	def test_submit_needs_open_pregnancy(self):
		preg = self.book()
		draft = self.contact()
		with self.as_user("midwife"):
			anc.close_pregnancy(preg.name, c.PREGNANCY_ENDED)
		with self.assertRaises(MCHRecordError):
			self.submit(draft)

	def test_role_gates(self):
		self.book()
		for key in ("triage", "reception", "mho", "his"):
			with self.subTest(user=key), self.assertRaises(frappe.PermissionError):
				self.contact(user=key)
		doc = self.contact(user="mic")
		self.assertEqual(self.submit(doc, user="mic").docstatus, 1)  # Medical In-Charge records and submits
		for key, read in (("his", True), ("mho", True), ("triage", False), ("reception", False)):
			with self.subTest(user=key):
				self.assertEqual(
					bool(frappe.has_permission(CONTACT, "read", doc.name, user=self.users[key])), read
				)
		draft = self.contact()
		with self.as_user("his"), self.assertRaises(frappe.PermissionError):
			frappe.get_doc(CONTACT, draft.name).submit()

	def test_open_writes_access_log(self):
		from frappe.desk.form.load import getdoc

		self.book()
		doc = self.contact()
		with self.as_user("clinician"):
			getdoc(CONTACT, doc.name)
		rows = frappe.get_all(
			"Access Log Entry",
			filters={"reference_doctype": CONTACT, "reference_name": doc.name},
			fields=["action", "user", "patient"],
			ignore_permissions=True,
		)
		self.assertEqual(
			[(r.action, r.user, r.patient) for r in rows],
			[("Open", self.users["clinician"], self.mother.name)],
		)


# ------------------------------------------------------------------------------------------ delivery


class TestCloseOnDelivery(ANCCase):
	def test_delivery_closes_pregnancy(self):
		preg = self.book(booking_date=add_days(today(), -10))  # booked before the delivery (yesterday)
		draft = self.contact()
		dr = make_delivery(self.mother.name, user=self.users["midwife"], days_ago=1)
		self.assertEqual(dr.docstatus, 1)
		preg.reload()
		self.assertEqual(
			(preg.status, preg.closed_reason, getdate(preg.closed_on), preg.delivery_record, preg.closed_by),
			(
				c.PREGNANCY_CLOSED,
				c.PREGNANCY_DELIVERED,
				getdate(dr.delivered_at),
				dr.name,
				self.users["midwife"],
			),
		)
		self.assertIsNone(anc.open_pregnancy(self.mother.name))
		with self.assertRaises(MCHRecordError):
			self.submit(draft)  # the pregnancy is closed
		with self.assertRaises(MCHRecordError):
			self.contact()  # no open pregnancy any more
		# A new pregnancy can be booked after the delivery.
		self.assertTrue(self.book(lmp=add_days(today(), -1)).name)

	def test_no_pregnancy_nothing_happens(self):
		dr = make_delivery(self.mother.name, user=self.users["midwife"])
		self.assertEqual(dr.docstatus, 1)
		self.assertEqual(frappe.db.count(PREG, {"patient": self.mother.name}), 0)
		# Idempotent: running the hook again closes nothing and raises nothing.
		anc.close_on_delivery(frappe.get_doc(c.DELIVERY_RECORD_DOCTYPE, dr.name))

	def test_other_mothers_pregnancy_untouched(self):
		other = make_pregnancy()
		make_delivery(self.mother.name, user=self.users["midwife"])
		self.assertEqual(frappe.db.get_value(PREG, other.name, "status"), c.PREGNANCY_OPEN)

	def test_failure_never_fails_the_delivery(self):
		preg = self.book()

		def half_close(doc):
			# Writes, then fails: the write must be rolled back to the savepoint, the delivery kept.
			frappe.db.set_value(PREG, preg.name, "status", c.PREGNANCY_CLOSED)
			frappe.throw("T- boom")

		errors_before = frappe.db.count("Error Log")
		with patch.object(anc, "_close_on_delivery", side_effect=half_close):
			frappe.clear_messages()
			dr = make_delivery(self.mother.name, user=self.users["midwife"])
			messages = " ".join(str(m) for m in frappe.get_message_log())
		self.assertEqual(frappe.db.get_value(c.DELIVERY_RECORD_DOCTYPE, dr.name, "docstatus"), 1)
		self.assertEqual(
			frappe.db.get_value(c.MATERNITY_ADMISSION_DOCTYPE, dr.admission, "status"), c.ADMISSION_DELIVERED
		)
		self.assertEqual(frappe.db.get_value(PREG, preg.name, "status"), c.PREGNANCY_OPEN)
		self.assertNotIn("T- boom", messages)
		self.assertEqual(frappe.db.count("Error Log"), errors_before + 1)

	def test_superseding_delivery_repoints_pregnancy(self):
		from onerc_healthcare.maternity import delivery

		preg = self.book()
		dr = make_delivery(self.mother.name, user=self.users["midwife"])
		with self.as_user("mic"):
			new_name = delivery.supersede(dr.name, "T- wrong mode")["name"]
		later = self.book(
			lmp=add_days(today(), -1)
		)  # booked after the delivery: never closed by the correction
		with self.as_user("midwife"):
			frappe.get_doc(c.DELIVERY_RECORD_DOCTYPE, new_name).submit()
		preg.reload()
		self.assertEqual((preg.status, preg.delivery_record), (c.PREGNANCY_CLOSED, new_name))
		self.assertEqual(frappe.db.get_value(PREG, later.name, "status"), c.PREGNANCY_OPEN)

	def test_pregnancy_booked_after_the_delivery_stays_open(self):
		# A delivery 3 days ago recorded late; the pregnancy was booked today, so it is a later one.
		preg = self.book(lmp=add_days(today(), -1))
		make_delivery(self.mother.name, user=self.users["midwife"], days_ago=3)
		self.assertEqual(frappe.db.get_value(PREG, preg.name, "status"), c.PREGNANCY_OPEN)
