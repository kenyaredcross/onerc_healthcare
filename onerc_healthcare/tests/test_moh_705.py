# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MOH 705A/705B (R2; FR-29..31, SDD §5.8, OI-5): ICD-11 block structure, the category mapping and its
seeds, the diagnosis counting rule, and the mapping endpoints. Expected counts are worked out by hand in
each test's docstring. Counting tests call `diagnosis.evaluate` directly with a `SourceContext` (no R1
DocTypes needed) on a quiet past month, with records written straight to the tables."""

from __future__ import annotations

import copy
import os
from datetime import date

import frappe
from frappe.utils import add_months, add_years

from onerc_healthcare.coding import icd11
from onerc_healthcare.constants import (
	ENCOUNTER_DOCTYPE,
	ICD11_BLOCK_DOCTYPE,
	MOH_CATEGORY_DOCTYPE,
	MOH_MAPPING_DOCTYPE,
	REFERRAL_DOCTYPE,
	ROLE_CLINICIAN,
	ROLE_HIS,
	ROLE_MIC,
	VISIT_DOCTYPE,
)
from onerc_healthcare.reporting import diagnosis, mapping
from onerc_healthcare.reporting.registry import ElementSpec, SourceContext
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.clinical_factories import as_user
from onerc_healthcare.tests.factories import get_facility, make_patient, make_user

FROM, TO = date(2019, 3, 1), date(2019, 3, 31)
DAY = date(2019, 3, 15)
SRC = "Diagnosis Category"


def cv(code: str) -> str:
	return f"{code}-ICD-11"


def el_a(category: str) -> ElementSpec:
	return ElementSpec(f"A-{category}", category, SRC, {"category": category, "age_max_months": 60})


def el_b(category: str) -> ElementSpec:
	return ElementSpec(f"B-{category}", category, SRC, {"category": category, "age_min_months": 60})


def el_any(category: str) -> ElementSpec:
	return ElementSpec(f"X-{category}", category, SRC, {"category": category})


CATEGORIES = (
	"Diarrhoea",
	"Cholera",
	"Pneumonia",
	"Confirmed Malaria",
	"Clinical Malaria",
	"All Other Diseases",
	"Hypertension",
	"Road Traffic Injuries",
	"Other Injuries",
)


def elements() -> list[ElementSpec]:
	return [el_a(c) for c in CATEGORIES] + [el_b(c) for c in CATEGORIES]


class Counting(EMRTestCase):
	"""Records for the counting rule. Visits, encounters, referrals and observations are inserted directly
	(db_insert) so only the reporting rule is under test."""

	def setUp(self):
		super().setUp()
		self.facility = get_facility(0)
		self.n = 0

	def uid(self, prefix: str) -> str:
		self.n += 1
		return f"T-R2-{prefix}-{frappe.generate_hash(length=8)}-{self.n}"

	def patient(self, dob=None, months: int | None = 360, sex="Female") -> str:
		"""A patient `months` old on DAY (dob given explicitly wins; months=None and no dob: no DOB)."""
		p = make_patient(sex=sex).name
		if dob is None and months is not None:
			dob = add_months(DAY, -months)
		frappe.db.set_value("Patient", p, "dob", dob)
		return p

	def visit(self, patient: str, facility: str | None = None, **values) -> str:
		doc = frappe.new_doc(VISIT_DOCTYPE)
		doc.update(
			{
				"name": self.uid("PV"),
				"patient": patient,
				"facility": facility or self.facility,
				"visit_date": values.pop("visit_date", DAY),
				"status": "Discharged",
				**values,
			}
		)
		doc.db_insert()
		return doc.name

	def encounter(self, patient: str, visit: str, dx, day=DAY, docstatus: int = 1) -> str:
		"""`dx`: list of (code, case_type) or (code, case_type, is_primary)."""
		doc = frappe.new_doc(ENCOUNTER_DOCTYPE)
		doc.update(
			{
				"name": self.uid("ENC"),
				"patient": patient,
				"krcs_visit": visit,
				"encounter_date": day,
				"docstatus": docstatus,
			}
		)
		for i, row in enumerate(dx):
			code, case = row[0], row[1]
			primary = row[2] if len(row) > 2 else int(i == 0)
			doc.append("krcs_diagnoses", {"code": cv(code), "case_type": case, "is_primary": primary})
		doc.db_insert()
		for child in doc.krcs_diagnoses:
			child.name = self.uid("DX")
			child.parent, child.parenttype, child.parentfield = doc.name, ENCOUNTER_DOCTYPE, "krcs_diagnoses"
			child.docstatus = docstatus
			child.db_insert()
		return doc.name

	def refer(self, source_encounter: str, patient: str, day=DAY, **visit_values) -> str:
		"""An internal referral from `source_encounter` and the visit it created. Returns the new visit."""
		ref = frappe.new_doc(REFERRAL_DOCTYPE)
		target = self.visit(patient, visit_date=day, **visit_values)
		ref.update(
			{
				"name": self.uid("REF"),
				"patient": patient,
				"referral_type": "Internal",
				"source_encounter": source_encounter,
				"target_visit": target,
				"status": "Received",
			}
		)
		ref.db_insert()
		frappe.db.set_value(VISIT_DOCTYPE, target, "referred_from", ref.name)
		return target

	def observation(self, patient, encounter, test, result, docstatus=1, via_request=False) -> str:
		obs = frappe.new_doc("Observation")
		obs.update(
			{
				"name": self.uid("OBS"),
				"patient": patient,
				"observation_template": test,
				"result": result,
				"docstatus": docstatus,
			}
		)
		if via_request:
			sr = frappe.new_doc("Service Request")
			sr.update(
				{
					"name": self.uid("SR"),
					"patient": patient,
					"order_group": encounter,
					"template_dt": "Observation Template",
					"template_dn": test,
					"docstatus": 1,
				}
			)
			sr.db_insert()
			obs.service_request = sr.name
		else:
			obs.reference_doctype, obs.reference_docname = ENCOUNTER_DOCTYPE, encounter
		obs.db_insert()
		return obs.name

	def ctx(self, **kw) -> SourceContext:
		return SourceContext(report="T-R2", from_date=FROM, to_date=TO, facility=self.facility, **kw)

	def counts(self, specs=None, ctx=None) -> dict[str, int]:
		ctx = ctx or self.ctx()
		cells = diagnosis.evaluate(ctx, specs or elements())
		self.last_ctx = ctx
		return {key[0]: value for key, value in cells.items()}

	def assertCounts(self, expected: dict[str, int], **kw):
		got = self.counts(**kw)
		self.assertEqual({k: v for k, v in got.items() if v}, {k: v for k, v in expected.items() if v})


class TestCountingRule(Counting):
	def test_new_only_primary_and_secondary_two_codes_one_category(self):
		"""Child (24 months), one encounter: 1A40.Z New primary + 1A22 New secondary (both Diarrhoea),
		CA40.Z New secondary (Pneumonia), 1A00 Follow-up (Cholera, not counted).
		Expected 705A: Diarrhoea 1 (once per encounter), Pneumonia 1, Cholera 0."""
		p = self.patient(months=24)
		v = self.visit(p)
		self.encounter(
			p, v, [("1A40.Z", "New", 1), ("1A22", "New", 0), ("CA40.Z", "New", 0), ("1A00", "Follow-up", 0)]
		)
		self.assertCounts({"A-Diarrhoea": 1, "A-Pneumonia": 1})

	def test_follow_up_only_counts_nothing(self):
		"""An encounter whose only diagnosis is a follow-up: nothing counted."""
		p = self.patient(months=24)
		self.encounter(p, self.visit(p), [("1A40.Z", "Follow-up")])
		self.assertCounts({})

	def test_internal_referral_chain_counts_once(self):
		"""Adult: E1 Diarrhoea, internally referred; E2 (the referral visit) Diarrhoea again + Pneumonia.
		Expected 705B: Diarrhoea 1 (chain de-duplicated), Pneumonia 1. Without the referral link the same
		two encounters would give Diarrhoea 2 (checked with a second, unlinked patient: +1)."""
		p = self.patient()
		e1 = self.encounter(p, self.visit(p), [("1A40.Z", "New")])
		v2 = self.refer(e1, p)
		self.encounter(p, v2, [("1A22", "New"), ("CA40.Z", "New", 0)])
		q = self.patient()
		self.encounter(q, self.visit(q), [("1A40.Z", "New")])
		self.encounter(q, self.visit(q), [("1A40.Z", "New")])
		self.assertCounts({"B-Diarrhoea": 3, "B-Pneumonia": 1})

	def test_referral_from_previous_period_not_counted_again(self):
		"""E1 on 28 Feb (Diarrhoea) referred internally; E2 on 1 Mar (Diarrhoea + Pneumonia). March:
		Diarrhoea 0 (counted in February), Pneumonia 1. February: Diarrhoea 1."""
		p = self.patient()
		e1 = self.encounter(
			p, self.visit(p, visit_date=date(2019, 2, 28)), [("1A40.Z", "New")], day=date(2019, 2, 28)
		)
		v2 = self.refer(e1, p, day=date(2019, 3, 1))
		self.encounter(p, v2, [("1A40.Z", "New"), ("CA40.Z", "New", 0)], day=date(2019, 3, 1))
		self.assertCounts({"B-Pneumonia": 1})
		feb = SourceContext("T-R2", date(2019, 2, 1), date(2019, 2, 28), facility=self.facility)
		self.assertCounts({"B-Diarrhoea": 1}, ctx=feb)

	def test_confirmed_vs_clinical_malaria(self):
		"""Four adults with a malaria code:
		- 1F40.Z + approved positive RDT on the encounter (reference)     -> Confirmed
		- 1F45   + approved positive slide via the encounter's request    -> Confirmed
		- 1F40.Z + approved negative RDT                                  -> Clinical
		- 1F40.Z + positive RDT still in draft (not approved)             -> Clinical
		- 1F40.Z + approved positive RDT on another encounter of the patient -> Clinical
		Expected 705B: Confirmed 2, Clinical 3."""
		rdt, slide = "Malaria Rapid Diagnostic Test", "Malaria Blood Slide"
		for code, test, result, docstatus, via in (
			("1F40.Z", rdt, "Positive - P. falciparum", 1, False),
			("1F45", slide, "P. falciparum seen", 1, True),
			("1F40.Z", rdt, "Negative", 1, False),
			("1F40.Z", rdt, "Positive - P. falciparum", 0, False),
		):
			p = self.patient()
			e = self.encounter(p, self.visit(p), [(code, "New")])
			self.observation(p, e, test, result, docstatus=docstatus, via_request=via)
		p = self.patient()
		e = self.encounter(p, self.visit(p), [("1F40.Z", "New")])
		other = self.encounter(p, self.visit(p), [("FB56.2", "Follow-up")], day=date(2019, 3, 10))
		self.observation(p, other, rdt, "Positive - P. falciparum")
		self.assertCounts({"B-Confirmed Malaria": 2, "B-Clinical Malaria": 3})

	def test_positive_test_on_referring_encounter_confirms(self):
		"""Review L4. Adult: OPD encounter A orders an RDT, approved Positive, and refers internally with no
		New diagnosis; encounter B (the referral visit) diagnoses 1F40.Z with no test of its own.
		One episode, positive result in it: 705B Confirmed Malaria 1, Clinical Malaria 0."""
		p = self.patient()
		a = self.encounter(p, self.visit(p), [("MG26", "Follow-up")])
		self.observation(p, a, "Malaria Rapid Diagnostic Test", "Positive - P. falciparum", via_request=True)
		self.encounter(p, self.refer(a, p), [("1F40.Z", "New")])
		self.assertCounts({"B-Confirmed Malaria": 1})

	def test_group_priority_in_one_encounter_and_across_referral(self):
		"""Adult A: NA02.0 (skull fracture: Other Injuries) + PA00 (pedestrian road traffic event) ->
		Road Traffic Injuries 1, Other Injuries 0.
		Adult B: E1 1F40.Z with a positive RDT (Confirmed), referred; E2 1F40.Z without a test (Clinical)
		-> one episode: Confirmed 1, Clinical 0."""
		a = self.patient()
		self.encounter(a, self.visit(a), [("NA02.0", "New"), ("PA00", "New", 0)])
		b = self.patient()
		e1 = self.encounter(b, self.visit(b), [("1F40.Z", "New")])
		self.observation(b, e1, "Malaria Rapid Diagnostic Test", "Positive - P. falciparum")
		self.encounter(b, self.refer(e1, b), [("1F40.Z", "New")])
		self.assertCounts({"B-Road Traffic Injuries": 1, "B-Confirmed Malaria": 1})

	def test_unmapped_goes_to_catch_all_and_exception(self):
		"""Adult: FB56.2 (myalgia, no mapping) and a chapter 24 code (excluded). Expected 705B: All Other
		Diseases 1, and one "Unmapped diagnosis" exception referencing the encounter (no patient data).
		The excluded code is neither counted nor an exception."""
		q_code = frappe.db.get_value(
			"Code Value", {"code_system": "ICD-11", "code_value": ["like", "QA0%"]}, "code_value"
		)
		p = self.patient()
		e = self.encounter(p, self.visit(p), [("FB56.2", "New"), (q_code, "New", 0)])
		self.assertCounts({"B-All Other Diseases": 1})
		exc = [x for x in self.last_ctx.exceptions if x["kind"] == diagnosis.EXCEPTION_UNMAPPED]
		self.assertEqual(len(exc), 1)
		self.assertEqual((exc[0]["reference_doctype"], exc[0]["reference_name"]), (ENCOUNTER_DOCTYPE, e))
		self.assertIn("FB56.2", exc[0]["detail"])
		self.assertEqual(exc[0]["element"], "B-All Other Diseases")
		self.assertNotIn(p, exc[0]["detail"])

	def test_category_not_on_patient_form_goes_to_catch_all(self):
		"""Child (24 months) with BA00 (Hypertension, a 705B line): 705A All Other Diseases 1, no
		exception (it is mapped)."""
		p = self.patient(months=24)
		self.encounter(p, self.visit(p), [("BA00", "New")])
		self.assertCounts({"A-All Other Diseases": 1})
		self.assertFalse(self.last_ctx.exceptions)

	def test_age_exactly_five_years_is_705b(self):
		"""Born exactly 5 years before the encounter (60 months): 705B. 59 months: 705A."""
		five = self.patient(dob=add_years(DAY, -5))
		self.encounter(five, self.visit(five), [("1A40.Z", "New")])
		young = self.patient(months=59)
		self.encounter(young, self.visit(young), [("CA40.Z", "New")])
		self.assertCounts({"B-Diarrhoea": 1, "A-Pneumonia": 1})

	def test_missing_dob_counts_on_neither_form(self):
		"""No date of birth: 705A 0 and 705B 0, one "Missing date of birth" exception; an element with no
		age limit still counts it (1)."""
		p = self.patient(months=None)
		e = self.encounter(p, self.visit(p), [("1A40.Z", "New")])
		self.assertCounts({})
		exc = [x for x in self.last_ctx.exceptions if x["kind"] == diagnosis.EXCEPTION_NO_DOB]
		self.assertEqual([x["reference_name"] for x in exc], [e])
		self.assertCounts({"X-Diarrhoea": 1}, specs=[el_any("Diarrhoea")])

	def test_draft_registered_in_error_and_other_facility_excluded(self):
		"""Three adults with Diarrhoea: a draft encounter, a visit closed "Registered in error", and a visit
		at the second facility. Expected for facility 1: 0. With no facility filter: 1 (the other
		facility's)."""
		a = self.patient()
		self.encounter(a, self.visit(a), [("1A40.Z", "New")], docstatus=0)
		b = self.patient()
		self.encounter(b, self.visit(b, closure_reason="Registered in error"), [("1A40.Z", "New")])
		c = self.patient()
		self.encounter(c, self.visit(c, facility=get_facility(1)), [("1A40.Z", "New")])
		self.assertCounts({})
		self.assertCounts({"B-Diarrhoea": 1}, ctx=SourceContext("T-R2", FROM, TO))

	def test_unknown_category_in_rule_is_a_configuration_exception(self):
		ctx = self.ctx()
		self.assertEqual(diagnosis.evaluate(ctx, [el_a("T-No Such Category")]), {})
		self.assertEqual(ctx.exceptions[0]["kind"], diagnosis.EXCEPTION_CONFIG)


class TestMapping(EMRTestCase):
	def category(self, name: str, **kw) -> str:
		frappe.get_doc({"doctype": MOH_CATEGORY_DOCTYPE, "category_name": name, "title": name, **kw}).insert()
		return name

	def map(self, category: str, match_type: str, value: str) -> str:
		doc = frappe.get_doc(
			{"doctype": MOH_MAPPING_DOCTYPE, "category": category, "match_type": match_type, "value": value}
		).insert()
		return doc.name

	def test_precedence_exact_longest_prefix_deepest_block_catch_all(self):
		"""Exact 8A80.10 > Prefix 8A80.1 > Prefix 8A8 > Block BlockL1-8A8; BlockL2-9A7 (depth 2) beats
		BlockL1-9A6 (depth 1); an unmapped code goes to the catch-all."""
		for c in ("T-Exact", "T-Long", "T-Short", "T-Block1", "T-Block2", "T-HeadBlock"):
			self.category(c)
		self.map("T-Exact", mapping.MATCH_EXACT, "8A80.10")
		self.map("T-Long", mapping.MATCH_PREFIX, "8A80.1")
		self.map("T-Short", mapping.MATCH_PREFIX, "8A8")
		self.map("T-HeadBlock", mapping.MATCH_BLOCK, "BlockL1-8A8")
		self.map("T-Block1", mapping.MATCH_BLOCK, "BlockL1-9A6")
		self.map("T-Block2", mapping.MATCH_BLOCK, "BlockL2-9A7")
		r = mapping.Resolver()
		self.assertEqual(mapping.resolve(cv("8A80.10"), r), ("T-Exact", mapping.MATCH_EXACT, "8A80.10"))
		self.assertEqual(mapping.resolve("8A80.1Z", r), ("T-Long", mapping.MATCH_PREFIX, "8A80.1"))
		self.assertEqual(mapping.resolve("8A80.0", r), ("T-Short", mapping.MATCH_PREFIX, "8A8"))
		self.assertEqual(mapping.resolve("9A72", r), ("T-Block2", mapping.MATCH_BLOCK, "BlockL2-9A7"))
		self.assertEqual(mapping.resolve("9A90", r), ("T-Block1", mapping.MATCH_BLOCK, "BlockL1-9A6"))
		self.assertEqual(
			mapping.resolve("FB56.2", r), ("All Other Diseases", mapping.MATCHED_CATCH_ALL, None)
		)
		# a disabled mapping is ignored
		frappe.db.set_value(
			MOH_MAPPING_DOCTYPE, mapping.mapping_name(mapping.MATCH_EXACT, "8A80.10"), "enabled", 0
		)
		self.assertEqual(mapping.resolve("8A80.10")[0], "T-Long")

	def test_seeded_resolution(self):
		r = mapping.Resolver()
		self.assertEqual(
			mapping.resolve("1F40.Z", r), ("Confirmed Malaria", mapping.MATCH_BLOCK, "BlockL2-1F4")
		)
		self.assertEqual(mapping.resolve("1A07.Z", r), ("Typhoid Fever", mapping.MATCH_PREFIX, "1A07"))
		self.assertEqual(mapping.resolve("1A09", r)[0], "Diarrhoea")  # block BlockL2-1A0
		self.assertEqual(
			mapping.resolve("3A80", r)[0], "All Other Diseases"
		)  # polycythaemia, mapped on purpose
		self.assertEqual(mapping.resolve("3A80", r)[1], mapping.MATCH_BLOCK)

	def test_mapping_values_are_validated(self):
		self.category("T-Val")
		for match_type, value in (
			(mapping.MATCH_EXACT, "1F40.9"),
			(mapping.MATCH_PREFIX, "ZZ9"),
			(mapping.MATCH_BLOCK, "BlockL9-1F4"),
			(mapping.MATCH_PREFIX, "1F%"),
		):
			with self.assertRaises(frappe.ValidationError):
				self.map("T-Val", match_type, value)
		name = self.map("T-Val", mapping.MATCH_EXACT, "1f40.z")  # normalised to upper case
		self.assertEqual(name, "EXACT-1F40.Z")
		self.assertEqual(frappe.db.get_value(MOH_MAPPING_DOCTYPE, name, "matching_codes"), 1)
		with self.assertRaises(frappe.DuplicateEntryError):
			self.map("T-Val", mapping.MATCH_EXACT, "1F40.Z")

	def test_category_rules(self):
		with self.assertRaises(frappe.ValidationError):
			self.category("T-Second Catch-all", is_catch_all=1)
		with self.assertRaises(frappe.ValidationError):
			self.category("T-Lab", requires_lab_confirmation=1)

	def test_seed_refresh_never_overwrites_edits(self):
		"""Seed v2 changes the titles of Measles, Mumps and Plague and disables mapping PREFIX-1D80.
		Measles was edited in the desk (marker Edited), Plague changed outside the form (hash differs):
		both keep their values; Mumps and the mapping are updated."""
		measles = frappe.get_doc(MOH_CATEGORY_DOCTYPE, "Measles")
		self.assertEqual(measles.seed_marker, "Seed v1")
		measles.title = "Measles (HIS wording)"
		measles.save()
		self.assertEqual(measles.seed_marker, mapping.SEED_EDITED)
		frappe.db.set_value(MOH_CATEGORY_DOCTYPE, "Plague", "title", "Plague (edited by SQL)")

		data = copy.deepcopy(mapping.load_seed())
		data["seed_version"] = "Seed v2"
		for c in data["categories"]:
			if c["name"] in ("Measles", "Mumps", "Plague"):
				c["title"] = f"{c['name']} v2"
			if c["name"] == "Mumps":
				c["mappings"][0]["enabled"] = 0
		counts = mapping.install(data)
		self.assertEqual(counts["failed"], 0)
		get = frappe.db.get_value
		self.assertEqual(get(MOH_CATEGORY_DOCTYPE, "Measles", "title"), "Measles (HIS wording)")
		self.assertEqual(
			get(MOH_CATEGORY_DOCTYPE, "Plague", ["title", "seed_marker"]),
			("Plague (edited by SQL)", "Edited"),
		)
		self.assertEqual(
			get(MOH_CATEGORY_DOCTYPE, "Mumps", ["title", "seed_marker"]), ("Mumps v2", "Seed v2")
		)
		self.assertEqual(get(MOH_MAPPING_DOCTYPE, "PREFIX-1D80", ["enabled", "seed_marker"]), (0, "Seed v2"))
		# a re-run changes nothing
		again = mapping.install(data)
		self.assertEqual((again["created"], again["updated"], again["failed"]), (0, 0, 0))

	def test_seed_installed(self):
		categories, mappings = mapping.seed_rows(mapping.load_seed())
		for row in categories:
			self.assertTrue(frappe.db.exists(MOH_CATEGORY_DOCTYPE, row["name"]), row["name"])
			self.assertIn(mapping.PROVISIONAL_NOTE, row["note"])
		for row in mappings:
			self.assertTrue(frappe.db.exists(MOH_MAPPING_DOCTYPE, row["name"]), row["name"])
		self.assertEqual(frappe.db.count(MOH_CATEGORY_DOCTYPE, {"is_catch_all": 1}), 1)
		self.assertEqual(
			mapping.install(),
			{
				"created": 0,
				"updated": 0,
				"kept": 0,
				"unchanged": len(categories) + len(mappings),
				"failed": 0,
			},
		)

	def test_fingerprint_follows_the_mapping(self):
		before = mapping.fingerprint()
		self.assertEqual(before, mapping.fingerprint())
		self.category("T-FP")
		self.map("T-FP", mapping.MATCH_EXACT, "FB56.2")
		self.assertNotEqual(before, mapping.fingerprint())


class TestSeedFiles(EMRTestCase):
	def test_every_stem_exists_in_the_bundled_release(self):
		"""OI-5 provisional stems: every Exact code, Prefix and Block of the seed is in the WHO 2025-01 file."""
		codes, blocks = set(), set()
		for entry in icd11.parse_rows(icd11.read_rows(icd11.BUNDLED_FILE)):
			if entry["code"]:
				codes.add(entry["code"])
			elif entry["block"]:
				blocks.add(entry["block"])
		_categories, mappings = mapping.seed_rows(mapping.load_seed())
		self.assertGreater(len(mappings), 100)
		for m in mappings:
			value = m["value"]
			if m["match_type"] == mapping.MATCH_EXACT:
				ok = value in codes
			elif m["match_type"] == mapping.MATCH_PREFIX:
				ok = any(c.startswith(value) for c in codes)
			else:
				ok = value in blocks
			self.assertTrue(ok, f"{m['match_type']} {value} ({m['category']}) is not in the bundled release")

	def test_definitions_reference_seeded_categories(self):
		"""moh_705a/705b.json: every Available element names a seeded category; 705A has an age maximum of
		60 months, 705B a minimum of 60; both are provisional."""
		import json

		data_dir = os.path.dirname(mapping.SEED_FILE)
		names = {c["name"] for c in mapping.load_seed()["categories"]}
		for fname, key in (("moh_705a.json", "age_max_months"), ("moh_705b.json", "age_min_months")):
			with open(os.path.join(data_dir, fname)) as f:
				d = json.load(f)
			self.assertEqual(d["status"], "Active - Provisional")
			self.assertEqual(d["note"], mapping.PROVISIONAL_NOTE)
			for el in d["elements"]:
				self.assertEqual(el["source_type"], SRC)
				if el["availability"] == "Available":
					self.assertIn(el["rule"]["category"], names)
					self.assertEqual(el["rule"][key], 60)
				else:
					self.assertTrue(el["gap_note"])


class TestBlockStructure(EMRTestCase):
	def test_blocks_and_backfill(self):
		"""The backfill restores a code's block ids and a missing block, set-based, and a re-run is a no-op."""
		frappe.db.sql(
			"UPDATE `tabCode Value` SET krcs_blocks = NULL WHERE name IN (%s, %s)", (cv("1F40.Z"), cv("BA00"))
		)
		frappe.db.sql(f"DELETE FROM `tab{ICD11_BLOCK_DOCTYPE}` WHERE name = 'BlockL2-1F4'")
		counts = icd11.ensure_structure(force=True)
		self.assertEqual((counts["blocks_created"], counts["codes_structured"]), (1, 2))
		self.assertEqual(
			frappe.db.get_value("Code Value", cv("1F40.Z"), "krcs_blocks"), "BlockL1-1F4,BlockL2-1F4"
		)
		self.assertEqual(frappe.db.get_value("Code Value", cv("BA00"), "krcs_blocks"), "BlockL1-BA0")
		self.assertEqual(frappe.db.get_value("Code Value", cv("1F40.Z"), "krcs_primary_tabulation"), 1)
		block = frappe.db.get_value(
			ICD11_BLOCK_DOCTYPE, "BlockL2-1F4", ["title", "chapter", "parent_block", "depth"]
		)
		self.assertEqual(block, ("Malaria", "01", "BlockL1-1F4", 2))
		again = icd11.ensure_structure(force=True)
		self.assertEqual(
			(again["blocks_created"], again["blocks_updated"], again["codes_structured"]), (0, 0, 0)
		)
		self.assertIsNone(icd11.ensure_structure())  # once per release
		self.assertEqual(frappe.db.count(ICD11_BLOCK_DOCTYPE), 788)

	def test_patch_runs(self):
		from onerc_healthcare.patches import icd11_block_backfill

		icd11_block_backfill.execute()
		self.assertTrue(frappe.db.exists(ICD11_BLOCK_DOCTYPE, "BlockL1-1A0"))


class TestEndpoints(EMRTestCase):
	def setUp(self):
		super().setUp()
		self.his = make_user("r2-his", [ROLE_HIS])
		self.mic = make_user("r2-mic", [ROLE_MIC])
		self.clinician = make_user("r2-co", [ROLE_CLINICIAN])

	def test_test_code_resolver(self):
		for user in (self.his, self.mic):
			with as_user(user):
				out = mapping.test_code("1F40.Z")
			self.assertEqual(
				(out["category"], out["matched_by"], out["value"], out["mapping"]),
				("Confirmed Malaria", mapping.MATCH_BLOCK, "BlockL2-1F4", "BLOCK-BlockL2-1F4"),
			)
			self.assertTrue(out["requires_lab_confirmation"])
		with as_user(self.his):
			self.assertTrue(mapping.test_code(cv("FB56.2"))["unmapped"])
		with as_user(self.clinician), self.assertRaises(frappe.PermissionError):
			mapping.test_code("1F40.Z")

	def test_bulk_map_is_his_only(self):
		for user in (self.mic, self.clinician):
			with as_user(user), self.assertRaises(frappe.PermissionError):
				mapping.map_codes('["FB56.2"]', "Arthritis and Joint Pains")
		self.assertFalse(frappe.db.exists(MOH_MAPPING_DOCTYPE, "EXACT-FB56.2"))
		with as_user(self.his):
			out = mapping.map_codes([cv("FB56.2"), "FB56.4", "1F40.Z"], "Arthritis and Joint Pains")
		self.assertEqual(sorted(out["created"]), ["EXACT-1F40.Z", "EXACT-FB56.2", "EXACT-FB56.4"])
		self.assertEqual(mapping.resolve("FB56.2")[:2], ("Arthritis and Joint Pains", mapping.MATCH_EXACT))
		with as_user(self.his):
			again = mapping.map_codes(["FB56.2", "NOPE"], "Arthritis and Joint Pains")
		self.assertEqual(again["created"], [])
		self.assertEqual(len(again["skipped"]), 2)

	def test_http_verbs(self):
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[mapping.test_code], ["GET"])
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[mapping.map_codes], ["POST"])

	def test_reports_are_his_and_mic_only(self):
		from onerc_healthcare.emr_reporting.report.moh_mapping_coverage import moh_mapping_coverage
		from onerc_healthcare.emr_reporting.report.moh_unmapped_diagnoses import moh_unmapped_diagnoses

		filters = {"from_date": "2019-03-01", "to_date": "2019-03-31"}
		with as_user(self.mic):
			moh_unmapped_diagnoses.execute(filters)
			_cols, rows, *_ = moh_mapping_coverage.execute(filters)
		self.assertTrue(any(r["category"] == "All Other Diseases" for r in rows))
		with as_user(self.clinician):
			for module in (moh_unmapped_diagnoses, moh_mapping_coverage):
				with self.assertRaises(frappe.PermissionError):
					module.execute(filters)


