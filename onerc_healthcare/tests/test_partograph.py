# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Partograph (M2, INP-15, contract §6.2 / §7): get_or_create, add_observation, get_chart. Never commit.

Expectations are hand-computed from the WHO modified partograph: the alert line starts at the first
dilatation >= 4 cm (d0 at t0) and rises 1 cm/h; the action line is 4 h to its right. A point (t, d) is right
of the alert line when (t - t0) in hours > d - d0, and right of the action line when (t - t0) - 4 > d - d0.
"""

from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

from onerc_healthcare.constants import (
	ADMISSION_DELIVERED,
	ADMISSION_DISCHARGED,
	ADMISSION_IN_LABOUR,
	MATERNITY_ADMISSION_DOCTYPE,
	PARTOGRAPH_DOCTYPE,
	PARTOGRAPH_OBSERVATION_DOCTYPE,
)
from onerc_healthcare.maternity import partograph as ptg
from onerc_healthcare.maternity.partograph import (
	ImplausiblePartographValueError,
	PartographError,
	PartographLockedError,
)
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.maternity_factories import make_admission, maternity_users


class TestPartograph(EMRTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users = maternity_users()

	def setUp(self):
		super().setUp()
		now = now_datetime().replace(second=0, microsecond=0)
		# t0 well inside the admission (admitted 12 h ago) and far enough back for t0 + 5 h to be in the past.
		self.t0 = now - timedelta(hours=8)
		self.adm = make_admission(admitted_at=now - timedelta(hours=12))

	# ------------------------------------------------------------------ helpers

	def at(self, hours: float) -> str:
		return str(self.t0 + timedelta(hours=hours))

	def new_partograph(self, adm=None, user=None) -> str:
		frappe.set_user(user or self.users["midwife"])
		try:
			return ptg.get_or_create((adm or self.adm).name)
		finally:
			frappe.set_user("Administrator")

	def add(self, name: str, user=None, **values) -> dict:
		frappe.set_user(user or self.users["midwife"])
		try:
			return ptg.add_observation(name, values)
		finally:
			frappe.set_user("Administrator")

	def codes(self, result: dict) -> list[str]:
		return [w["code"] for w in result["warning_details"]]

	# ------------------------------------------------------------------ get_or_create

	def test_get_or_create_is_idempotent(self):
		first = self.new_partograph()
		self.assertEqual(self.new_partograph(), first)
		self.assertEqual(self.new_partograph(user=self.users["clinician"]), first)
		self.assertEqual(self.new_partograph(user=self.users["mic"]), first)
		self.assertEqual(frappe.db.count(PARTOGRAPH_DOCTYPE, {"admission": self.adm.name}), 1)
		doc = frappe.get_doc(PARTOGRAPH_DOCTYPE, first)
		self.assertEqual(doc.patient, self.adm.patient)
		self.assertEqual(doc.facility, self.adm.facility)
		self.assertFalse(doc.started_at)

	def test_get_or_create_by_mic_without_create_right(self):
		self.assertTrue(self.new_partograph(user=self.users["mic"]))

	def test_get_or_create_needs_open_admission(self):
		self.assertTrue(self.new_partograph(adm=make_admission(status=ADMISSION_IN_LABOUR)))
		for status in (ADMISSION_DELIVERED, ADMISSION_DISCHARGED):
			adm = make_admission(status=status)
			with self.subTest(status=status), self.assertRaises(PartographError):
				self.new_partograph(adm=adm)
			self.assertFalse(frappe.db.exists(PARTOGRAPH_DOCTYPE, {"admission": adm.name}))

	def test_second_partograph_for_admission_refused(self):
		self.new_partograph()
		doc = frappe.get_doc({"doctype": PARTOGRAPH_DOCTYPE, "admission": self.adm.name})
		with self.assertRaises(PartographError):
			doc.insert()

	def test_desk_insert_with_rows_refused(self):
		doc = frappe.get_doc(
			{
				"doctype": PARTOGRAPH_DOCTYPE,
				"admission": self.adm.name,
				"observations": [{"observed_at": self.at(0), "foetal_heart_rate": 140}],
			}
		)
		with self.assertRaises(PartographLockedError):
			doc.insert()

	# ------------------------------------------------------------------ roles

	def test_roles(self):
		name = self.new_partograph()
		for key in ("midwife", "clinician", "mic"):
			with self.subTest(allowed=key):
				result = self.add(name, user=self.users[key], foetal_heart_rate=140)
				row = frappe.db.get_value(
					PARTOGRAPH_OBSERVATION_DOCTYPE,
					result["row"],
					["recorded_by", "foetal_heart_rate"],
					as_dict=True,
				)
				self.assertEqual(row.recorded_by, self.users[key])
				self.assertEqual(row.foetal_heart_rate, 140)
		for key in ("reception", "his", "mho"):
			with self.subTest(refused=key):
				with self.assertRaises(frappe.PermissionError):
					self.new_partograph(user=self.users[key])
				with self.assertRaises(frappe.PermissionError):
					self.add(name, user=self.users[key], foetal_heart_rate=140)
		self.assertEqual(len(frappe.get_doc(PARTOGRAPH_DOCTYPE, name).observations), 3)

	def test_get_chart_read_roles(self):
		name = self.new_partograph()
		self.add(name, observed_at=self.at(0), cervical_dilatation_cm=4)
		for key, can_add in (("midwife", True), ("mic", True), ("his", False), ("mho", False)):
			frappe.set_user(self.users[key])
			with self.subTest(key=key):
				chart = ptg.get_chart(name)
				self.assertEqual(len(chart["points"]), 1)
				self.assertEqual(chart["can_add"], can_add)
		frappe.set_user(self.users["reception"])
		with self.assertRaises(frappe.PermissionError):
			ptg.get_chart(name)

	def test_http_methods(self):
		allowed = frappe.allowed_http_methods_for_whitelisted_func
		self.assertEqual(allowed[ptg.get_or_create], ["POST"])
		self.assertEqual(allowed[ptg.add_observation], ["POST"])
		self.assertEqual(allowed[ptg.get_chart], ["GET"])

	# ------------------------------------------------------------------ admission state

	def test_closed_admission_refuses_rows(self):
		name = self.new_partograph()
		self.add(name, foetal_heart_rate=140)
		for status in (ADMISSION_DELIVERED, ADMISSION_DISCHARGED):
			frappe.db.set_value(MATERNITY_ADMISSION_DOCTYPE, self.adm.name, "status", status)
			with self.subTest(status=status), self.assertRaises(PartographError):
				self.add(name, foetal_heart_rate=140)
		frappe.db.set_value(MATERNITY_ADMISSION_DOCTYPE, self.adm.name, "status", ADMISSION_IN_LABOUR)
		self.add(name, foetal_heart_rate=141)
		self.assertEqual(len(frappe.get_doc(PARTOGRAPH_DOCTYPE, name).observations), 2)
		# get_or_create still returns the existing partograph (to view it) after the delivery
		frappe.db.set_value(MATERNITY_ADMISSION_DOCTYPE, self.adm.name, "status", ADMISSION_DELIVERED)
		self.assertEqual(self.new_partograph(), name)

	# ------------------------------------------------------------------ row validation

	def test_row_validation(self):
		name = self.new_partograph()
		future = str(now_datetime() + timedelta(minutes=30))
		before_admission = str(get_datetime(self.adm.admitted_at) - timedelta(minutes=1))
		bad = {
			"fhr below 60": {"foetal_heart_rate": 59},
			"fhr above 220": {"foetal_heart_rate": 221},
			"fhr not a number": {"foetal_heart_rate": "abc"},
			"fhr not whole": {"foetal_heart_rate": 140.5},
			"dilatation below 0": {"cervical_dilatation_cm": -1},
			"dilatation above 10": {"cervical_dilatation_cm": 10.5},
			"descent above 5": {"descent_fifths": 6},
			"descent not whole": {"descent_fifths": 2.5},
			"contractions above 6": {"contractions_per_10_min": 7},
			"duration without contractions": {"contraction_duration": "20-40 s"},
			"duration band unknown": {"contractions_per_10_min": 3, "contraction_duration": "60 s"},
			"oxytocin above 20 U/L": {"oxytocin_u_per_l": 21},
			"drops above 80": {"oxytocin_drops_per_min": 81},
			"pulse below range": {"pulse": 19},
			"temperature in fahrenheit": {"temperature": 98.6},
			"respiration below range": {"respiratory_rate": 3},
			"systolic only": {"bp_systolic": 120},
			"systolic not above diastolic": {"bp_systolic": 80, "bp_diastolic": 80},
			"liquor code unknown": {"liquor": "X"},
			"moulding unknown": {"moulding": "++++"},
			"urine protein unknown": {"urine_protein": "positive"},
			"urine acetone unknown": {"urine_acetone": "4+"},
			"urine volume above range": {"urine_volume_ml": 5000},
			"no measurement": {"note": "Seen", "drugs_iv_fluids": "Ringer's lactate"},
			"empty": {},
			"future time": {"observed_at": future, "foetal_heart_rate": 140},
			"before admission": {"observed_at": before_admission, "foetal_heart_rate": 140},
			"unknown field": {"foetal_heart_rate": 140, "recorded_by": "Administrator"},
		}
		for label, values in bad.items():
			with self.subTest(label), self.assertRaises(ImplausiblePartographValueError):
				self.add(name, **values)
		self.assertEqual(len(frappe.get_doc(PARTOGRAPH_DOCTYPE, name).observations), 0)

	def test_valid_row_and_edges(self):
		name = self.new_partograph()
		self.add(
			name,
			observed_at=self.at(0),
			foetal_heart_rate=60,
			liquor="C",
			moulding="0",
			cervical_dilatation_cm="4,5",
			descent_fifths=0,
			contractions_per_10_min=0,
			oxytocin_u_per_l=20,
			oxytocin_drops_per_min=80,
			pulse=20,
			bp_systolic="120",
			bp_diastolic="80",
			temperature=36.8,
			respiratory_rate=16,
			urine_protein="Nil",
			urine_acetone="Trace",
			urine_volume_ml=0,
			drugs_iv_fluids="  Ringer's lactate 1 L  ",
		)
		row = frappe.get_doc(PARTOGRAPH_DOCTYPE, name).observations[0]
		self.assertEqual(get_datetime(row.observed_at), self.t0)
		# string columns hold normalised numbers; 0 is a recorded 0
		self.assertEqual(row.cervical_dilatation_cm, "4.5")
		self.assertEqual(row.descent_fifths, "0")
		self.assertEqual(row.contractions_per_10_min, "0")
		self.assertEqual(
			(row.oxytocin_u_per_l, row.oxytocin_drops_per_min, row.urine_volume_ml), ("20", "80", "0")
		)
		self.assertEqual(row.bp_systolic, 120)
		self.assertEqual(row.drugs_iv_fluids, "Ringer's lactate 1 L")
		self.assertEqual(row.recorded_by, self.users["midwife"])
		# observed_at defaults to now; JSON text values are accepted (the desk sends a string)
		result = ptg_add_json(self, name, '{"foetal_heart_rate": 220}')
		row = frappe.db.get_value(PARTOGRAPH_OBSERVATION_DOCTYPE, result["row"], "observed_at")
		self.assertLess(abs((get_datetime(row) - now_datetime()).total_seconds()), 120)

	def test_dilatation_decrease_needs_note(self):
		name = self.new_partograph()
		self.add(name, observed_at=self.at(0), cervical_dilatation_cm=4)
		self.add(name, observed_at=self.at(2), cervical_dilatation_cm=6)
		with self.assertRaises(ImplausiblePartographValueError):
			self.add(name, observed_at=self.at(3), cervical_dilatation_cm=5)
		# a back-entered row may not exceed a later value either
		with self.assertRaises(ImplausiblePartographValueError):
			self.add(name, observed_at=self.at(1), cervical_dilatation_cm=7)
		self.add(name, observed_at=self.at(1), cervical_dilatation_cm=5)  # between 4 and 6: fine
		self.add(name, observed_at=self.at(3), cervical_dilatation_cm=6)  # equal: fine
		self.add(name, observed_at=self.at(4), cervical_dilatation_cm=5, note="Re-examined by the clinician")
		self.assertEqual(len(frappe.get_doc(PARTOGRAPH_DOCTYPE, name).observations), 5)

	# ------------------------------------------------------------------ append-only

	def test_rows_are_append_only(self):
		name = self.new_partograph()
		first = self.add(name, observed_at=self.at(0), foetal_heart_rate=140)["row"]
		frappe.set_user(self.users["midwife"])

		doc = frappe.get_doc(PARTOGRAPH_DOCTYPE, name)
		doc.observations[0].foetal_heart_rate = 120
		with self.assertRaises(PartographLockedError):
			doc.save()

		doc = frappe.get_doc(PARTOGRAPH_DOCTYPE, name)
		doc.set("observations", [])
		with self.assertRaises(PartographLockedError):
			doc.save()

		doc = frappe.get_doc(PARTOGRAPH_DOCTYPE, name)
		doc.append("observations", {"observed_at": self.at(1), "foetal_heart_rate": 130})
		with self.assertRaises(PartographLockedError):
			doc.save()

		doc = frappe.get_doc(PARTOGRAPH_DOCTYPE, name)
		doc.alert_line_crossed = 1
		with self.assertRaises(PartographLockedError):
			doc.save()

		# the REST paths: frappe.client.save of the parent, set_value / save / delete of the child row
		data = frappe.get_doc(PARTOGRAPH_DOCTYPE, name).as_dict()
		data["observations"][0]["foetal_heart_rate"] = 110
		with self.assertRaises(PartographLockedError):
			frappe.client.save(data)
		with self.assertRaises(PartographLockedError):
			frappe.client.set_value(PARTOGRAPH_OBSERVATION_DOCTYPE, first, "foetal_heart_rate", 110)
		row = frappe.get_doc(PARTOGRAPH_OBSERVATION_DOCTYPE, first)
		row.foetal_heart_rate = 110
		with self.assertRaises((PartographLockedError, frappe.PermissionError)):
			row.save()
		with self.assertRaises(PartographLockedError):
			frappe.client.delete(PARTOGRAPH_OBSERVATION_DOCTYPE, first)
		with self.assertRaises(frappe.PermissionError):
			frappe.client.delete(PARTOGRAPH_DOCTYPE, name)

		frappe.set_user("Administrator")
		with self.assertRaises(PartographLockedError):
			frappe.delete_doc(PARTOGRAPH_OBSERVATION_DOCTYPE, first)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc(PARTOGRAPH_DOCTYPE, name)
		row = frappe.get_doc(PARTOGRAPH_OBSERVATION_DOCTYPE, first)
		row.foetal_heart_rate = 110
		with self.assertRaises(PartographLockedError):
			row.save()
		self.assertEqual(frappe.db.get_value(PARTOGRAPH_OBSERVATION_DOCTYPE, first, "foetal_heart_rate"), 140)
		self.assertEqual(len(frappe.get_doc(PARTOGRAPH_DOCTYPE, name).observations), 1)

	def test_correction_row_supersedes_on_the_chart(self):
		name = self.new_partograph()
		self.add(name, observed_at=self.at(0), cervical_dilatation_cm=4)
		result = self.add(name, observed_at=self.at(2), cervical_dilatation_cm=9)  # a typo for 5
		self.assertEqual(result["alert_line_crossed"], 0)
		result = self.add(name, observed_at=self.at(2), cervical_dilatation_cm=5, note="Typo: 5 cm, not 9")
		# effective value at t0+2h is now 5 cm: 2 h elapsed > 5 - 4 = 1 cm, so right of the alert line
		self.assertEqual(result["alert_line_crossed"], 1)
		self.assertEqual(result["action_line_crossed"], 0)
		chart = ptg.get_chart(name)
		self.assertEqual([p["dilatation"] for p in chart["points"]], [4, 5])
		rows = frappe.get_doc(PARTOGRAPH_DOCTYPE, name).observations
		self.assertEqual([r.cervical_dilatation_cm for r in rows], ["4", "9", "5"])  # nothing rewritten

	# ------------------------------------------------------------------ alert / action lines

	def test_alert_line_math(self):
		name = self.new_partograph()
		self.add(name, observed_at=self.at(-2), cervical_dilatation_cm=2)  # latent phase: origin t0 - 2 h
		result = self.add(name, observed_at=self.at(0), cervical_dilatation_cm=4)
		self.assertEqual(get_datetime(result["started_at"]), self.t0)
		self.assertEqual(result["alert_line_crossed"], 0)
		result = self.add(name, observed_at=self.at(1), cervical_dilatation_cm=5)  # exactly on the line
		self.assertEqual(result["alert_line_crossed"], 0)
		self.assertEqual(result["warnings"], [])
		result = self.add(name, observed_at=self.at(3), cervical_dilatation_cm=5)  # 3 h > 1 cm
		self.assertEqual((result["alert_line_crossed"], result["action_line_crossed"]), (1, 0))
		self.assertEqual(self.codes(result), ["alert_line"])

		doc = frappe.get_doc(PARTOGRAPH_DOCTYPE, name)
		self.assertEqual(get_datetime(doc.started_at), self.t0)
		self.assertEqual((doc.alert_line_crossed, doc.action_line_crossed), (1, 0))

		chart = ptg.get_chart(name)
		self.assertEqual(get_datetime(chart["origin"]), self.t0 - timedelta(hours=2))
		# hours from the first observation: the alert line runs (2 h, 4 cm) -> (8 h, 10 cm); action +4 h
		self.assertEqual(chart["alert_line"], [[2.0, 4.0], [8.0, 10]])
		self.assertEqual(chart["action_line"], [[6.0, 4.0], [12.0, 10]])
		self.assertEqual([p["hours"] for p in chart["points"]], [0.0, 2.0, 3.0, 5.0])
		self.assertEqual([p["alert_crossed"] for p in chart["points"]], [False, False, False, True])
		self.assertEqual([p["action_crossed"] for p in chart["points"]], [False, False, False, False])

	def test_action_line_math(self):
		name = self.new_partograph()
		self.add(name, observed_at=self.at(0), cervical_dilatation_cm=4)
		result = self.add(
			name, observed_at=self.at(4), cervical_dilatation_cm=4
		)  # 4 h - 4 = 0: on the action line
		self.assertEqual((result["alert_line_crossed"], result["action_line_crossed"]), (1, 0))
		result = self.add(name, observed_at=self.at(5), cervical_dilatation_cm=4)  # 5 h - 4 = 1 > 0 cm
		self.assertEqual((result["alert_line_crossed"], result["action_line_crossed"]), (1, 1))
		self.assertEqual(self.codes(result), ["action_line"])  # the action warning replaces the alert one
		chart = ptg.get_chart(name)
		self.assertEqual(chart["alert_line"], [[0.0, 4.0], [6.0, 10]])
		self.assertEqual(chart["action_line"], [[4.0, 4.0], [10.0, 10]])
		self.assertEqual([p["action_crossed"] for p in chart["points"]], [False, False, True])

	def test_alert_line_from_higher_first_dilatation(self):
		name = self.new_partograph()
		self.add(name, observed_at=self.at(0), cervical_dilatation_cm=3)
		self.add(name, observed_at=self.at(1), cervical_dilatation_cm=6)  # d0 = 6 at 1 h
		result = self.add(
			name, observed_at=self.at(5), cervical_dilatation_cm=10
		)  # 4 h = 10 - 6: on the line
		self.assertEqual(result["alert_line_crossed"], 0)
		self.assertEqual(get_datetime(result["started_at"]), self.t0 + timedelta(hours=1))
		chart = ptg.get_chart(name)
		self.assertEqual(chart["alert_line"], [[1.0, 6.0], [5.0, 10]])
		self.assertEqual(chart["action_line"], [[5.0, 6.0], [9.0, 10]])

	def test_no_active_labour_no_lines(self):
		name = self.new_partograph()
		result = self.add(name, observed_at=self.at(0), cervical_dilatation_cm=3)
		self.assertIsNone(result["started_at"])
		chart = ptg.get_chart(name)
		self.assertEqual((chart["alert_line"], chart["action_line"]), ([], []))

	def test_zero_kept_and_blank_kept(self):
		"""0 is a recorded value; a blank stays blank (stored and returned as None, never 0)."""
		name = self.new_partograph()
		zero = self.add(
			name,
			observed_at=self.at(0),
			cervical_dilatation_cm=0,
			descent_fifths=0,
			contractions_per_10_min=0,
			oxytocin_u_per_l=0,
			oxytocin_drops_per_min=0,
			urine_volume_ml=0,
		)["row"]
		blank = self.add(name, observed_at=self.at(1), foetal_heart_rate=140, cervical_dilatation_cm="")[
			"row"
		]
		fields = list(ptg.STRING_NUMBER_FIELDS)
		stored = frappe.db.get_value(PARTOGRAPH_OBSERVATION_DOCTYPE, zero, fields, as_dict=True)
		self.assertEqual(stored, dict.fromkeys(fields, "0"))
		stored = frappe.db.get_value(PARTOGRAPH_OBSERVATION_DOCTYPE, blank, fields, as_dict=True)
		self.assertTrue(all(v in (None, "") for v in stored.values()), stored)
		# the Int column of the first row stores 0 for "not recorded" and reads back as None
		self.assertEqual(frappe.db.get_value(PARTOGRAPH_OBSERVATION_DOCTYPE, zero, "foetal_heart_rate"), 0)

		chart = ptg.get_chart(name)
		self.assertEqual([p["dilatation"] for p in chart["points"]], [0, None])
		self.assertEqual([p["descent"] for p in chart["points"]], [0, None])
		self.assertEqual([p["fhr"] for p in chart["points"]], [None, 140])
		last = chart["last_values"]
		for fieldname in fields:
			self.assertEqual(last[fieldname]["value"], 0)
			self.assertIsInstance(last[fieldname]["value"], int | float)
		self.assertEqual(last["foetal_heart_rate"]["value"], 140)
		self.assertIsNone(chart["started_at"])  # 0 cm is not active labour
		self.assertEqual(chart["warnings"], [])

	def test_chart_returns_numbers(self):
		name = self.new_partograph()
		self.add(name, observed_at=self.at(0), cervical_dilatation_cm="4,5", descent_fifths="3")
		# an FHR-only row 5 h later is not a dilatation at all: no line is crossed by it
		result = self.add(name, observed_at=self.at(5), foetal_heart_rate=140)
		self.assertEqual((result["alert_line_crossed"], result["action_line_crossed"]), (0, 0))
		self.add(name, observed_at=self.at(6), cervical_dilatation_cm=10, descent_fifths=0)
		chart = ptg.get_chart(name)
		self.assertEqual([p["dilatation"] for p in chart["points"]], [4.5, None, 10])
		self.assertEqual([p["descent"] for p in chart["points"]], [3, None, 0])
		for p in chart["points"]:
			for key in ("dilatation", "descent", "fhr"):
				self.assertTrue(p[key] is None or isinstance(p[key], int | float), (key, p[key]))
		self.assertEqual(chart["alert_line"], [[0.0, 4.5], [5.5, 10]])
		self.assertIsInstance(chart["last_values"]["cervical_dilatation_cm"]["value"], float)
		with self.assertRaises(ImplausiblePartographValueError):
			self.add(name, cervical_dilatation_cm="10 cm")  # trailing text is not a number

	# ------------------------------------------------------------------ warnings

	def test_warnings_use_latest_values(self):
		name = self.new_partograph()
		result = self.add(
			name,
			observed_at=self.at(0),
			foetal_heart_rate=100,
			liquor="M",
			moulding="+++",
			bp_systolic=160,
			bp_diastolic=100,
			temperature=38,
		)
		self.assertEqual(self.codes(result), ["fhr_low", "liquor_meconium", "moulding", "bp_severe", "fever"])
		self.assertEqual(len(result["warnings"]), 5)
		result = self.add(
			name,
			observed_at=self.at(1),
			foetal_heart_rate=140,
			liquor="B",
			moulding="++",
			bp_systolic=159,
			bp_diastolic=109,
			temperature=37.9,
		)
		self.assertEqual(self.codes(result), ["liquor_blood"])
		result = self.add(name, observed_at=self.at(2), foetal_heart_rate=161)
		self.assertEqual(self.codes(result), ["fhr_high", "liquor_blood"])
		result = self.add(
			name, observed_at=self.at(3), foetal_heart_rate=160, bp_systolic=150, bp_diastolic=110
		)
		self.assertEqual(self.codes(result), ["liquor_blood", "bp_severe"])
		result = self.add(
			name, observed_at=self.at(4), foetal_heart_rate=110, liquor="C", bp_systolic=120, bp_diastolic=80
		)
		self.assertEqual(result["warnings"], [])
		chart = ptg.get_chart(name)
		self.assertEqual([p["fhr"] for p in chart["points"]], [100, 140, 161, 160, 110])


def ptg_add_json(case: TestPartograph, name: str, values: str) -> dict:
	frappe.set_user(case.users["midwife"])
	try:
		return ptg.add_observation(name, values)
	finally:
		frappe.set_user("Administrator")
