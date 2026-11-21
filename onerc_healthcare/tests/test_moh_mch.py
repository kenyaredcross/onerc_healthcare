# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MCH-A: MOH 717 B07 (CWC), B08 (ANC) and B09 (PNC) from the MCH records (`reporting.mch`; provisional,
OI-5 / OI-8, D62). Never commit.

Fixture rows are written with ``db_insert`` (no controllers), as in `test_moh_maternity`: the evaluator only
reads the tables, so these tests pin the counting rules (the `reporting.mch` docstring), not the capture
rules (`test_mch_anc`, `test_mch_pnc`). One test at the end counts a real ANC Contact made through the
controllers. Period: May 2004 at facility 0 (F0); facility 1 (F1) holds records that must not appear at F0.

Patients: C1 boy born 1 Nov 2003; C2 girl born 1 Jan 2002; M1 woman born 1 Jan 1980; M2 woman born
1 Jun 1985; B1 girl born 1 May 2004; X boy merged into another record.

CWC (I = Immunisation, G = Growth Monitoring):

| Rec | Patient | Visit | Facility | Date | State | Counted at F0 in May |
|---|---|---|---|---|---|---|
| I1 | C1 | V1 | F0 | 3 May | submitted | yes |
| G1 | C1 | V1 | F0 | 3 May | submitted | yes (same visit as I1) |
| I2 | C1 | V2 | F0 | 20 May | submitted | yes |
| G2 | C1 | V2 | F0 | 20 May | submitted | yes (same visit as I2) |
| I3 | C2 | V3 | F0 | 10 May | submitted | yes |
| G3 | C2 | V4 | F0 | 15 May | submitted | yes (growth only) |
| I4 | C2 | V5 | F0 | 11 May | draft | no |
| G4 | C2 | V6 | F0 | 12 May | archived | no |
| G6 | C2 | V11 | F0 | 25 May | cancelled (docstatus 2) | no |
| I5 | C1 | V7 | F1 | 5 May | submitted | F1 only |
| I6 | C1 | V8 | F0 | 30 Apr | submitted | no: April |
| G5 | C2 | V9 | F0 | 1 Jun | submitted | no: June |
| I7 | X | V10 | F0 | 6 May | submitted | no: merged patient |

F0: records 6, visits 4 (V1-V4; B07), patients 2; by sex: boy C1 2 visits, girl C2 2 visits (all "<5").

ANC Contacts: A1 M1 2 May, A2 M1 30 May, A3 M2 15 May (F0, submitted: counted); A4 M2 16 May cancelled; A6 M1
31 May archived; A5 M2 17 May at F1. F0: B08 = 3 (all women aged 18-24: band "5-59", Female), patients 2.

