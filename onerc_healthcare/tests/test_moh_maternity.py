# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""M3: MOH 717 sections J (inpatient) and K (maternity) from the maternity records (provisional, OI-5).
Never commit.

Fixture rows are written with ``db_insert`` (no controllers), as in `test_moh_717`: the evaluators only read
the tables, so these tests pin the counting rules of `reporting.maternity` (its docstring), not the capture
logic (`test_delivery`, `test_maternity_admission`). Period: May 2003 at facility 0 (F0); facility 1 (F1)
holds records that must not appear. Every expected number is counted by hand in the tables below.

Admissions (A; "open" = not discharged; bed days = dates from admission to the day before discharge,
at least one, inside May; stay days = max(1, discharge date - admission date)):

| A | Facility | Admitted | Discharged (outcome) | J01 | J02 | Deaths | May bed days | Stay days |
|---|---|---|---|---|---|---|---|---|
| A1 | F0 | 29 Apr | 3 May (home) | - | 1 | - | 2 (1, 2 May) | 4 |
| A2 | F0 | 10 May 09:00 | 10 May 18:00 (home) | 1 | 1 | - | 1 (same day) | 1 |
| A3 | F0 | 30 May 22:00 | open (Delivered) | 1 | - | - | 2 (30, 31 May) | - |
| A4 | F0 | 25 May | 2 Jun (referred out) | 1 | - | - | 7 (25-31 May) | - |
| A5 | F0 | 12 May | 15 May (died) | 1 | 1 | 1 | 3 (12-14 May) | 3 |
| A11 | F0 | 20 Apr | 1 May (home) | - | 1 | - | 0 (20-30 Apr) | 11 |
| A9 | F0 | 1 Mar | 30 Apr (home) | - | - | - | 0 | - |
| A10 | F0 | 1 Jun | open | - | - | - | 0 | - |
| A6 | F1 | 5 May | 6 May (died) | F1 | F1 | F1 | F1: 1 | F1: 1 |
| A7 | F0, archived | 7 May | 8 May (died) | - | - | - | - | - |
| A8 | F0, mother merged | 8 May | open | - | - | - | - | - |

F0 May: J01 = 4, J02 = 4, J03 = K08 = 1, J04 = 2 + 1 + 2 + 7 + 3 = 15, stay days = 4 + 1 + 3 + 11 = 19.

Deliveries (D; babies: L = live, FS = fresh stillbirth, MS = macerated stillbirth):

| D | Admission | Delivered | Mode | BBA | Babies | Counted at F0 in May |
|---|---|---|---|---|---|---|
| D1 | A2 | 10 May | SVD | | L F | K01, K05 F |
| D2 | A3 | 31 May 23:30 | Caesarean | | L M, L M (twins) | K02, K05 M 2 |
| D3 | A4 | 25 May | SVD | yes | L F, FS | K04 (not K01), K05 F, K06 |
| D4 | A5 | 13 May | Assisted | | MS (sex F, ignored) | K03, K07 |
| D5 | A1 | 30 Apr 23:59 | SVD | | L F | no: April |
| D13 | A10 | 1 Jun 00:10 | SVD | | L M | no: June |
| D6 | A11 | 1 May | SVD | | L F | no: draft |
| D7 | A11 | 1 May | SVD | | L M | no: archived |
| D12 | A5 | 13 May | SVD | | L M | no: cancelled (docstatus 2) |
| D8 | A6 (F1) | 5 May | SVD | | L F | no: other facility (F1: K01, K05 F) |
| D9 | A7 (archived) | 7 May | SVD | | L F | no: archived admission |
| D10 | A8 (merged mother) | 8 May | SVD | | L M | no: merged mother |

