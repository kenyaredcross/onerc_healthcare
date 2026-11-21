# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MCH-B: Growth Monitoring and WHO z-scores (docs/MCH_CONTRACT.md §8, CON-17). Never commit.

Expected values are written out by hand: the LMS parameters and the published SD columns are copied from the
WHO expanded tables (boys weight-for-age, day 365: L 0.0645, M 9.646, S 0.10925; -3/-2/+2/+3 SD 6.926, 7.741,
11.983, 13.341 kg), and the z-scores computed from them with z = ((y/M)^L - 1)/(L*S) and WHO's SD23 rule.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, today

from onerc_healthcare import constants as c
from onerc_healthcare.mch import common, growth
from onerc_healthcare.mch.common import MCHRecordError
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.mch_factories import make_child, make_mch_visit, mch_users

GM = c.GROWTH_MONITORING_DOCTYPE
SAM, MAM, NORMAL = c.WASTING_SAM, c.WASTING_MAM, c.GROWTH_NORMAL


def close_visit(name: str) -> None:
	frappe.db.set_value(c.VISIT_DOCTYPE, name, "status", c.VISIT_STATUS_DISCHARGED, update_modified=False)


# =====================================================================================================
# WHO tables and z-score maths (no database)
# =====================================================================================================
class TestWHOTables(EMRTestCase):
	def test_files_name_their_source(self):
		expected = {
			"wfa": (0, 1856, 1857),
			"lhfa": (0, 1856, 1857),
			"wfl": (45, 110, 651),
			"wfh": (65, 120, 551),
		}
		for indicator, (first, last, count) in expected.items():
			for sex in ("boys", "girls"):
				table = growth.load_table(indicator, sex)
				self.assertTrue(table["source"].startswith("https://cdn.who.int/"), (indicator, sex))
				self.assertIn(f"{indicator}-{sex}-zscore-expanded-table", table["source"])
				self.assertTrue(table["retrieved"])
				rows = table["rows"]
				self.assertEqual(len(rows), count, (indicator, sex))
				self.assertEqual((rows[0][0], rows[-1][0]), (first, last), (indicator, sex))

	def test_who_spot_checks(self):
		# WHO weight-for-age, boys, 365 days: L 0.0645, M 9.6460 (about 9.6 kg), S 0.10925
		L, M, S = growth.lms("wfa", "Male", 365)
		self.assertEqual((L, M, S), (0.0645, 9.646, 0.10925))
		self.assertAlmostEqual(M, 9.6, delta=0.1)
		self.assertEqual(growth.lms("wfa", "Female", 365)[1], 8.9462)
		self.assertEqual(growth.lms("wfa", "Male", 0)[1], 3.3464)  # birth weight median
		self.assertEqual(growth.lms("lhfa", "Male", 730)[1], 87.8018)
		self.assertEqual(growth.lms("lhfa", "Female", 730)[1], 86.4008)
		self.assertEqual(growth.lms("wfl", "Male", 75)[1], 9.5032)
		self.assertEqual(growth.lms("wfh", "Female", 87)[1], 11.8965)

	def test_zscore_at_published_sd_lines(self):
		# The published SD columns (3 decimals) give z of -2, 0, +2 to 2 decimals.
		cases = (
			("wfa", "Male", 365, 7.741, -2),
			("wfa", "Male", 365, 9.646, 0),
			("wfa", "Female", 365, 11.506, 2),
			("lhfa", "Male", 730, 81.693, -2),
			("lhfa", "Female", 730, 92.851, 2),
			("wfl", "Male", 75, 11.275, 2),
			("wfh", "Female", 87, 10.015, -2),
			("wfh", "Male", 87, 12.164, 0),
		)
		for indicator, sex, x, y, z in cases:
			self.assertAlmostEqual(
				growth.zscore(indicator, sex, x, y), z, places=2, msg=(indicator, sex, x, y)
			)

	def test_zscore_hand_computed(self):
		# ((8.0 / 9.646) ** 0.0645 - 1) / (0.0645 * 0.10925) = -1.7023
		self.assertAlmostEqual(growth.zscore("wfa", "Male", 365, 8.0), -1.7023, places=3)
		# L = 1 (length): (64 - 75.7391) / (75.7391 * 0.03137) = -4.9408
		self.assertAlmostEqual(growth.zscore("lhfa", "Male", 365, 64), -4.9408, places=3)
		self.assertEqual(growth.fmt_z(growth.zscore("wfa", "Male", 365, 8.0)), "-1.70")
		self.assertEqual(growth.fmt_z(-0.001), "0.00")

	def test_sd23_beyond_three(self):
		# Above +3: 3 + (14.0 - SD3) / (SD3 - SD2) = 3 + (14.0 - 13.341) / (13.341 - 11.983) = 3.4853
		self.assertAlmostEqual(growth.zscore("wfa", "Male", 365, 14.0), 3.4853, places=2)
		# Below -3: -3 + (6.5 - SD3neg) / (SD2neg - SD3neg) = -3 + (6.5 - 6.926) / (7.741 - 6.926) = -3.5227
		self.assertAlmostEqual(growth.zscore("wfa", "Male", 365, 6.5), -3.5227, places=2)
		# The plain LMS formula would give 3.4510 and -3.5676: the adjustment is applied.
		self.assertNotAlmostEqual(growth.zscore("wfa", "Male", 365, 14.0), 3.4510, places=2)
		self.assertNotAlmostEqual(growth.zscore("wfa", "Male", 365, 6.5), -3.5676, places=2)
		# Within ±3 nothing changes.
		self.assertAlmostEqual(growth.zscore("wfa", "Male", 365, 8.0), -1.7023, places=3)

	def test_outside_the_tables(self):
		self.assertIsNone(growth.zscore("wfa", "Male", 1857, 18))
		self.assertIsNone(growth.zscore("wfa", "Male", -1, 3))
		self.assertIsNone(growth.zscore("wfl", "Male", 44.9, 2))
		self.assertIsNone(growth.zscore("wfl", "Male", 110.1, 18))
		self.assertIsNone(growth.zscore("wfh", "Female", 64.9, 7))
		self.assertIsNone(growth.zscore("wfh", "Female", 120.1, 22))
		self.assertIsNone(growth.zscore("wfa", "Other", 365, 9))
		self.assertIsNone(growth.zscore("wfa", "Male", 365, 0))
		self.assertIsNotNone(growth.zscore("wfl", "Male", 110, 18))

	def test_length_between_rows_is_interpolated(self):
		_L, M, S = growth.lms("wfl", "Male", 75.05)
		self.assertAlmostEqual(M, (9.5032 + 9.5235) / 2, places=6)
		self.assertAlmostEqual(S, (0.08295 + 0.08297) / 2, places=7)

	def test_classification_boundaries(self):
		cl = growth.classify
		self.assertEqual(cl(-2.0, "M", "S"), NORMAL)
		self.assertEqual(cl(-2.01, "M", "S"), "M")
		self.assertEqual(cl(-3.0, "M", "S"), "M")
		self.assertEqual(cl(-3.01, "M", "S"), "S")
		self.assertIsNone(cl(None, "M", "S"))
		w = growth.classify_wasting
		age = 200  # 6-59 months
		self.assertEqual(w(None, 11.4, "None", age), SAM)
		self.assertEqual(w(None, 11.5, "None", age), MAM)
		self.assertEqual(w(None, 12.4, "None", age), MAM)
		self.assertEqual(w(None, 12.5, "None", age), NORMAL)
		self.assertEqual(w(0.5, 0, "+", age), SAM)  # any bilateral oedema
		self.assertEqual(w(0.5, 0, "+++", 30), SAM)
		self.assertEqual(w(-3.01, 0, "None", age), SAM)
		self.assertEqual(w(-3.0, 13, "None", age), MAM)
		self.assertEqual(w(-2.01, 13, "None", age), MAM)
		self.assertEqual(w(-2.0, 13, "None", age), NORMAL)
		self.assertEqual(w(-1.0, 11.0, "None", age), SAM)  # MUAC alone is enough
		# MUAC is not used under 6 months: nothing to judge.
		self.assertIsNone(w(None, 11.0, "None", 182))
		self.assertEqual(w(None, 11.0, "None", 183), SAM)
		self.assertIsNone(w(None, 0, "None", age))


