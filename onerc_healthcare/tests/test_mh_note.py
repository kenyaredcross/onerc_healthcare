# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Mental Health consultation note tests (FR-13, CON-13; provisional format D63, OI-7)."""

from __future__ import annotations

import frappe
from frappe.utils import add_days, today

from onerc_healthcare.clinical import consultation, mh_note
from onerc_healthcare.clinical import setup as clinical_setup
from onerc_healthcare.clinical.mh_note import MentalHealthNoteError
from onerc_healthcare.constants import (
	ACCESS_LOG_DOCTYPE,
	DEPT_MENTAL_HEALTH,
	DEPT_OUTPATIENT,
	ENCOUNTER_DOCTYPE,
	ROLE_MIDWIFE,
	VISIT_DOCTYPE,
	VISIT_STATUS_DISCHARGED,
)
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import add_diagnosis, as_user, routed_visit, start, users
from onerc_healthcare.tests.factories import make_user

# The minimum a Mental Health consultation needs to be completed.
MINIMUM_NOTE = {
	"krcs_mh_suicide_risk": "None identified",
	"krcs_mh_harm_to_others": "Low",
	"krcs_mh_mood": "T- low",
}


def fill_note(encounter: str, **values):
	doc = frappe.get_doc(ENCOUNTER_DOCTYPE, encounter)
	doc.update(values)
	doc.save(ignore_permissions=True)
	return doc


class MentalHealthNoteCase(EMRTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users = users()

	def mh_encounter(self, user: str = "mho", department: str = DEPT_MENTAL_HEALTH) -> tuple[str, str]:
		"""(visit, encounter) of a consultation started through the endpoint, with a diagnosis."""
		visit = routed_visit(department)
		encounter = start(visit.name, self.users[user])
		add_diagnosis(encounter)
		return visit.name, encounter

	def complete(self, encounter: str, user: str = "mho"):
		with as_user(self.users[user]):
			return consultation.complete_consultation(encounter)


class TestFields(MentalHealthNoteCase):
	def test_fields_exist_and_depend_on_department(self):
		meta = frappe.get_meta(ENCOUNTER_DOCTYPE)
		names = [df["fieldname"] for df in clinical_setup.MH_NOTE_FIELDS]
		self.assertTrue(all(n.startswith("krcs_mh_") for n in names))
		for name in names:
			df = meta.get_field(name)
			self.assertTrue(df, name)
			self.assertEqual(df.depends_on, "eval:doc.krcs_department=='Mental Health'", name)
			self.assertFalse(df.allow_on_submit, name)
			self.assertFalse(df.collapsible, name)
		for name in ("krcs_mh_phq9", "krcs_mh_gad7"):
			self.assertEqual(meta.get_field(name).fieldtype, "Data", name)  # blank differs from 0
		self.assertEqual(meta.get_field("krcs_mh_section").fieldtype, "Section Break")
		order = [df.fieldname for df in meta.fields]
		self.assertLess(order.index("krcs_examination_findings"), order.index("krcs_mh_section"))
		self.assertEqual(
			order[order.index("krcs_mh_section") : order.index("krcs_diagnosis_section")],
			names,
		)
		self.assertIn("High", meta.get_field("krcs_mh_suicide_risk").options.split("\n"))

	def test_install_idempotent(self):
		clinical_setup.install()
		count = frappe.db.count("Custom Field", {"dt": ENCOUNTER_DOCTYPE, "fieldname": ["like", "krcs_mh_%"]})
		clinical_setup.install()
		self.assertEqual(
			frappe.db.count("Custom Field", {"dt": ENCOUNTER_DOCTYPE, "fieldname": ["like", "krcs_mh_%"]}),
			count,
		)
		self.assertEqual(count, len(clinical_setup.MH_NOTE_FIELDS))


class TestCompletion(MentalHealthNoteCase):
	def test_completion_requires_risk_fields_and_mood(self):
		visit, enc = self.mh_encounter()
		with self.assertRaises(MentalHealthNoteError) as ctx:
			self.complete(enc)
		message = str(ctx.exception)
		for label in ("Suicide Risk", "Risk of Harm to Others", "Mood"):
			self.assertIn(label, message)
		self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "docstatus"), 0)

		fill_note(enc, krcs_mh_suicide_risk="Low", krcs_mh_harm_to_others="None identified")
		with self.assertRaises(MentalHealthNoteError) as ctx:
			self.complete(enc)
		self.assertIn("Mood", str(ctx.exception))
		self.assertNotIn("Suicide Risk", str(ctx.exception))

		fill_note(enc, krcs_mh_mood="T- low")
		out = self.complete(enc)
		self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "docstatus"), 1)
		self.assertEqual(out["visit_status"], VISIT_STATUS_DISCHARGED)
		self.assertEqual(frappe.db.get_value(VISIT_DOCTYPE, visit, "status"), VISIT_STATUS_DISCHARGED)

	def test_desk_submit_path_also_checks(self):
		"""before_submit (server-side submit with the consultation flag) applies the same rule."""
		_visit, enc = self.mh_encounter()
		doc = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
		doc.flags.krcs_via_consultation = True
		with self.assertRaises(MentalHealthNoteError):
			doc.submit()

	def test_high_risk_needs_safety_plan(self):
		for values in (
			{"krcs_mh_suicide_risk": "High"},
			{"krcs_mh_harm_to_others": "High"},
			{"krcs_mh_recent_self_harm": 1},
		):
			with self.subTest(values=values):
				_visit, enc = self.mh_encounter()
				fill_note(enc, **{**MINIMUM_NOTE, **values})
				with self.assertRaises(MentalHealthNoteError) as ctx:
					self.complete(enc)
				self.assertIn("Safety Plan", str(ctx.exception))
				fill_note(enc, krcs_mh_safety_plan="   ")
				with self.assertRaises(MentalHealthNoteError):
					self.complete(enc)
				fill_note(enc, krcs_mh_safety_plan="T- remove means; daily check-in; crisis contact")
				self.complete(enc)
				self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "docstatus"), 1)

	def test_moderate_risk_without_safety_plan_completes(self):
		_visit, enc = self.mh_encounter()
		fill_note(enc, **{**MINIMUM_NOTE, "krcs_mh_suicide_risk": "Moderate"})
		self.complete(enc)
		self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "docstatus"), 1)

	def test_clinical_officer_can_complete(self):
		"""D28: either consulting role may consult in Mental Health."""
		_visit, enc = self.mh_encounter(user="clinician")
		with as_user(self.users["clinician"]):
			doc = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
			doc.update(MINIMUM_NOTE)
			doc.save()
		self.complete(enc, user="clinician")
		self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "docstatus"), 1)

	def test_outpatient_completion_unaffected(self):
		_visit, enc = self.mh_encounter(user="clinician", department=DEPT_OUTPATIENT)
		self.complete(enc, user="clinician")
		self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "docstatus"), 1)


