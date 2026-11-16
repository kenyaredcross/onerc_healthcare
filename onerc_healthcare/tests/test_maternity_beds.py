# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Adding a bed (found by the user: saving a bed from the admission's bed picker showed "Healthcare Service Unit
Type None not found")."""

import frappe
from frappe.utils import random_string

from onerc_healthcare.maternity.setup import ward_for_facility
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import get_facility


class TestAddBed(EMRTestCase):
	def unit(self, parent, **kw):
		return frappe.get_doc(
			{
				"doctype": "Healthcare Service Unit",
				"healthcare_service_unit_name": f"T-Bed-{random_string(5)}",
				"parent_healthcare_service_unit": parent,
				"is_group": 0,
				**kw,
			}
		)

	def test_bed_under_maternity_ward_gets_type_and_company(self):
		ward = ward_for_facility(get_facility())
		self.assertTrue(ward)
		bed = self.unit(ward).insert()
		self.assertEqual(bed.service_unit_type, "Bed")
		self.assertEqual(bed.company, frappe.db.get_value("Healthcare Service Unit", ward, "company"))
		self.assertEqual(bed.occupancy_status, "Vacant")
		self.assertEqual(bed.inpatient_occupancy, 1)

	def test_leaf_elsewhere_without_type_gets_a_clear_message(self):
		with self.assertRaises(frappe.MandatoryError):
			self.unit(get_facility()).insert()
