# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""ICD-11 code system, importer and code search (FR-11, BR-4; SDD v0.1 §4.5)."""

import csv
import gzip
import hashlib
import os
import tempfile

import frappe
from frappe.desk.search import search_widget

from onerc_healthcare.coding.icd11 import (
	BUNDLED_FILE,
	BUNDLED_RELEASE,
	RELEASE_DEFAULT_KEY,
	import_bundled,
	import_file,
	parse_rows,
	read_rows,
)
from onerc_healthcare.coding.setup import ICD11_CODE_SYSTEM, ICD11_URI
from onerc_healthcare.constants import ROLE_CLINICIAN
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import make_user

FIXTURE = os.path.join(
	os.path.dirname(os.path.dirname(__file__)),
	"coding",
	"test_data",
	"SimpleTabulation-ICD-11-MMS-en.sample.txt",
)
# The fixture is the header plus the first 14 rows of the WHO 2025-01 release:
# 1 chapter, 2 blocks and 11 categories.
FIXTURE_ROWS = 14
FIXTURE_CODES = 11
# SHA-256 of the unmodified WHO 2025-01 SimpleTabulation .txt (also recorded in coding/data/NOTICE.md).
BUNDLED_SHA256 = "7bc6b566394b8763efccda81f99f5ff0242b8c59d9cd230a90c3f38cba36992c"


class TestICD11(EMRTestCase):
	def test_code_system_seeded(self):
		"""BR-4: ICD-11 is a code system and the default for codification."""
		self.assertEqual(frappe.db.get_value("Code System", ICD11_CODE_SYSTEM, "uri"), ICD11_URI)
		self.assertEqual(
			frappe.db.get_single_value("Healthcare Settings", "default_code_system"), ICD11_CODE_SYSTEM
		)

	def test_parse_derives_title_and_parent(self):
		"""Titles lose their depth dashes; the parent comes from the depth."""
		rows = {r["code"] or r["block"] or r["chapter"]: r for r in parse_rows(read_rows(FIXTURE))}
		self.assertEqual(len(rows), FIXTURE_ROWS)
		self.assertFalse(rows["1A00"]["title"].startswith("-"))
		self.assertEqual(rows["BlockL1-1A0"]["parent"], "Chapter 01")
		self.assertEqual(rows["BlockL2-1A0"]["parent"], "BlockL1-1A0")
		self.assertEqual(rows["1A00"]["parent"], "BlockL2-1A0")
		self.assertEqual(rows["1A03.0"]["parent"], "1A03")
		self.assertEqual(rows["1A04"]["parent"], "BlockL2-1A0")
		self.assertEqual(rows["1A03"]["is_leaf"], 0)
		self.assertEqual(rows["1A03.0"]["level"], 2)
		self.assertTrue(rows["1A00"]["browser_link"].startswith("https://icd.who.int/"))

	def test_import_is_idempotent(self):
		"""FR-11: importing the same release twice creates nothing the second time."""
		first = import_file(FIXTURE)
		self.assertEqual(first["rows"], FIXTURE_ROWS)
		self.assertEqual(first["with_code"], FIXTURE_CODES)
		self.assertEqual(first["skipped"], FIXTURE_ROWS - FIXTURE_CODES)
		self.assertEqual(first["created"] + first["updated"] + first["unchanged"], FIXTURE_CODES)

		second = import_file(FIXTURE)
		self.assertEqual(second["created"], 0)
		self.assertEqual(second["updated"], 0)
		self.assertEqual(second["unchanged"], FIXTURE_CODES)

		value = frappe.get_doc("Code Value", f"1A03.0-{ICD11_CODE_SYSTEM}")
		self.assertEqual(value.code_system, ICD11_CODE_SYSTEM)
		self.assertEqual(value.code_value, "1A03.0")
		self.assertEqual(value.krcs_parent_code, "1A03")
		self.assertEqual(value.krcs_chapter, "01")

	def test_csv_variant(self):
		"""The same columns as CSV import to the same codes."""
		import_file(FIXTURE)
		with tempfile.TemporaryDirectory() as tmp:
			path = os.path.join(tmp, "icd11.csv")
			with open(path, "w", encoding="utf-8", newline="") as f:
				rows = list(read_rows(FIXTURE))
				writer = csv.DictWriter(f, fieldnames=list(rows[0]))
				writer.writeheader()
				writer.writerows(rows)
			result = import_file(path)
		self.assertEqual(result["unchanged"], FIXTURE_CODES)

	def test_search_by_keyword_and_code(self):
		"""FR-11: a clinician finds codes by keyword or by code number."""
		import_file(FIXTURE)
		frappe.set_user(make_user("coding-clinician", [ROLE_CLINICIAN]))
		# Search the way a diagnosis picker does: chapter X holds ~17k extension codes (organisms,
		# severity, laterality) that are not diagnoses and would crowd the first page of results.
		filters = {"code_system": ICD11_CODE_SYSTEM, "krcs_chapter": ["!=", "X"]}

		def names(txt):
			return {r["name"] for r in search_widget("Code Value", txt, filters=dict(filters), as_dict=True)}

		self.assertIn(f"1A00-{ICD11_CODE_SYSTEM}", names("cholera"))
		self.assertIn(f"1A03.0-{ICD11_CODE_SYSTEM}", names("1A03"))
		self.assertIn(f"1A03.0-{ICD11_CODE_SYSTEM}", names("Enteropathogenic"))


class TestBundledICD11(EMRTestCase):
	def test_bundled_file_is_unmodified_who_release(self):
		"""D11: the bundled release is the WHO file byte for byte (CC BY-ND: no derivatives)."""
		with gzip.open(BUNDLED_FILE, "rb") as f:
			self.assertEqual(hashlib.sha256(f.read()).hexdigest(), BUNDLED_SHA256)

	def test_bundled_file_parses(self):
		"""FR-11: the full release parses: about 35k coded categories under 28 chapters."""
		rows = list(parse_rows(read_rows(BUNDLED_FILE)))
		codes = [r for r in rows if r["code"]]
		self.assertGreater(len(codes), 30000)
		self.assertTrue(all(r["title"] and not r["title"].startswith("-") for r in codes))
		self.assertTrue(any(r["code"] == "1A00" for r in codes))

	def test_bundled_release_loaded_on_install(self):
		"""FR-11: install/migrate loaded the bundled release and a re-run skips it."""
		self.assertEqual(frappe.db.get_default(RELEASE_DEFAULT_KEY), BUNDLED_RELEASE)
		self.assertGreater(frappe.db.count("Code Value", {"code_system": ICD11_CODE_SYSTEM}), 30000)
		self.assertIsNone(import_bundled())