class TestValidate(MentalHealthNoteCase):
	def test_scores_range_and_blank_versus_zero(self):
		_visit, enc = self.mh_encounter()
		doc = fill_note(enc, krcs_mh_phq9="0", krcs_mh_gad7="")
		self.assertEqual((doc.krcs_mh_phq9, doc.krcs_mh_gad7), ("0", None))
		row = frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, ["krcs_mh_phq9", "krcs_mh_gad7"], as_dict=True)
		self.assertEqual((row.krcs_mh_phq9, row.krcs_mh_gad7), ("0", None))

		doc = fill_note(enc, krcs_mh_phq9=" 027 ", krcs_mh_gad7="21")
		self.assertEqual((doc.krcs_mh_phq9, doc.krcs_mh_gad7), ("27", "21"))
		doc = fill_note(enc, krcs_mh_phq9=7, krcs_mh_gad7="  ")
		self.assertEqual((doc.krcs_mh_phq9, doc.krcs_mh_gad7), ("7", None))

		for field, bad in (
			("krcs_mh_phq9", "28"),
			("krcs_mh_phq9", "-1"),
			("krcs_mh_phq9", "5.5"),
			("krcs_mh_phq9", "ten"),
			("krcs_mh_gad7", "22"),
			("krcs_mh_gad7", "1e1"),
		):
			with self.subTest(field=field, value=bad), self.assertRaises(MentalHealthNoteError):
				fill_note(enc, **{field: bad})

	def test_score_band(self):
		self.assertEqual(mh_note.score_band("krcs_mh_phq9", "0"), "Minimal")
		self.assertEqual(mh_note.score_band("krcs_mh_phq9", "14"), "Moderate")
		self.assertEqual(mh_note.score_band("krcs_mh_phq9", "19"), "Moderately severe")
		self.assertEqual(mh_note.score_band("krcs_mh_phq9", "27"), "Severe")
		self.assertEqual(mh_note.score_band("krcs_mh_gad7", "15"), "Severe")
		self.assertIsNone(mh_note.score_band("krcs_mh_gad7", None))
		self.assertIsNone(mh_note.score_band("krcs_mh_gad7", ""))

	def test_follow_up_date_not_in_past(self):
		_visit, enc = self.mh_encounter()
		with self.assertRaises(MentalHealthNoteError):
			fill_note(enc, krcs_mh_follow_up_date=add_days(today(), -1))
		doc = fill_note(enc, krcs_mh_follow_up_date=today())
		self.assertEqual(str(doc.krcs_mh_follow_up_date), today())
		fill_note(enc, krcs_mh_follow_up_date=add_days(today(), 14))
		# A draft whose follow-up date has since passed is refused at completion.
		frappe.db.set_value(ENCOUNTER_DOCTYPE, enc, "krcs_mh_follow_up_date", add_days(today(), -2))
		fill_note(enc, krcs_mh_mood="T- unchanged date is not re-checked on a draft save")
		fill_note(enc, **MINIMUM_NOTE)
		with self.assertRaises(MentalHealthNoteError):
			self.complete(enc)
		fill_note(enc, krcs_mh_follow_up_date=add_days(today(), 7))
		self.complete(enc)

	def test_note_cleared_on_other_department(self):
		_visit, enc = self.mh_encounter(user="clinician", department=DEPT_OUTPATIENT)
		values = {f: "T- x" for f in ("krcs_mh_mood", "krcs_mh_risk_notes", "krcs_mh_formulation")}
		values.update(
			krcs_mh_suicide_risk="High",
			krcs_mh_recent_self_harm=1,
			krcs_mh_phq9="12",
			krcs_mh_follow_up_date=add_days(today(), 3),
		)
		doc = fill_note(enc, **values)
		stored = frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, list(mh_note.NOTE_FIELDS), as_dict=True)
		for field in mh_note.NOTE_FIELDS:
			self.assertFalse(stored[field], field)
			self.assertFalse(doc.get(field), field)
		self.assertIn("krcs_mh_follow_up_date", mh_note.NOTE_FIELDS)
		self.assertNotIn("krcs_mh_section", mh_note.NOTE_FIELDS)