# =====================================================================================================
# Growth Monitoring record
# =====================================================================================================
class GrowthCase(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.users = mch_users()

	def record(self, child: str, user: str | None = None, submit=False, close=True, visit=None, **kw):
		values = {
			"doctype": GM,
			"patient": child,
			"visit": visit or make_mch_visit(child).name,
			"measured_on": today(),
		}
		values.update(kw)
		doc = frappe.get_doc(values)
		if user:
			frappe.set_user(self.users[user])
		try:
			doc.insert()
			if submit:
				doc.submit()
		finally:
			frappe.set_user("Administrator")
			if close:  # also after a refusal, so the child can start another visit
				close_visit(doc.visit)
		return doc


class TestGrowthRecord(GrowthCase):
	def test_calculates_and_classifies(self):
		child = make_child(age_days=365, sex="Male").name
		doc = self.record(
			child, user="triage", submit=True, weight_kg=8.0, length_height_cm=75.7, muac_cm=13.0
		)
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual((doc.age_days, doc.age_label, doc.sex), (365, "11 months", "Male"))
		self.assertEqual(doc.measurement_position, growth.POSITION_LYING)  # under 2 years
		self.assertEqual(doc.wfa_z, "-1.70")
		# (75.7 - 75.7391) / (75.7391 * 0.03137) = -0.0165
		self.assertEqual(doc.lhfa_z, "-0.02")
		self.assertEqual(doc.wflh_z, growth.fmt_z(growth.zscore("wfl", "Male", 75.7, 8.0)))
		self.assertEqual((doc.underweight, doc.stunting), (NORMAL, NORMAL))
		self.assertIn(doc.wasting, (NORMAL, MAM, SAM))
		self.assertEqual(doc.wasting, growth.classify_wasting(float(doc.wflh_z), 13.0, "None", 365))
		self.assertFalse(doc.zscore_note)
		self.assertEqual(doc.recorded_by, self.users["triage"])
		self.assertEqual(doc.dob_estimated, 0)

	def test_severe_underweight_and_stunting(self):
		child = make_child(age_days=365, sex="Male").name
		doc = self.record(child, weight_kg=6.5, length_height_cm=64)
		self.assertEqual(doc.wfa_z, "-3.52")  # SD23 rule
		self.assertEqual(doc.lhfa_z, "-4.94")
		self.assertEqual(doc.underweight, c.UNDERWEIGHT_SEVERE)
		self.assertEqual(doc.stunting, c.STUNTING_SEVERE)

	def test_standing_under_two_years_adds_0_7(self):
		child = make_child(age_days=400, sex="Female").name
		doc = self.record(
			child, weight_kg=9.0, length_height_cm=75.0, measurement_position=growth.POSITION_STANDING
		)
		self.assertEqual(doc.lhfa_z, growth.fmt_z(growth.zscore("lhfa", "Female", 400, 75.7)))
		self.assertNotEqual(doc.lhfa_z, growth.fmt_z(growth.zscore("lhfa", "Female", 400, 75.0)))
		self.assertEqual(doc.wflh_z, growth.fmt_z(growth.zscore("wfl", "Female", 75.7, 9.0)))
		self.assertIn("0.7", doc.zscore_note)
		self.assertIn("75.7", doc.zscore_note)

	def test_lying_from_two_years_subtracts_0_7(self):
		child = make_child(age_days=800, sex="Male").name
		doc = self.record(
			child, weight_kg=12.0, length_height_cm=88.0, measurement_position=growth.POSITION_LYING
		)
		self.assertEqual(doc.lhfa_z, growth.fmt_z(growth.zscore("lhfa", "Male", 800, 87.3)))
		self.assertEqual(doc.wflh_z, growth.fmt_z(growth.zscore("wfh", "Male", 87.3, 12.0)))  # height table
		self.assertIn("0.7", doc.zscore_note)
		# The expected position: no correction, no note; blank position = standing from 2 years.
		child2 = make_child(age_days=800, sex="Male").name
		doc2 = self.record(child2, weight_kg=12.0, length_height_cm=88.0)
		self.assertEqual(doc2.measurement_position, growth.POSITION_STANDING)
		self.assertEqual(doc2.lhfa_z, growth.fmt_z(growth.zscore("lhfa", "Male", 800, 88.0)))
		self.assertEqual(doc2.wflh_z, growth.fmt_z(growth.zscore("wfh", "Male", 88.0, 12.0)))
		self.assertFalse(doc2.zscore_note)

	def test_blank_z_scores_are_explained(self):
		child = make_child(age_days=10, sex="Male").name
		doc = self.record(
			child, weight_kg=2.0, length_height_cm=44.0
		)  # below the 45 cm weight-for-length row
		self.assertTrue(doc.wfa_z)
		self.assertTrue(doc.lhfa_z)
		self.assertFalse(doc.wflh_z)
		self.assertIn("outside the WHO table", doc.zscore_note)
		self.assertIsNone(doc.wasting)  # nothing to judge
		child2 = make_child(age_days=100, sex="Male").name
		doc2 = self.record(child2, weight_kg=5.5)
		self.assertTrue(doc2.wfa_z)
		self.assertFalse(doc2.lhfa_z or doc2.wflh_z)
		self.assertIn("Not calculated", doc2.zscore_note)
		self.assertFalse(doc2.stunting)

	def test_muac_and_oedema_on_a_record(self):
		child = make_child(age_days=200).name
		self.assertEqual(self.record(child, muac_cm=11.4).wasting, SAM)
		self.assertEqual(self.record(child, muac_cm=11.5).wasting, MAM)
		self.assertEqual(self.record(child, muac_cm=12.5).wasting, NORMAL)
		self.assertEqual(self.record(child, weight_kg=7.5, oedema="+").wasting, SAM)
		young = make_child(age_days=100).name
		doc = self.record(young, muac_cm=11.0)
		self.assertIsNone(doc.wasting)
		self.assertIn("MUAC", doc.zscore_note)

	def test_estimated_dob_adds_a_note(self):
		child = make_child(age_days=300, dob_estimated=1).name
		doc = self.record(child, weight_kg=8.0)
		self.assertEqual(doc.dob_estimated, 1)
		self.assertIn("estimated", doc.zscore_note)

	def test_age_five_years_refused(self):
		old = make_child(age_days=c.GROWTH_MAX_AGE_DAYS).name
		with self.assertRaisesRegex(MCHRecordError, "under 5"):
			self.record(old, weight_kg=18)
		young = make_child(age_days=c.GROWTH_MAX_AGE_DAYS - 1).name
		doc = self.record(young, weight_kg=18)
		self.assertEqual(doc.age_days, c.GROWTH_MAX_AGE_DAYS - 1)
		self.assertTrue(doc.wfa_z)

	def test_dates_and_ranges(self):
		child = make_child(age_days=300).name
		with self.assertRaisesRegex(MCHRecordError, "future"):
			self.record(child, weight_kg=8, measured_on=add_days(today(), 1))
		with self.assertRaisesRegex(MCHRecordError, "before the child's date of birth"):
			self.record(child, weight_kg=8, measured_on=add_days(today(), -301))
		for field, value in (
			("weight_kg", 45),
			("weight_kg", 0.4),
			("length_height_cm", 20),
			("muac_cm", 30),
			("head_circumference_cm", 70),
		):
			with self.assertRaises(MCHRecordError, msg=field):
				self.record(child, **{field: value})
		with self.assertRaisesRegex(MCHRecordError, "at least one measurement"):
			self.record(child)
		# 0 means not measured
		doc = self.record(child, weight_kg=8, length_height_cm=0, muac_cm=0)
		self.assertFalse(doc.measurement_position)


class TestGrowthLifecycle(GrowthCase):
	def test_one_per_visit_and_closed_visit(self):
		child = make_child(age_days=300).name
		first = self.record(child, weight_kg=8, close=False)
		with self.assertRaisesRegex(MCHRecordError, "already recorded"):
			self.record(child, weight_kg=8.1, visit=first.visit)
		close_visit(first.visit)
		with self.assertRaisesRegex(MCHRecordError, "open visit"):
			self.record(child, weight_kg=8.1, visit=first.visit)

	def test_cancel_and_edit_refused_addendum_allowed(self):
		child = make_child(age_days=300).name
		doc = self.record(child, user="triage", submit=True, weight_kg=8)
		doc.reload()
		with self.assertRaises(frappe.PermissionError):
			doc.cancel()
		doc.reload()
		doc.weight_kg = 9
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		frappe.set_user(self.users["triage"])
		try:
			common.add_addendum(GM, doc.name, "Scale recalibrated: weight 8.2 kg")
		finally:
			frappe.set_user("Administrator")
		self.assertIn("8.2 kg", frappe.db.get_value(GM, doc.name, "addendum"))
		self.assertEqual(frappe.db.get_value(GM, doc.name, "weight_kg"), 8)

	def test_archive_only_by_medical_in_charge(self):
		child = make_child(age_days=300).name
		doc = self.record(child, submit=True, weight_kg=8)
		frappe.set_user(self.users["clinician"])
		try:
			with self.assertRaises(frappe.PermissionError):
				common.archive(GM, doc.name, "Wrong child")
		finally:
			frappe.set_user("Administrator")
		frappe.set_user(self.users["mic"])
		try:
			common.archive(GM, doc.name, "Wrong child")
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(GM, doc.name, "krcs_archived"), 1)

	def test_open_writes_access_log(self):
		from frappe.desk.form.load import getdoc

		child = make_child(age_days=300).name
		doc = self.record(child, submit=True, weight_kg=8)
		frappe.set_user(self.users["his"])
		try:
			getdoc(GM, doc.name)
		finally:
			frappe.set_user("Administrator")
		rows = frappe.get_all(
			c.ACCESS_LOG_DOCTYPE,
			filters={"reference_doctype": GM, "reference_name": doc.name, "user": self.users["his"]},
			fields=["action", "patient"],
		)
		self.assertEqual([(r.action, r.patient) for r in rows], [("Open", child)])

	def test_roles(self):
		child = make_child(age_days=300).name
		doc = self.record(child, submit=True, weight_kg=8)
		for key in ("clinician", "midwife", "mic", "triage"):
			for ptype in ("read", "create", "write", "submit"):
				self.assertTrue(frappe.has_permission(GM, ptype, user=self.users[key]), (key, ptype))
		for key in ("his", "mho"):
			self.assertTrue(frappe.has_permission(GM, "read", doc=doc.name, user=self.users[key]), key)
			self.assertFalse(frappe.has_permission(GM, "create", user=self.users[key]), key)
		self.assertFalse(frappe.has_permission(GM, "read", user=self.users["reception"]))
		for key in ("his", "reception"):
			other = make_child(age_days=300).name
			with self.assertRaises(frappe.PermissionError):
				self.record(other, user=key, weight_kg=8)
		# Triage Nurse records (above, as Administrator here): an insert as the Triage Nurse works.
		self.assertEqual(self.record(make_child(age_days=300).name, user="triage", weight_kg=8).docstatus, 0)