F0 May: K01 = 1, K02 = 1, K03 = 1, K04 = 1, K05 = F 2 + M 2 (total 4), K06 = 1, K07 = 1; deliveries = 4,
babies = 6.
"""

from __future__ import annotations

from datetime import date

import frappe

from onerc_healthcare import constants as c
from onerc_healthcare.reporting import engine, registry, workload
from onerc_healthcare.reporting import maternity as m
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import get_company_name, get_facility, make_patient

FROM, TO = date(2003, 5, 1), date(2003, 5, 31)
NONE, SEX = registry.DISAGGREGATION_NONE, registry.DISAGGREGATION_SEX
SVD, ASSISTED, CS = c.DELIVERY_SVD, c.DELIVERY_ASSISTED, c.DELIVERY_CAESAREAN
L, FS, MS = c.BABY_LIVE_BIRTH, c.BABY_FRESH_STILLBIRTH, c.BABY_MACERATED_STILLBIRTH

EXPECTED_F0 = {
	("717-J01", "", ""): 4,
	("717-J02", "", ""): 4,
	("717-J03", "", ""): 1,
	("717-J04", "", ""): 15,
	("717-K01", "", ""): 1,
	("717-K02", "", ""): 1,
	("717-K03", "", ""): 1,
	("717-K04", "", ""): 1,
	("717-K05", "", "Female"): 2,
	("717-K05", "", "Male"): 2,
	("717-K05", "", ""): 4,  # engine total
	("717-K06", "", ""): 1,
	("717-K07", "", ""): 1,
	("717-K08", "", ""): 1,
}
EXPECTED_F1 = {
	("717-J01", "", ""): 1,
	("717-J02", "", ""): 1,
	("717-J03", "", ""): 1,
	("717-J04", "", ""): 1,
	("717-K01", "", ""): 1,
	("717-K05", "", "Female"): 1,
	("717-K05", "", ""): 1,
	("717-K08", "", ""): 1,
}


def _row(doctype: str, **values) -> str:
	doc = frappe.get_doc({"doctype": doctype, **values})
	doc.name = values.get("name") or f"T-M3-{frappe.generate_hash(length=10)}"
	doc.db_insert()
	return doc.name


def el(code, source, rule, dis=NONE) -> registry.ElementSpec:
	return registry.ElementSpec(code, code, source, rule, dis)


class MaternityReportCase(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.f0, self.f1 = get_facility(0), get_facility(1)
		self.company = get_company_name()
		self.build()

	def admission(self, facility, admitted, discharged=None, outcome=None, **kw) -> str:
		return _row(
			c.MATERNITY_ADMISSION_DOCTYPE,
			patient=kw.pop("patient", None) or make_patient(sex="Female", dob="1980-01-01").name,
			facility=facility,
			status=c.ADMISSION_DISCHARGED if discharged else kw.pop("status", c.ADMISSION_ADMITTED),
			admitted_at=admitted,
			discharged_at=discharged,
			discharge_outcome=outcome,
			**kw,
		)

	def delivery(self, admission, delivered, mode, babies, docstatus=1, **kw) -> str:
		adm = frappe.db.get_value(
			c.MATERNITY_ADMISSION_DOCTYPE, admission, ["patient", "facility"], as_dict=True
		)
		name = _row(
			c.DELIVERY_RECORD_DOCTYPE,
			admission=admission,
			patient=adm.patient,
			facility=adm.facility,
			delivered_at=delivered,
			delivery_mode=mode,
			docstatus=docstatus,
			**kw,
		)
		for idx, (outcome, sex) in enumerate(babies, 1):
			_row(
				c.DELIVERY_BABY_DOCTYPE,
				parent=name,
				parenttype=c.DELIVERY_RECORD_DOCTYPE,
				parentfield="babies",
				idx=idx,
				outcome=outcome,
				sex=sex,
				docstatus=docstatus,
			)
		return name

	def build(self) -> None:
		f0, f1 = self.f0, self.f1
		home, died, referred = c.MATERNITY_DISCHARGED_HOME, c.MATERNITY_DIED, c.MATERNITY_REFERRED_OUT
		a1 = self.admission(f0, "2003-04-29 08:00", "2003-05-03 10:00", home)
		a2 = self.admission(f0, "2003-05-10 09:00", "2003-05-10 18:00", home)
		a3 = self.admission(f0, "2003-05-30 22:00", status=c.ADMISSION_DELIVERED)
		a4 = self.admission(f0, "2003-05-25 06:00", "2003-06-02 09:00", referred)
		a5 = self.admission(f0, "2003-05-12 07:00", "2003-05-15 11:00", died)
		a11 = self.admission(f0, "2003-04-20 07:00", "2003-05-01 08:00", home)
		self.admission(f0, "2003-03-01 07:00", "2003-04-30 08:00", home)  # A9
		a10 = self.admission(f0, "2003-06-01 00:05")
		a6 = self.admission(f1, "2003-05-05 07:00", "2003-05-06 08:00", died)
		a7 = self.admission(f0, "2003-05-07 07:00", "2003-05-08 08:00", died, krcs_archived=1)
		merged = make_patient(sex="Female", dob="1985-01-01").name
		survivor = make_patient(sex="Female", dob="1985-01-01").name
		frappe.db.set_value("Patient", merged, "krcs_merged_into", survivor, update_modified=False)
		a8 = self.admission(f0, "2003-05-08 07:00", patient=merged)

		self.delivery(a2, "2003-05-10 12:00", SVD, [(L, "Female")])  # D1
		self.delivery(a3, "2003-05-31 23:30", CS, [(L, "Male"), (L, "Male")])  # D2
		self.delivery(a4, "2003-05-25 05:00", SVD, [(L, "Female"), (FS, None)], born_before_arrival=1)  # D3
		self.delivery(a5, "2003-05-13 10:00", ASSISTED, [(MS, "Female")])  # D4
		self.delivery(a1, "2003-04-30 23:59", SVD, [(L, "Female")])  # D5
		self.delivery(a10, "2003-06-01 00:10", SVD, [(L, "Male")])  # D13
		self.delivery(a11, "2003-05-01 06:00", SVD, [(L, "Female")], docstatus=0)  # D6
		self.delivery(a11, "2003-05-01 06:00", SVD, [(L, "Male")], krcs_archived=1)  # D7
		self.delivery(a5, "2003-05-13 10:00", SVD, [(L, "Male")], docstatus=2)  # D12
		self.delivery(a6, "2003-05-05 10:00", SVD, [(L, "Female")])  # D8
		self.delivery(a7, "2003-05-07 10:00", SVD, [(L, "Female")])  # D9
		self.delivery(a8, "2003-05-08 10:00", SVD, [(L, "Male")])  # D10

	def ctx(self, facility=None, report="T-M3") -> registry.SourceContext:
		return registry.SourceContext(
			report=report, from_date=FROM, to_date=TO, facility=facility or self.f0, age_bands=()
		)


class TestEvaluators(MaternityReportCase):
	def test_delivery_source(self):
		elements = [
			el("SVD", m.SOURCE_DELIVERY, {"delivery_modes": [SVD], "born_before_arrival": "exclude"}),
			el("SVD-ALL", m.SOURCE_DELIVERY, {"delivery_modes": [SVD]}),  # D1 + D3 (BBA)
			el("BBA", m.SOURCE_DELIVERY, {"born_before_arrival": "only"}),
			el("ALL", m.SOURCE_DELIVERY, {}),  # D1-D4
			el("BABIES", m.SOURCE_DELIVERY, {"measure": "babies"}),  # 1 + 2 + 2 + 1
			el("LIVE", m.SOURCE_DELIVERY, {"measure": "babies", "baby_outcomes": [L]}, SEX),
			el("STILL", m.SOURCE_DELIVERY, {"measure": "babies", "baby_outcomes": [FS, MS]}),
		]
		got = workload.evaluate_delivery(self.ctx(), elements)
		self.assertEqual(
			got,
			{
				("SVD", "", ""): 1,
				("SVD-ALL", "", ""): 2,
				("BBA", "", ""): 1,
				("ALL", "", ""): 4,
				("BABIES", "", ""): 6,
				("LIVE", "", "Female"): 2,
				("LIVE", "", "Male"): 2,
				("STILL", "", ""): 2,
			},
		)

	def test_admission_source(self):
		A = m.SOURCE_MATERNITY_ADMISSION
		elements = [
			el("ADM", A, {"measure": "admissions"}),
			el("DIS", A, {"measure": "discharges"}),
			el("DIED", A, {"measure": "discharges", "outcomes": [c.MATERNITY_DIED]}),
			el("BED", A, {"measure": "bed_days"}),
			el("STAY", A, {"measure": "stay_days"}),
			el("STAY-DIED", A, {"measure": "stay_days", "outcomes": [c.MATERNITY_DIED]}),
		]
		got = workload.evaluate_maternity_admission(self.ctx(), elements)
		self.assertEqual(
			got,
			{
				("ADM", "", ""): 4,
				("DIS", "", ""): 4,
				("DIED", "", ""): 1,
				("BED", "", ""): 15,
				("STAY", "", ""): 19,
				("STAY-DIED", "", ""): 3,
			},
		)

	def test_open_stay_counts_up_to_today_only(self):
		# A stay admitted yesterday and still open, in a period running to the end of next month: two
		# bed days (yesterday and today), not the rest of the month.
		from frappe.utils import add_days, add_months, get_last_day, getdate, today

		start = add_days(today(), -1)
		ctx = registry.SourceContext(
			report="T-M3",
			from_date=getdate(start),
			to_date=get_last_day(add_months(today(), 1)),
			facility=self.f0,
		)
		bed = [el("BED", m.SOURCE_MATERNITY_ADMISSION, {"measure": "bed_days"})]
		before = workload.evaluate_maternity_admission(ctx, bed).get(("BED", "", ""), 0)
		self.admission(self.f0, f"{start} 10:00")
		after = workload.evaluate_maternity_admission(ctx, bed).get(("BED", "", ""), 0)
		self.assertEqual(after - before, 2)

	def test_rules(self):
		v = workload.validate_rule
		D, A = m.SOURCE_DELIVERY, m.SOURCE_MATERNITY_ADMISSION
		self.assertEqual(v(D, {}), [])
		self.assertEqual(v(D, {"measure": "babies", "baby_outcomes": [L]}, SEX), [])
		self.assertTrue(v(D, {"delivery_modes": ["Water birth"]}))
		self.assertTrue(v(D, {"born_before_arrival": "maybe"}))
		self.assertTrue(v(D, {"baby_outcomes": [L]}))  # babies only
		self.assertTrue(v(D, {}, SEX))  # deliveries are not split by sex
		self.assertTrue(v(D, {"measure": "babies"}, registry.DISAGGREGATION_AGE))
		self.assertTrue(v(D, {"colour": "red"}))
		self.assertTrue(v(A, {}))  # measure required
		self.assertEqual(v(A, {"measure": "discharges", "outcomes": [c.MATERNITY_DIED]}), [])
		self.assertTrue(v(A, {"measure": "admissions", "outcomes": [c.MATERNITY_DIED]}))
		self.assertTrue(v(A, {"measure": "discharges", "outcomes": ["Cured"]}))
		self.assertTrue(v(A, {"measure": "admissions"}, SEX))
		# An invalid rule is an "Invalid rule" exception at generation, never a crash.
		ctx = self.ctx()
		self.assertEqual(workload.evaluate_maternity_admission(ctx, [el("X", A, {})]), {})
		self.assertEqual([e["kind"] for e in ctx.exceptions], ["Invalid rule"])

	def test_registry(self):
		for source in (m.SOURCE_DELIVERY, m.SOURCE_MATERNITY_ADMISSION):
			self.assertIn(source, registry.SOURCE_TYPES)
			self.assertIn(source, workload.RULE_SCHEMAS)
		self.assertIs(registry.get_evaluator(m.SOURCE_DELIVERY), workload.evaluate_delivery)
		self.assertIs(
			registry.get_evaluator(m.SOURCE_MATERNITY_ADMISSION), workload.evaluate_maternity_admission
		)


class TestMOH717(MaternityReportCase):
	"""The seeded MOH 717 definition (reporting/data/moh_717.json, synced on migrate) through the engine."""

	def evaluate(self, facility) -> dict:
		ctx = registry.SourceContext(
			report=c.MOH_717,
			from_date=FROM,
			to_date=TO,
			facility=facility,
			age_bands=engine.age_bands_of(c.MOH_717),
		)
		cells = engine.evaluate_report(c.MOH_717, ctx).cells
		return {k: v for k, v in cells.items() if k[0][:5] in ("717-J", "717-K") and v}

	def test_f0(self):
		self.assertEqual(self.evaluate(self.f0), EXPECTED_F0)

	def test_f1(self):
		self.assertEqual(self.evaluate(self.f1), EXPECTED_F1)

	def test_gaps(self):
		rows = {
			r.code: r
			for r in frappe.get_doc(c.MOH_REPORT_DOCTYPE, c.MOH_717).elements
			if r.code[:5] in ("717-J", "717-K")
		}
		gaps = sorted(code for code, r in rows.items() if r.availability == registry.AVAILABILITY_GAP)
		self.assertEqual(gaps, ["717-J05", "717-J06"])
		self.assertEqual(rows["717-K05"].disaggregation, SEX)
