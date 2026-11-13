# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""W0 install and seeding checks (BR-3, BR-8, C-5, C-7; SDD v0.1 §3.2)."""

import frappe

from onerc_healthcare.constants import DEPARTMENTS, OPERATIONS
from onerc_healthcare.setup.install import HEALTHCARE_SETTINGS, seed_operation
from onerc_healthcare.setup.operations import (
	get_facilities,
	get_operation,
	get_operation_config,
	lab_unit_name,
	load_data,
)
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import make_patient


class TestInstall(EMRTestCase):
	def test_site_has_an_operation(self):
		"""BR-8 / C-7: every EMR site is bound to exactly one operation."""
		self.assertIn(get_operation(), OPERATIONS)
		self.assertTrue(frappe.is_setup_complete())

	def test_single_company(self):
		"""C-7: one company per site; the other operation's company does not exist here."""
		cfg = get_operation_config()
		self.assertEqual(frappe.get_all("Company", pluck="name"), [cfg["company_name"]])
		other = next(op for op in OPERATIONS if op != get_operation())
		self.assertFalse(frappe.db.exists("Company", get_operation_config(other)["company_name"]))

	def test_facilities_seeded(self):
		cfg = get_operation_config()
		for facility, name in zip(cfg["facilities"], get_facilities(), strict=True):
			unit = frappe.db.get_value(
				"Healthcare Service Unit",
				name,
				["is_group", "company", "krcs_facility_code", "krcs_county"],
				as_dict=True,
			)
			self.assertIsNotNone(unit, name)
			self.assertEqual(unit.is_group, 1)
			self.assertEqual(unit.company, cfg["company_name"])
			self.assertEqual(unit.krcs_facility_code, facility["code"])
			self.assertEqual(unit.krcs_county, cfg["county"])
			lab = frappe.db.get_value(
				"Healthcare Service Unit",
				lab_unit_name(facility, cfg["abbr"]),
				["parent_healthcare_service_unit", "service_unit_type"],
				as_dict=True,
			)
			self.assertIsNotNone(lab)
			self.assertEqual(lab.parent_healthcare_service_unit, name)
			self.assertEqual(lab.service_unit_type, "Laboratory")

	def test_departments_seeded(self):
		"""BR-3: the nine EMR departments exist."""
		for dept in DEPARTMENTS:
			self.assertTrue(frappe.db.exists("Medical Department", dept), dept)

	def test_healthcare_settings_billing_free(self):
		"""C-5: no billing behaviour is enabled."""
		settings = frappe.get_single("Healthcare Settings")
		for field, value in HEALTHCARE_SETTINGS.items():
			self.assertEqual(settings.get(field), value, field)
		self.assertFalse(settings.lab_test_approval_required)
		self.assertEqual(settings.default_intent, "Order-Intent")
		self.assertEqual(settings.default_priority, "Routine-Priority")

	def test_lab_catalogue_provisional_and_non_billable(self):
		"""C-5 / OI-9: the provisional catalogue exists and nothing in it is billable."""
		data = load_data("lab_catalogue.json")
		names = [t["name"] for t in data["single_tests"]] + [p["name"] for p in data["panels"]]
		for name in names:
			self.assertTrue(frappe.db.exists("Observation Template", name), name)
		self.assertFalse(frappe.db.exists("Observation Template", {"name": ("in", names), "is_billable": 1}))

	def test_seeding_is_idempotent(self):
		before = {
			dt: frappe.db.count(dt)
			for dt in ("Healthcare Service Unit", "Medical Department", "Observation Template", "Company")
		}
		seed_operation(get_operation())
		after = {dt: frappe.db.count(dt) for dt in before}
		self.assertEqual(before, after)

	def test_patient_factory(self):
		patient = make_patient()
		self.assertTrue(patient.name)
		self.assertTrue(patient.first_name.startswith("T-"))

	def test_roles_exist(self):
		"""SDD §6.1: all EMR roles exist."""
		from onerc_healthcare.constants import ALL_ROLES

		for role in ALL_ROLES:
			self.assertTrue(frappe.db.exists("Role", role), role)

	def test_initial_setup_recorded(self):
		"""A site installed before krcs_operation was set completes its setup on migrate."""
		from onerc_healthcare.setup.install import INITIAL_SETUP_KEY

		self.assertEqual(frappe.db.get_default(INITIAL_SETUP_KEY), get_operation())

	def test_company_deletion_ignores_clinical_doctypes(self):
		"""NFR-9: ERPNext "Delete Company Transactions" must skip every clinical DocType."""
		from onerc_healthcare.constants import CLINICAL_DOCTYPES

		ignored = set(frappe.get_hooks("company_data_to_be_ignored"))
		self.assertTrue(set(CLINICAL_DOCTYPES) <= ignored, set(CLINICAL_DOCTYPES) - ignored)
