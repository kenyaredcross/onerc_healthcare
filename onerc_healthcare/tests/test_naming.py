# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Record ID series per operation, configured in EMR Naming Settings (D19, BR-8 / C-7)."""

import frappe

from onerc_healthcare.naming import (
	DEFAULT_RULES,
	PROTECTED_OPTIONS,
	SETTINGS_DOCTYPE,
	current_series,
	preview,
	resolve,
	supports_series,
)
from onerc_healthcare.setup.operations import get_company_abbr
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import make_patient


class TestNamingSettings(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.settings = frappe.get_single(SETTINGS_DOCTYPE)
		self.abbr = get_company_abbr()

	def test_seeded_with_operation_prefix_and_defaults(self):
		"""The site's ID prefix is its operation abbreviation; every supported default rule is seeded."""
		self.assertEqual(self.settings.id_prefix, self.abbr)
		seeded = {r.document_type for r in self.settings.rules}
		for doctype, _series in DEFAULT_RULES:
			if supports_series(doctype):
				self.assertIn(doctype, seeded)
		self.assertIn("Patient Visit", seeded)

	def test_every_rule_is_applied(self):
		"""Each enabled rule is the doctype's default series and its only option (plus protected ones)."""
		for row in self.settings.rules:
			if not row.enabled:
				continue
			resolved = resolve(row.series, self.settings.id_prefix)
			self.assertEqual(current_series(row.document_type), resolved, row.document_type)
			options = frappe.get_meta(row.document_type).get_field("naming_series").options.split("\n")
			self.assertEqual(options[0], resolved)
			self.assertEqual(set(options[1:]), set(PROTECTED_OPTIONS.get(row.document_type, ())))
			self.assertTrue(row.example.startswith(f"{self.abbr}-"), row.example)

	def test_new_patient_id_carries_prefix(self):
		"""A new Patient is named <prefix>-PAT-<year>-<counter>; Marley's ER- series stays valid."""
		patient = make_patient()
		self.assertRegex(patient.name, rf"^{self.abbr}-PAT-\d{{4}}-\d{{5}}$")
		self.assertIn("ER-.YYYY.-", frappe.get_meta("Patient").get_field("naming_series").options)

	def test_changing_a_rule_applies_on_save(self):
		# The savepoint rollback restores the property setters but not the cached meta; cleanups run
		# after tearDown's rollback, so clear the cache then.
		self.addCleanup(frappe.clear_cache, doctype="Vital Signs")
		row = next(r for r in self.settings.rules if r.document_type == "Vital Signs")
		row.series = "{prefix}-VIT-.YYYY.-"
		self.settings.save()
		self.assertEqual(current_series("Vital Signs"), f"{self.abbr}-VIT-.YYYY.-")
		self.assertTrue(row.example.startswith(f"{self.abbr}-VIT-"))

	def test_invalid_prefix_refused(self):
		for bad in ("K", "klb!", "TOOLONGPREFIX", "KL B"):
			self.settings.id_prefix = bad
			self.assertRaises(frappe.ValidationError, self.settings.save)

	def test_lowercase_prefix_is_upper_cased(self):
		self.settings.id_prefix = self.abbr.lower()
		self.settings.save()
		self.assertEqual(self.settings.id_prefix, self.abbr)

	def test_shared_series_refused(self):
		"""Two record types may not share one series (and counter)."""
		rows = [r for r in self.settings.rules if r.enabled][:2]
		rows[1].series = rows[0].series
		self.assertRaises(frappe.ValidationError, self.settings.save)

	def test_duplicate_record_type_refused(self):
		self.settings.append(
			"rules", {"document_type": "Patient", "series": "{prefix}-P2-.YYYY.-", "enabled": 1}
		)
		self.assertRaises(frappe.ValidationError, self.settings.save)

	def test_record_type_without_series_refused(self):
		"""Record types not named by a naming series (e.g. Clinical Note) cannot be configured here."""
		self.settings.append("rules", {"document_type": "Clinical Note", "series": "{prefix}-CN-.YYYY.-"})
		self.assertRaises(frappe.ValidationError, self.settings.save)

	def test_bad_placeholder_refused(self):
		row = self.settings.rules[0]
		row.series = "{facility}-PAT-.YYYY.-"
		self.assertRaises(frappe.ValidationError, self.settings.save)

	def test_preview(self):
		self.assertRegex(preview("{prefix}-ENC-.YYYY.-", "KLB"), r"^KLB-ENC-\d{4}-00001$")
