# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Integrator: MCH form entry points (`mch.forms`): which MCH buttons a visit or a consultation offers, and the
Patient form summary. Never commit."""

from __future__ import annotations

import frappe

from onerc_healthcare import constants as c
from onerc_healthcare.mch import forms
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import make_patient
from onerc_healthcare.tests.mch_factories import (
	make_child,
	make_mch_visit,
	make_mother,
	make_pregnancy,
	make_record,
	mch_users,
)


def _by_doctype(result: dict) -> dict:
	return {a["doctype"]: a for a in result["actions"]}


class TestVisitActions(EMRTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users = mch_users()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_child_gets_immunisation_growth_and_baby_pnc(self):
		child = make_child(age_days=60)
		visit = make_mch_visit(child.name)
		frappe.set_user(self.users["clinician"])
		actions = _by_doctype(forms.visit_actions(visit.name))
		self.assertEqual(
			set(actions), {c.PNC_VISIT_DOCTYPE, c.IMMUNISATION_DOCTYPE, c.GROWTH_MONITORING_DOCTYPE}
		)
		self.assertEqual(actions[c.PNC_VISIT_DOCTYPE]["defaults"]["subject"], c.PNC_SUBJECT_BABY)
		self.assertEqual(actions[c.IMMUNISATION_DOCTYPE]["action"], "new")
		self.assertEqual(
			actions[c.IMMUNISATION_DOCTYPE]["defaults"], {"patient": child.name, "visit": visit.name}
		)

	def test_older_child_gets_no_pnc(self):
		child = make_child(age_days=500)
		visit = make_mch_visit(child.name)
		frappe.set_user(self.users["clinician"])
		self.assertEqual(
			set(_by_doctype(forms.visit_actions(visit.name))),
			{c.IMMUNISATION_DOCTYPE, c.GROWTH_MONITORING_DOCTYPE},
		)

	def test_woman_without_pregnancy_is_offered_booking(self):
		mother = make_mother()
		visit = make_mch_visit(mother.name)
		frappe.set_user(self.users["midwife"])
		actions = _by_doctype(forms.visit_actions(visit.name))
		self.assertEqual(set(actions), {c.ANC_CONTACT_DOCTYPE, c.PNC_VISIT_DOCTYPE})
		anc = actions[c.ANC_CONTACT_DOCTYPE]
		self.assertEqual(anc["action"], "book")
		self.assertEqual(anc["defaults"]["booking_visit"], visit.name)
		self.assertEqual(actions[c.PNC_VISIT_DOCTYPE]["defaults"]["subject"], c.PNC_SUBJECT_MOTHER)

	def test_woman_with_open_pregnancy_gets_new_contact(self):
		mother = make_mother()
		make_pregnancy(mother.name)
		visit = make_mch_visit(mother.name)
		frappe.set_user(self.users["clinician"])
		self.assertEqual(_by_doctype(forms.visit_actions(visit.name))[c.ANC_CONTACT_DOCTYPE]["action"], "new")

	def test_man_gets_nothing(self):
		man = make_patient(sex="Male")
		visit = make_mch_visit(man.name)
		frappe.set_user(self.users["clinician"])
		self.assertEqual(forms.visit_actions(visit.name)["actions"], [])

	def test_existing_record_is_opened(self):
		child = make_child(age_days=200)
		visit = make_mch_visit(child.name)
		rec = make_record(c.GROWTH_MONITORING_DOCTYPE, child.name, visit=visit.name, weight_kg=7.5)
		frappe.set_user(self.users["triage"])
		action = _by_doctype(forms.visit_actions(visit.name))[c.GROWTH_MONITORING_DOCTYPE]
		self.assertEqual((action["action"], action["existing"]), ("open", rec.name))

	def test_triage_nurse_gets_child_records_only(self):
		child = make_child(age_days=60)
		visit = make_mch_visit(child.name)
		frappe.set_user(self.users["triage"])
		self.assertEqual(
			set(_by_doctype(forms.visit_actions(visit.name))),
			{c.IMMUNISATION_DOCTYPE, c.GROWTH_MONITORING_DOCTYPE},
		)

	def test_readers_get_no_new_buttons(self):
		child = make_child(age_days=60)
		visit = make_mch_visit(child.name)
		frappe.set_user(self.users["his"])
		self.assertEqual(forms.visit_actions(visit.name)["actions"], [])

	def test_closed_visit_gets_nothing(self):
		child = make_child(age_days=60)
		visit = make_mch_visit(child.name)
		frappe.db.set_value(c.VISIT_DOCTYPE, visit.name, "status", c.VISIT_STATUS_DISCHARGED)
		frappe.set_user(self.users["clinician"])
		self.assertEqual(forms.visit_actions(visit.name)["actions"], [])

	def test_receptionist_gets_no_mch_buttons(self):
		child = make_child(age_days=60)
		visit = make_mch_visit(child.name)
		frappe.set_user(self.users["reception"])
		# The receptionist reads visits but may create no MCH record.
		self.assertEqual(forms.visit_actions(visit.name)["actions"], [])


class TestPatientSummary(EMRTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users = mch_users()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_mother_summary_shows_open_pregnancy(self):
		mother = make_mother()
		preg = make_pregnancy(mother.name)
		frappe.set_user(self.users["midwife"])
		s = forms.patient_summary(mother.name)
		self.assertEqual(s["pregnancy"]["name"], preg.name)
		self.assertFalse(s["is_child"])

	def test_child_summary_counts_submitted_records(self):
		child = make_child(age_days=200)
		make_record(c.GROWTH_MONITORING_DOCTYPE, child.name, weight_kg=7.5, submit=True)
		frappe.set_user(self.users["triage"])
		s = forms.patient_summary(child.name)
		self.assertTrue(s["is_child"])
		self.assertEqual(s["counts"], {c.GROWTH_MONITORING_DOCTYPE: 1})
		self.assertIsNone(s["pregnancy"])