class TestAfterCompletion(MentalHealthNoteCase):
	def completed(self) -> str:
		_visit, enc = self.mh_encounter()
		fill_note(enc, **MINIMUM_NOTE)
		self.complete(enc)
		return enc

	def test_edit_refused_addendum_works(self):
		enc = self.completed()
		with as_user(self.users["mho"]):
			doc = frappe.get_doc(ENCOUNTER_DOCTYPE, enc)
			doc.krcs_mh_mood = "T- changed"
			with self.assertRaises(frappe.UpdateAfterSubmitError):
				doc.save()
			out = consultation.add_addendum(enc, "T- mood reassessed: euthymic")
		self.assertIn("T- mood reassessed", out["krcs_addendum"])
		self.assertEqual(frappe.db.get_value(ENCOUNTER_DOCTYPE, enc, "krcs_mh_mood"), "T- low")

	def test_midwife_cannot_read(self):
		enc = self.completed()
		midwife = make_user("mh-midwife", [ROLE_MIDWIFE])
		with as_user(midwife):
			self.assertFalse(frappe.has_permission(ENCOUNTER_DOCTYPE, "read", doc=enc))
			self.assertNotIn(
				enc, frappe.get_list(ENCOUNTER_DOCTYPE, filters={"name": enc}, pluck="name") or []
			)
		with as_user(self.users["mic"]):
			self.assertTrue(frappe.has_permission(ENCOUNTER_DOCTYPE, "read", doc=enc))

	def test_open_is_logged_sensitive(self):
		from frappe.desk.form.load import getdoc

		enc = self.completed()
		with as_user(self.users["mic"]):
			getdoc(ENCOUNTER_DOCTYPE, enc)
		rows = frappe.get_all(
			ACCESS_LOG_DOCTYPE,
			filters={"reference_doctype": ENCOUNTER_DOCTYPE, "reference_name": enc, "action": "Open"},
			fields=["user", "sensitive"],
			ignore_permissions=True,
		)
		self.assertEqual([(r.user, r.sensitive) for r in rows], [(self.users["mic"], 1)])
