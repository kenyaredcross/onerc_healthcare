# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Opening the encounter form works for every role that reads it: Marley's form calls
`get_encounter_details`, which lists the patient's Medication Requests, Service Requests and Clinical Notes
with the user's permissions (found by the user: Start Consultation as a Clinical Officer failed on Clinical Note)."""

import json

import frappe
from healthcare.healthcare.doctype.patient_encounter.patient_encounter import get_encounter_details

from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import as_user, routed_visit, start, users


class TestEncounterFormSummary(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.users = users()
		visit = routed_visit()
		self.patient = visit.patient
		self.encounter = start(visit.name, self.users["clinician"])

	def test_roles_that_read_the_encounter_can_open_its_summary(self):
		payload = json.dumps({"doctype": "Patient Encounter", "patient": self.patient})
		for key in ("clinician", "mho", "his", "mic"):
			with as_user(self.users[key]):
				self.assertTrue(frappe.has_permission("Patient Encounter", "read", doc=self.encounter), key)
				_meds, _orders, notes, _codes = get_encounter_details(payload)
				self.assertIsInstance(notes, list, key)

	def test_clinical_note_is_read_only(self):
		for key in ("clinician", "mho", "his", "mic"):
			with as_user(self.users[key]):
				self.assertTrue(frappe.has_permission("Clinical Note", "read"), key)
				self.assertFalse(frappe.has_permission("Clinical Note", "create"), key)
				self.assertFalse(frappe.has_permission("Clinical Note", "write"), key)
