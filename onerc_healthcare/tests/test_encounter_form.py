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


ENCOUNTER_READERS = ("clinician", "mho", "his", "mic")


def visible_links(root: str) -> list[tuple[str, str]]:
	"""(field path, target DocType) of every visible Link / Table MultiSelect on `root` and its tables."""
	stack, seen, links = [(root, "")], {root}, []
	while stack:
		doctype, path = stack.pop()
		for df in frappe.get_meta(doctype).fields:
			if df.hidden or not df.options:
				continue
			if df.fieldtype == "Link":
				links.append((path + df.fieldname, df.options))
			elif df.fieldtype == "Table MultiSelect":
				inner = [f for f in frappe.get_meta(df.options).fields if f.fieldtype == "Link"]
				if inner:
					links.append((path + df.fieldname, inner[0].options))
			elif df.fieldtype == "Table" and df.options not in seen:
				seen.add(df.options)
				stack.append((df.options, f"{path}{df.fieldname}."))
	return links


class TestEncounterFormLinks(EMRTestCase):
	"""Found by the user clicking through: picking an ICD-11 code blanked it, and the Symptoms field failed on
	Complaint. Every link the form shows must work for the roles that open the form."""

	def setUp(self):
		super().setUp()
		self.users = users()

	def test_every_visible_link_is_readable(self):
		for field, target in visible_links("Patient Encounter"):
			for key in ENCOUNTER_READERS:
				with as_user(self.users[key]):
					self.assertTrue(
						frappe.has_permission(target, "select") or frappe.has_permission(target, "read"),
						f"{key} cannot search {target} ({field})",
					)

	def test_chosen_icd11_code_passes_link_validation(self):
		from frappe.client import validate_link_and_fetch

		code = frappe.db.get_value(
			"Code Value", {"code_system": "ICD-11", "krcs_chapter": ["!=", "X"], "krcs_is_leaf": 1}, "name"
		)
		if not code:
			self.skipTest("ICD-11 not imported")
		with as_user(self.users["clinician"]):
			result = validate_link_and_fetch(
				"Code Value",
				code,
				fields_to_fetch=["code_value", "display"],
				query="onerc_healthcare.clinical.diagnosis.search_icd11",
			)
		self.assertEqual(result.get("name"), code)
		self.assertTrue(result.get("display"))
