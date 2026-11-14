# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""DEFECT-RX-1: a prescription's quantity is the whole course, so a pharmacist (or an outreach team) can
dispense it. Found by the Phase 4 smoke test."""

import frappe

from onerc_healthcare.pharmacy.catalogue import DEFAULT_INTERVAL, set_default_intervals
from onerc_healthcare.tests.base import EMRTestCase


class TestPrescribedQuantity(EMRTestCase):
	def test_seeded_medications_have_a_daily_interval(self):
		self.assertFalse(frappe.get_all("Medication", filters={"default_interval": ["in", [0, None]]}))

	def test_course_quantity(self):
		"""BID (2 doses a day) for 5 days = 10; once daily for 1 day = 1."""
		if not (
			frappe.db.exists("Prescription Dosage", "BID")
			and frappe.db.exists("Prescription Duration", "5 Day")
		):
			self.skipTest("BID / 5 Day not seeded")
		row = frappe.new_doc("Drug Prescription")
		row.update({"dosage": "BID", "period": "5 Day", "interval": DEFAULT_INTERVAL})
		self.assertEqual(row.get_quantity(), 10)
		if frappe.db.exists("Prescription Duration", "1 Day") and frappe.db.exists(
			"Prescription Dosage", "Once Daily"
		):
			row.update({"dosage": "Once Daily", "period": "1 Day"})
			self.assertEqual(row.get_quantity(), 1)

	def test_backfill_is_idempotent(self):
		self.assertEqual(set_default_intervals(), 0)
