# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Start Visit from the Patient form (visit/patient.js): the receptionist inserts the visit through
`frappe.client.insert` with only the start-of-visit answers, and every server rule still applies."""

import frappe
from frappe.client import insert

from onerc_healthcare.constants import VISIT_DOCTYPE
from onerc_healthcare.emr_visit.doctype.patient_visit.patient_visit import OpenVisitExistsError
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import as_user, users
from onerc_healthcare.tests.factories import get_facility, make_patient
from onerc_healthcare.visit.queue import get_open_visit


class TestStartVisitFromPatient(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.users = users()
		self.patient = make_patient().name

	def start(self, user=None, **kw):
		with as_user(user or self.users["reception"]):
			return insert(
				{"doctype": VISIT_DOCTYPE, "patient": self.patient, "facility": get_facility(), **kw}
			)

	def test_receptionist_starts_a_visit(self):
		visit = self.start(seen_in_smartcare=1)
		self.assertEqual(visit["status"], "Registered")
		self.assertTrue(visit["queue_number"])
		self.assertEqual(visit["seen_in_smartcare"], 1)
		with as_user(self.users["reception"]):
			self.assertEqual(get_open_visit(self.patient)["name"], visit["name"])

	def test_second_open_visit_refused(self):
		self.start()
		with self.assertRaises(OpenVisitExistsError):
			self.start()

	def test_roles_without_create_refused(self):
		for key in ("clinician", "pharmacist", "lab"):
			with self.assertRaises(frappe.PermissionError, msg=key):
				self.start(user=self.users[key])
