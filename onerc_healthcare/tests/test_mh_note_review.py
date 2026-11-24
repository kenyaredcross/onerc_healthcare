# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Independent review of the Mental Health consultation note (CON-13, D63).

Each test asserts the correct behaviour and is marked `expectedFailure` while the defect stands; a fix turns
it into an unexpected success."""

from __future__ import annotations

import unittest

import frappe
from frappe.client import set_value

from onerc_healthcare.clinical import consultation
from onerc_healthcare.clinical.mh_note import MentalHealthNoteError
from onerc_healthcare.constants import DEPT_MENTAL_HEALTH, DEPT_OUTPATIENT, ENCOUNTER_DOCTYPE, ROLE_MIDWIFE
from onerc_healthcare.privacy import access_log
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import add_diagnosis, as_user, routed_visit, start, users
from onerc_healthcare.tests.factories import make_user

HIGH_RISK_NOTE = {
	"krcs_mh_suicide_risk": "High",
	"krcs_mh_harm_to_others": "None identified",
	"krcs_mh_mood": "T- low",
	"krcs_mh_risk_notes": "T- plans described",
}


class MentalHealthReviewCase(EMRTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users = users()

	def mh_encounter(self, **note) -> str:
		visit = routed_visit(DEPT_MENTAL_HEALTH)
		enc = start(visit.name, self.users["mho"])
		add_diagnosis(enc)
		if note:
			doc = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
			doc.update(note)
			doc.save(ignore_permissions=True)
		return enc

	def change_department(self, enc: str, department: str = DEPT_OUTPATIENT) -> bool:
		"""The MHO sets the read-only `krcs_department` through the whitelisted frappe.client.set_value.
		Returns True when the server refuses the change."""
		with as_user(self.users["mho"]):
			try:
				set_value(ENCOUNTER_DOCTYPE, enc, "krcs_department", department)
			except frappe.ValidationError:
				return True
		return False


class TestDepartmentChange(MentalHealthReviewCase):
	# DEFECT-MH-1: `krcs_department` is read_only only in the desk form. A consulting user can change it on a
	# draft Mental Health encounter through the API; `mh_note.validate` then wipes the whole note and
	# `assert_complete` no longer applies, so a High suicide risk completes without a safety plan.
	def test_department_change_cannot_bypass_note(self):
		enc = self.mh_encounter(**HIGH_RISK_NOTE)
		if self.change_department(enc):
			return  # refusing the change is a correct fix
		row = frappe.db.get_value(
			ENCOUNTER_DOCTYPE, enc, ["krcs_mh_suicide_risk", "krcs_mh_risk_notes"], as_dict=True
		)
		with self.subTest("note kept"):
			self.assertEqual(row.krcs_mh_suicide_risk, "High", "the risk assessment was wiped")
			self.assertEqual(row.krcs_mh_risk_notes, "T- plans described", "clinical notes were wiped")
		with self.subTest("completion refused"), as_user(self.users["mho"]):
			with self.assertRaises(MentalHealthNoteError, msg="completed with no risk assessment"):
				consultation.complete_consultation(enc)
			self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "docstatus"), 0)

	# DEFECT-MH-1 (sensitivity): the same change also moves `medical_department` (its fetch_from
	# practitioner.department blanks it on every draft save and `encounter_before_validate` refills it from
	# krcs_department), so the Mental Health consultation is no longer sensitive for the access log and is
	# readable by a Midwife-only user.
	def test_department_change_keeps_sensitivity(self):
		enc = self.mh_encounter(**HIGH_RISK_NOTE)
		if self.change_department(enc):
			return
		with self.subTest("access log"):
			self.assertTrue(access_log.is_sensitive(ENCOUNTER_DOCTYPE, enc), "no longer logged as sensitive")
		midwife = make_user("mh-review-midwife", [ROLE_MIDWIFE])
		with self.subTest("midwife"), as_user(midwife):
			self.assertFalse(frappe.has_permission(ENCOUNTER_DOCTYPE, "read", doc=enc), "Midwife can read it")


class TestScoreInput(MentalHealthReviewCase):
	# DEFECT-MH-2: `normalise_score` runs in the validate hook, before Frappe's length check. A digit string
	# longer than Python's int-conversion limit (4300 digits) raises a bare ValueError (HTTP 500) instead of
	# the MentalHealthNoteError message.
	def test_oversized_score_is_a_validation_error(self):
		enc = self.mh_encounter()
		doc = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
		doc.krcs_mh_phq9 = "9" * 5000
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)


class TestPrint(MentalHealthReviewCase):
	# DEFECT-MH-3: D63 says the note does not show in print formats yet. Patient Encounter has no default
	# print format, so the default (Standard) print of a completed Mental Health consultation contains the
	# whole note, safety plan included, for every role with print (CO, MHO, HIS Officer, MIC).
	def test_note_not_in_default_print(self):
		enc = self.mh_encounter(**HIGH_RISK_NOTE, krcs_mh_safety_plan="T- SAFETYPLANMARKER")
		with as_user(self.users["mho"]):
			consultation.complete_consultation(enc)
		with as_user(self.users["mic"]):
			html = frappe.get_print(ENCOUNTER_DOCTYPE, enc)
		self.assertNotIn("SAFETYPLANMARKER", html)