PNC Visits: P1 M1 Mother on a visit 20 May; P2 B1 Baby on a ward admission (no visit) 2 May; P3 M2 Mother on a
ward admission 3 May (F0, submitted: counted); P4 M2 archived; P5 B1 draft; P6 B1 at F1. F0: B09 = 3 (Mother 2,
Baby 1; by age: B1 "<5" Female 1, M1 and M2 "5-59" Female 2); unit visits = 3 (each ward check its own).
F1: B07 = 1, B08 = 1, B09 = 1.
"""

from __future__ import annotations

from datetime import date

import frappe
from frappe.utils import today

from onerc_healthcare import constants as c
from onerc_healthcare.reporting import engine, registry, workload
from onerc_healthcare.reporting import mch as m
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import get_company_name, get_facility, make_patient

FROM, TO = date(2004, 5, 1), date(2004, 5, 31)
NONE, SEX = registry.DISAGGREGATION_NONE, registry.DISAGGREGATION_SEX
AGE, AGE_SEX = registry.DISAGGREGATION_AGE, registry.DISAGGREGATION_AGE_SEX
IMM, GRM, ANC, PNC = (
	c.IMMUNISATION_DOCTYPE,
	c.GROWTH_MONITORING_DOCTYPE,
	c.ANC_CONTACT_DOCTYPE,
	c.PNC_VISIT_DOCTYPE,
)
SRC = m.SOURCE_MCH
B_CODES = ("717-B07", "717-B08", "717-B09")


def _row(doctype: str, **values) -> str:
	doc = frappe.get_doc({"doctype": doctype, **values})
	doc.name = values.get("name") or f"T-MCH-{frappe.generate_hash(length=10)}"
	doc.db_insert()
	return doc.name


def el(code, rule, dis=NONE) -> registry.ElementSpec:
	return registry.ElementSpec(code, code, SRC, rule, dis)


class MCHReportCase(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.f0, self.f1 = get_facility(0), get_facility(1)
		self.company = get_company_name()
		self.build()

	def rec(self, doctype, patient, day, facility=None, visit=None, docstatus=1, **kw) -> str:
		date_field = m.DATE_FIELDS[doctype]
		values = {
			"patient": patient,
			"facility": facility or self.f0,
			date_field: day,
			"docstatus": docstatus,
			**kw,
		}
		if visit is not None:
			values["visit"] = f"T-MCH-{self.tag}-{visit}" if visit else None
		return _row(doctype, **values)

	def build(self) -> None:
		self.tag = frappe.generate_hash(length=6)
		f1 = self.f1
		c1 = make_patient(sex="Male", dob="2003-11-01").name
		c2 = make_patient(sex="Female", dob="2002-01-01").name
		m1 = make_patient(sex="Female", dob="1980-01-01").name
		m2 = make_patient(sex="Female", dob="1985-06-01").name
		b1 = make_patient(sex="Female", dob="2004-05-01").name
		x = make_patient(sex="Male", dob="2003-01-01").name
		frappe.db.set_value("Patient", x, "krcs_merged_into", c1, update_modified=False)

		self.rec(IMM, c1, "2004-05-03", visit="V1")  # I1
		self.rec(GRM, c1, "2004-05-03", visit="V1")  # G1
		self.rec(IMM, c1, "2004-05-20", visit="V2")  # I2
		self.rec(GRM, c1, "2004-05-20", visit="V2")  # G2
		self.rec(IMM, c2, "2004-05-10", visit="V3")  # I3
		self.rec(GRM, c2, "2004-05-15", visit="V4")  # G3
		self.rec(IMM, c2, "2004-05-11", visit="V5", docstatus=0)  # I4
		self.rec(GRM, c2, "2004-05-12", visit="V6", krcs_archived=1)  # G4
		self.rec(GRM, c2, "2004-05-25", visit="V11", docstatus=2)  # G6
		self.rec(IMM, c1, "2004-05-05", f1, visit="V7")  # I5
		self.rec(IMM, c1, "2004-04-30", visit="V8")  # I6
		self.rec(GRM, c2, "2004-06-01", visit="V9")  # G5
		self.rec(IMM, x, "2004-05-06", visit="V10")  # I7

		self.rec(ANC, m1, "2004-05-02", visit="VA1")  # A1
		self.rec(ANC, m1, "2004-05-30", visit="VA2")  # A2
		self.rec(ANC, m2, "2004-05-15", visit="VA3")  # A3
		self.rec(ANC, m2, "2004-05-16", visit="VA4", docstatus=2)  # A4
		self.rec(ANC, m1, "2004-05-31", visit="VA6", krcs_archived=1)  # A6
		self.rec(ANC, m2, "2004-05-17", f1, visit="VA5")  # A5

		self.rec(PNC, m1, "2004-05-20", visit="VP1", subject=c.PNC_SUBJECT_MOTHER)  # P1
		self.rec(
			PNC, b1, "2004-05-02", visit="", subject=c.PNC_SUBJECT_BABY, maternity_admission="T-ADM1"
		)  # P2
		self.rec(
			PNC, m2, "2004-05-03", visit="", subject=c.PNC_SUBJECT_MOTHER, maternity_admission="T-ADM2"
		)  # P3
		self.rec(PNC, m2, "2004-05-04", visit="VP2", subject=c.PNC_SUBJECT_MOTHER, krcs_archived=1)  # P4
		self.rec(PNC, b1, "2004-05-09", visit="VP3", subject=c.PNC_SUBJECT_BABY, docstatus=0)  # P5
		self.rec(PNC, b1, "2004-05-09", f1, visit="VP4", subject=c.PNC_SUBJECT_BABY)  # P6

	def ctx(self, facility=None, bands=()) -> registry.SourceContext:
		return registry.SourceContext(
			report="T-MCH", from_date=FROM, to_date=TO, facility=facility or self.f0, age_bands=bands
		)


class TestEvaluator(MCHReportCase):
	def test_units(self):
		cwc = [IMM, GRM]
		elements = [
			el("CWC-REC", {"record_types": cwc}),
			el("CWC-VIS", {"record_types": cwc, "unit": "visits"}),
			el("CWC-PAT", {"record_types": cwc, "unit": "patients"}),
			el("IMM", {"record_types": [IMM]}),
			el("GRM", {"record_types": [GRM], "unit": "visits"}),
			el("ANC", {"record_types": [ANC]}),
			el("ANC-PAT", {"record_types": [ANC], "unit": "patients"}),
			el("PNC", {"record_types": [PNC]}),
			el("PNC-VIS", {"record_types": [PNC], "unit": "visits"}),
			el("PNC-M", {"record_types": [PNC], "pnc_subjects": [c.PNC_SUBJECT_MOTHER]}),
			el("PNC-B", {"record_types": [PNC], "pnc_subjects": [c.PNC_SUBJECT_BABY]}),
			el("MOTHERS", {"record_types": [ANC, PNC], "unit": "patients"}),  # M1, M2, B1
			el("ALL-VIS", {"record_types": [IMM, GRM, ANC, PNC], "unit": "visits"}),  # 4 + 3 + 3
		]
		got = workload.evaluate_mch(self.ctx(), elements)
		self.assertEqual(
			got,
			{
				("CWC-REC", "", ""): 6,
				("CWC-VIS", "", ""): 4,
				("CWC-PAT", "", ""): 2,
				("IMM", "", ""): 3,
				("GRM", "", ""): 3,
				("ANC", "", ""): 3,
				("ANC-PAT", "", ""): 2,
				("PNC", "", ""): 3,
				("PNC-VIS", "", ""): 3,
				("PNC-M", "", ""): 2,
				("PNC-B", "", ""): 1,
				("MOTHERS", "", ""): 3,
				("ALL-VIS", "", ""): 10,
			},
		)

	def test_other_facility(self):
		elements = [
			el("CWC", {"record_types": [IMM, GRM], "unit": "visits"}),
			el("ANC", {"record_types": [ANC]}),
			el("PNC", {"record_types": [PNC]}),
		]
		got = workload.evaluate_mch(self.ctx(self.f1), elements)
		self.assertEqual(got, {("CWC", "", ""): 1, ("ANC", "", ""): 1, ("PNC", "", ""): 1})

	def test_disaggregation(self):
		bands = engine.age_bands_of(c.MOH_717)  # <5, 5-59, 60+
		elements = [
			el("CWC", {"record_types": [IMM, GRM], "unit": "visits"}, AGE_SEX),
			el("CWC-SEX", {"record_types": [IMM, GRM], "unit": "visits"}, SEX),
			el("ANC", {"record_types": [ANC]}, AGE_SEX),
			el("PNC", {"record_types": [PNC]}, AGE),
		]
		got = workload.evaluate_mch(self.ctx(bands=bands), elements)
		self.assertEqual(
			got,
			{
				("CWC", "<5", "Male"): 2,
				("CWC", "<5", "Female"): 2,
				("CWC-SEX", "", "Male"): 2,
				("CWC-SEX", "", "Female"): 2,
				("ANC", "5-59", "Female"): 3,
				("PNC", "<5", ""): 1,
				("PNC", "5-59", ""): 2,
			},
		)

	def test_rules(self):
		v = workload.validate_rule
		self.assertTrue(v(SRC, {}))  # record_types required
		self.assertTrue(v(SRC, {"record_types": []}))
		self.assertTrue(v(SRC, {"record_types": ["Patient Encounter"]}))
		self.assertTrue(v(SRC, {"record_types": [ANC], "unit": "babies"}))
		self.assertTrue(v(SRC, {"record_types": [ANC], "pnc_subjects": [c.PNC_SUBJECT_MOTHER]}))
		self.assertTrue(v(SRC, {"record_types": [PNC], "pnc_subjects": ["Father"]}))
		self.assertTrue(v(SRC, {"record_types": [PNC], "colour": "red"}))
		self.assertEqual(v(SRC, {"record_types": [PNC], "pnc_subjects": [c.PNC_SUBJECT_BABY]}), [])
		for dis in (NONE, AGE, SEX, AGE_SEX):  # the HIS Officer may disaggregate any MCH count
			with self.subTest(dis=dis):
				self.assertEqual(v(SRC, {"record_types": [IMM, GRM], "unit": "visits"}, dis), [])
		# An invalid rule is an "Invalid rule" exception at generation, never a crash.
		ctx = self.ctx()
		self.assertEqual(workload.evaluate_mch(ctx, [el("X", {"record_types": ["Lab"]})]), {})
		self.assertEqual([e["kind"] for e in ctx.exceptions], ["Invalid rule"])

	def test_registry(self):
		self.assertIn(SRC, registry.SOURCE_TYPES)
		self.assertIn(SRC, workload.RULE_SCHEMAS)
		self.assertIs(registry.get_evaluator(SRC), workload.evaluate_mch)


class TestMOH717(MCHReportCase):
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
		return {k: v for k, v in cells.items() if k[0] in B_CODES and v}

	def test_f0(self):
		self.assertEqual(
			self.evaluate(self.f0),
			{("717-B07", "", ""): 4, ("717-B08", "", ""): 3, ("717-B09", "", ""): 3},
		)

	def test_f1(self):
		self.assertEqual(
			self.evaluate(self.f1),
			{("717-B07", "", ""): 1, ("717-B08", "", ""): 1, ("717-B09", "", ""): 1},
		)

	def test_definition(self):
		rows = {
			r.code: r for r in frappe.get_doc(c.MOH_REPORT_DOCTYPE, c.MOH_717).elements if r.code in B_CODES
		}
		self.assertEqual(sorted(rows), list(B_CODES))
		for code, row in rows.items():
			with self.subTest(code=code):
				self.assertEqual((row.source_type, row.availability), (SRC, registry.AVAILABILITY_AVAILABLE))
				self.assertEqual(workload.validate_rule(SRC, row.rule, row.disaggregation), [])


class TestRealRecords(EMRTestCase):
	"""A real ANC Contact (controllers, then submit) is counted on its date at its visit's facility."""

	def test_submitted_contact_counted(self):
		from onerc_healthcare.tests.mch_factories import make_mother, make_next_visit, make_pregnancy

		ctx = registry.SourceContext(
			report="T-MCH", from_date=today(), to_date=today(), facility=get_facility(0)
		)
		anc = [el("ANC", {"record_types": [ANC]})]
		before = workload.evaluate_mch(ctx, anc).get(("ANC", "", ""), 0)
		mother = make_mother().name
		make_pregnancy(mother)
		doc = frappe.get_doc(
			{
				"doctype": ANC,
				"patient": mother,
				"visit": make_next_visit(mother).name,
				"contact_date": today(),
			}
		).insert(ignore_permissions=True)
		self.assertEqual(workload.evaluate_mch(ctx, anc).get(("ANC", "", ""), 0), before)  # a draft
		doc.submit()
		self.assertEqual(workload.evaluate_mch(ctx, anc).get(("ANC", "", ""), 0), before + 1)