class TestChartData(GrowthCase):
	def chart(self, child: str, user: str = "clinician") -> dict:
		frappe.set_user(self.users[user])
		try:
			return growth.chart_data(child)
		finally:
			frappe.set_user("Administrator")

	def test_points_and_curves(self):
		child = make_child(age_days=300, sex="Female").name
		a = self.record(child, submit=True, weight_kg=6.0, measured_on=add_days(today(), -200))
		b = self.record(child, submit=True, weight_kg=8.0)
		self.record(child, weight_kg=8.1)  # draft: not charted
		archived = self.record(
			child, submit=True, muac_cm=13, weight_kg=7.0, measured_on=add_days(today(), -100)
		)
		frappe.db.set_value(GM, archived.name, "krcs_archived", 1, update_modified=False)
		self.record(child, submit=True, muac_cm=13)  # no weight: not charted
		data = self.chart(child)
		self.assertEqual(data["sex"], "Female")
		self.assertEqual(data["dob_estimated"], 0)
		self.assertEqual(
			[(p["name"], p["age_days"], p["weight_kg"]) for p in data["points"]],
			[(a.name, 100, 6.0), (b.name, 300, 8.0)],
		)
		self.assertEqual(set(data["curves"]), {"-3", "-2", "0", "+2", "+3"})
		median = data["curves"]["0"]
		self.assertEqual(median[0], [0, 3.232])  # girls' birth weight median 3.2322 (WHO), 3 decimals
		self.assertEqual([d for d, _kg in median[:3]], [0, 30, 60])
		self.assertEqual(median[-1][0], 1856)
		# Girls, 360 days: the +2 line is the inverse LMS at z = 2.
		L, M, S = growth.lms("wfa", "Female", 360)
		day360 = dict(data["curves"]["+2"])[360]
		self.assertAlmostEqual(day360, round(growth.value_at(L, M, S, 2), 3), places=3)
		self.assertLess(dict(data["curves"]["-3"])[360], dict(data["curves"]["-2"])[360])

	def test_access(self):
		child = make_child(age_days=300).name
		self.assertIn("curves", self.chart(child, user="triage"))
		self.assertIn("curves", self.chart(child, user="his"))
		with self.assertRaises(frappe.PermissionError):
			self.chart(child, user="reception")
