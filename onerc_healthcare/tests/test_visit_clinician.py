# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""The visit shows its consultation's clinician (Patient Visit list column): set with the encounter at Start
Consultation, protected afterwards, back-filled for older visits."""

import frappe

from onerc_healthcare.constants import VISIT_DOCTYPE
from onerc_healthcare.emr_visit.doctype.patient_visit.patient_visit import VisitStatusLockedError
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import routed_visit, start, users
from onerc_healthcare.visit import engine


class TestVisitClinician(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.users = users()
		self.visit = routed_visit().name
		self.encounter = start(self.visit, self.users["clinician"])

	def test_start_consultation_sets_clinician(self):
		practitioner, name = frappe.db.get_value(
			"Patient Encounter", self.encounter, ["practitioner", "practitioner_name"]
		)
		row = frappe.db.get_value(
			VISIT_DOCTYPE, self.visit, ["practitioner", "practitioner_name"], as_dict=True
		)
		self.assertTrue(practitioner)
		self.assertEqual((row.practitioner, row.practitioner_name), (practitioner, name))
		self.assertTrue(frappe.get_meta(VISIT_DOCTYPE).get_field("practitioner_name").in_list_view)

	def test_clinician_cannot_be_edited_by_hand(self):
		doc = frappe.get_doc(VISIT_DOCTYPE, self.visit)
		doc.practitioner_name = "T- someone else"
		with self.assertRaises(VisitStatusLockedError):
			doc.save(ignore_permissions=True)

	def test_backfill(self):
		frappe.db.set_value(VISIT_DOCTYPE, self.visit, {"practitioner": None, "practitioner_name": None})
		self.assertEqual(engine.backfill_practitioners(skip=[self.visit]), 0)
		self.assertGreaterEqual(engine.backfill_practitioners(), 1)
		self.assertTrue(frappe.db.get_value(VISIT_DOCTYPE, self.visit, "practitioner"))