class TestReportsCount(Counting):
	def test_unmapped_and_coverage_reports(self):
		"""Two adults with FB56.2 (unmapped) and one with 1A40.Z: Unmapped lists FB56.2 with 2 encounters;
		Coverage (codes view) shows 1A40.Z by Prefix and FB56.2 by Catch-all."""
		from onerc_healthcare.emr_reporting.report.moh_mapping_coverage import moh_mapping_coverage
		from onerc_healthcare.emr_reporting.report.moh_unmapped_diagnoses import moh_unmapped_diagnoses

		for code in ("FB56.2", "FB56.2", "1A40.Z"):
			p = self.patient()
			self.encounter(p, self.visit(p), [(code, "New")])
		filters = {"from_date": "2019-03-01", "to_date": "2019-03-31", "facility": self.facility}
		_cols, rows, _msg = moh_unmapped_diagnoses.execute(filters)
		self.assertEqual([(r["code_value"], r["encounters"]) for r in rows], [("FB56.2", 2)])
		_cols, rows, *_ = moh_mapping_coverage.execute({**filters, "view": "Codes used"})
		got = {r["code_value"]: (r["matched_by"], r["diagnoses"]) for r in rows}
		self.assertEqual(got, {"FB56.2": (mapping.MATCHED_CATCH_ALL, 2), "1A40.Z": (mapping.MATCH_PREFIX, 1)})
