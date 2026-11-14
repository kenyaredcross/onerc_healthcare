# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""R1: MOH reporting framework (BR-9, FR-29, FR-30; SDD §5.8).

Definitions and seeds, the engine (age bands, disaggregation, totals, missing sources), the MOH Report
Run lifecycle by role, exports, and the read-only aggregate API. A fake source is registered by patching
`registry.SOURCE_TYPES`, so nothing here depends on R2 or R4. Expected numbers are worked out by hand
from the fake source's rules (see `PEOPLE_*`).
"""

from __future__ import annotations

import io
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import add_days, getdate, today

from onerc_healthcare.constants import (
	MOH_REPORT_DOCTYPE,
	MOH_REPORT_STATUSES,
	MOH_RUN_DOCTYPE,
	ROLE_HEAD_OPS,
	ROLE_HIS,
	ROLE_MIC,
	ROLE_MOH_API,
	ROLE_RECEPTIONIST,
)
from onerc_healthcare.reporting import api, definitions, engine, registry
from onerc_healthcare.reporting import run as run_module
from onerc_healthcare.reporting.ages import UNKNOWN_BAND
from onerc_healthcare.reporting.period import (
	InvalidPeriodError,
	month_period,
	period_label,
	previous_period,
	quarter_period,
	validate_period,
)
from onerc_healthcare.reporting.run import (
	DuplicateRunError,
	IllegalTransitionError,
	LockedFieldError,
	ReportBlockedError,
)
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import get_facility, make_user

FAKE_SOURCE = "Visit Attendance"  # an existing key, pointed at the fake evaluator below
MISSING_MODULE_SOURCE = "Referrals"
MISSING_FUNCTION_SOURCE = "Laboratory Tests"
FAKE_PATHS = {
	FAKE_SOURCE: "onerc_healthcare.tests.test_reporting_framework.fake_evaluate",
	MISSING_MODULE_SOURCE: "onerc_healthcare.reporting.t_no_such_module.evaluate",
	MISSING_FUNCTION_SOURCE: "onerc_healthcare.reporting.registry.t_no_such_function",
}
CALLS: list[list[str]] = []

BANDS = [
	{"label": "<1", "min_months": 0, "max_months": 12},
	{"label": "1-4", "min_months": 12, "max_months": 60},
	{"label": "5+", "min_months": 60, "max_months": None},
]
# (age in months or None, sex). Hand-computed cells next to each list.
PEOPLE_AGE = [[3, "Female"], [11, "Male"], [12, "Female"], [59, "Male"], [60, "Female"], [None, "Male"]]
# T-001 by age band: <1 = 2 (3, 11), 1-4 = 2 (12, 59), 5+ = 1 (60), Age unknown = 1; total 6
PEOPLE_AGE_SEX = [[3, "Female"], [5, "Female"], [30, "Male"], [70, "Male"], [70, "Female"]]
# T-002: <1/F 2, 1-4/M 1, 5+/M 1, 5+/F 1; bands <1 2, 1-4 1, 5+ 2; sexes F 3, M 2; total 5
PEOPLE_SEX = [[1, "Female"], [100, "Female"], [100, "Male"]]
# T-003: F 2, M 1; total 3
PEOPLE_NONE = [[1, "Female"], [2, "Male"], [3, "Male"], [4, "Female"]]
# T-004: 4


def fake_evaluate(ctx: registry.SourceContext, elements: list[registry.ElementSpec]) -> dict:
	"""Counts `rule["people"]` into the element's cells, as a real source counts records."""
	from onerc_healthcare.reporting.ages import band_for

	CALLS.append([e.code for e in elements])
	cells: dict = {}
	for el in elements:
		for months, sex in el.rule.get("people", []):
			key = registry.cell_key(el, band_for(months, ctx.age_bands), sex)
			cells[key] = cells.get(key, 0) + 1
		if el.rule.get("exception"):
			ctx.add_exception("Test", el.rule["exception"], "Patient Visit", "T-PV-0001", el.code)
		if el.rule.get("bad_cell"):
			cells[(el.code, "", "")] = 7  # the element is disaggregated by age: refused by the engine
		if el.rule.get("negative"):
			cells[registry.cell_key(el)] = -1
	return cells


def element(code, label, section, sort_order, disaggregation="None", source=FAKE_SOURCE, **rule):
	return {
		"code": code,
		"label": label,
		"section": section,
		"sort_order": sort_order,
		"source_type": source,
		"rule": rule or {},
		"disaggregation": disaggregation,
		"availability": "Available",
		"gap_note": None,
		"dhis2_uid": None,
	}


def definition(name="T-MOH TEST", status=MOH_REPORT_STATUSES[0], **kw) -> dict:
	data = {
		"name": name,
		"title": "T- Test Report",
		"status": status,
		"version": "t-1",
		"period_type": "Monthly",
		"note": "PROVISIONAL — HIS Officer to confirm (OI-5)",
		"blocked_reason": "T- not yet" if status == "Blocked" else None,
		"age_bands": BANDS,
		"dimensions": [],
		"elements": [
			element("T-001", "Age only", "Section A", 1, "Age Band", people=PEOPLE_AGE),
			element("T-002", "Age and sex", "Section A", 2, "Age Band and Sex", people=PEOPLE_AGE_SEX),
			element("T-003", "Sex only", "Section B", 3, "Sex", people=PEOPLE_SEX),
			element("T-004", "Plain count", "Section B", 4, people=PEOPLE_NONE),
			{
				**element("T-005", "Not captured", "Section B", 5),
				"availability": "Gap",
				"source_type": None,
				"gap_note": "T- not captured yet",
			},
			element("T-006", "Missing module", "Section C", 6, source=MISSING_MODULE_SOURCE),
			element("T-007", "With exception", "Section C", 7, exception="T- unmapped code X"),
			element("T-008", "Missing function", "Section C", 8, source=MISSING_FUNCTION_SOURCE),
		],
	}
	data.update(kw)
	return data


@contextmanager
def as_user(user: str):
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user("Administrator")


class ReportingTestCase(EMRTestCase):
	def setUp(self):
		super().setUp()
		self._patch = patch.dict(registry.SOURCE_TYPES, FAKE_PATHS)
		self._patch.start()
		CALLS.clear()
		self.his = make_user("r1-his", [ROLE_HIS])
		self.mic = make_user("r1-mic", [ROLE_MIC])
		self.head_ops = make_user("r1-headops", [ROLE_HEAD_OPS])
		self.reception = make_user("r1-reception", [ROLE_RECEPTIONIST])
		self.moh_api = make_user("r1-mohapi", [ROLE_MOH_API])
		frappe.db.set_value("User", self.his, "full_name", "T-HIS Officer One", update_modified=False)
		if frappe.get_meta("User").has_field("krcs_designation"):
			frappe.db.set_value("User", self.his, "krcs_designation", "T-HRIO", update_modified=False)
		self.facility = get_facility(0)
		self.from_date, self.to_date = previous_period("Monthly")
		definitions.sync_definitions([definition()])

	def tearDown(self):
		self._patch.stop()
		super().tearDown()

	def new_run(
		self,
		user=None,
		report="T-MOH TEST",
		facility=None,
		from_date=None,
		to_date=None,
		period_type="Monthly",
	):
		with as_user(user or self.his):
			return frappe.get_doc(
				{
					"doctype": MOH_RUN_DOCTYPE,
					"report": report,
					"facility": facility or self.facility,
					"period_type": period_type,
					"from_date": from_date or self.from_date,
					"to_date": to_date or self.to_date,
				}
			).insert()

	def act(self, run, action, user=None, reason=None):
		with as_user(user or self.his):
			return run_module.apply_action(run, action, reason=reason)

	def generated_run(self, **kw):
		name = self.new_run(**kw).name
		self.act(name, "generate")
		return frappe.get_doc(MOH_RUN_DOCTYPE, name)

	def status(self, run):
		return frappe.db.get_value(MOH_RUN_DOCTYPE, run, "status")


class TestPeriod(EMRTestCase):
	def test_labels(self):
		self.assertEqual(period_label("2026-11-01", "2026-11-30"), "November 2026")
		self.assertEqual(period_label("2026-10-01", "2026-12-31"), "Q4 2026")
		self.assertEqual(period_label("2026-11-01", "2026-11-15"), "1 Nov 2026 to 15 Nov 2026")
		self.assertEqual(period_label("2026-11-03", "2026-11-03"), "3 Nov 2026")
		self.assertEqual(month_period(2024, 2)[1], getdate("2024-02-29"))
		self.assertEqual(quarter_period(2026, 4), (getdate("2026-10-01"), getdate("2026-12-31")))
		self.assertEqual(previous_period("Monthly", "2026-11-14"), month_period(2026, 10))
		self.assertEqual(previous_period("Quarterly", "2026-11-14"), quarter_period(2026, 3))

	def test_validation(self):
		validate_period("2026-10-01", "2026-10-31", "Monthly", on_date="2026-11-14")
		validate_period("2026-07-01", "2026-09-30", "Quarterly", on_date="2026-11-14")
		validate_period("2026-10-05", "2026-11-14", "Custom", on_date="2026-11-14")
		for args in (
			("2026-10-31", "2026-10-01", "Custom"),  # reversed
			("2026-11-01", "2026-11-30", "Monthly"),  # ends after today
			("2026-11-14", "2026-11-15", "Custom"),  # ends after today
			("2026-10-02", "2026-10-31", "Monthly"),  # not a whole month
			("2026-08-01", "2026-10-31", "Quarterly"),  # not a calendar quarter
			("2026-10-01", "2026-10-31", "Weekly"),  # unknown type
		):
			with self.assertRaises(InvalidPeriodError, msg=args):
				validate_period(*args, on_date="2026-11-14")


class TestDefinitions(ReportingTestCase):
	def test_insert_then_unchanged(self):
		doc = frappe.get_doc(MOH_REPORT_DOCTYPE, "T-MOH TEST")
		self.assertEqual(len(doc.elements), 8)
		self.assertEqual([b.label for b in doc.age_bands], ["<1", "1-4", "5+"])
		self.assertEqual(doc.age_bands[2].max_months, 0)  # no upper limit
		self.assertEqual(doc.seed_hash, definitions.definition_hash(doc))
		self.assertEqual(definitions.definition_hash(doc), definitions.definition_hash(definition()))
		self.assertEqual(definitions.sync_definitions([definition()]), {"T-MOH TEST": "unchanged"})

	def test_new_seed_updates_an_unedited_definition(self):
		self.assertEqual(
			definitions.sync_definitions([definition(title="T- Renamed")]), {"T-MOH TEST": "updated"}
		)
		doc = frappe.get_doc(MOH_REPORT_DOCTYPE, "T-MOH TEST")
		self.assertEqual(doc.title, "T- Renamed")
		self.assertEqual(doc.seed_hash, definitions.definition_hash(doc))

	def test_his_edit_is_never_overwritten(self):
		with as_user(self.his):
			doc = frappe.get_doc(MOH_REPORT_DOCTYPE, "T-MOH TEST")
			doc.elements[0].label = "T- HIS label"
			doc.status = "Confirmed"
			doc.save()
		result = definitions.sync_definitions([definition(title="T- Renamed by seed")])
		self.assertEqual(result, {"T-MOH TEST": "kept"})
		doc = frappe.get_doc(MOH_REPORT_DOCTYPE, "T-MOH TEST")
		self.assertEqual(doc.elements[0].label, "T- HIS label")
		self.assertEqual(doc.title, "T- Test Report")
		self.assertEqual(doc.status, "Confirmed")

	def test_hand_made_definition_is_never_touched(self):
		frappe.db.set_value(MOH_REPORT_DOCTYPE, "T-MOH TEST", "seed_hash", None)
		self.assertEqual(definitions.sync_definitions([definition(title="T- x")]), {"T-MOH TEST": "kept"})

	def test_seed_files(self):
		with tempfile.TemporaryDirectory() as tmp:
			files = {
				"a_report.json": definition(name="T-MOH FILE A"),
				"b_categories.json": [{"category": "Diarrhoea", "group": "T-"}],  # not a definition
				"c_blocked.json": [
					definition(name="T-MOH FILE B", status="Blocked", elements=[]),
					{"name": "T-not-a-definition"},
				],
				"d_wrapped.json": {"definitions": [definition(name="T-MOH FILE C")]},
			}
			for fname, data in files.items():
				with open(os.path.join(tmp, fname), "w") as fh:
					json.dump(data, fh)
			with open(os.path.join(tmp, "e_broken.json"), "w") as fh:
				fh.write("{not json")
			loaded = definitions.load_seed_files(tmp)
			self.assertEqual([d["name"] for d in loaded], ["T-MOH FILE A", "T-MOH FILE B", "T-MOH FILE C"])
			result = definitions.sync_definitions(directory=tmp)
		self.assertEqual(set(result.values()), {"inserted"})
		self.assertEqual(frappe.db.get_value(MOH_REPORT_DOCTYPE, "T-MOH FILE B", "status"), "Blocked")

	def test_invalid_seed_is_skipped(self):
		bad = definition(name="T-MOH BAD")
		bad["elements"][0]["source_type"] = "T- no such source"
		result = definitions.sync_definitions([bad, definition(name="T-MOH GOOD")])
		self.assertEqual(result, {"T-MOH BAD": "error", "T-MOH GOOD": "inserted"})
		self.assertFalse(frappe.db.exists(MOH_REPORT_DOCTYPE, "T-MOH BAD"))

	def test_definition_validation(self):
		from onerc_healthcare.emr_reporting.doctype.moh_report.moh_report import MOHReportDefinitionError

		doc = frappe.get_doc(MOH_REPORT_DOCTYPE, "T-MOH TEST")
		doc.elements[0].rule = "{not json"
		self.assertRaises(MOHReportDefinitionError, doc.save)
		doc.reload()
		doc.elements[0].rule = "[1, 2]"
		self.assertRaises(MOHReportDefinitionError, doc.save)
		doc.reload()
		doc.elements[1].code = "T-001"
		self.assertRaises(MOHReportDefinitionError, doc.save)
		doc.reload()
		doc.age_bands[0].max_months = 0
		doc.age_bands[1].min_months = 30
		doc.age_bands[1].max_months = 20
		self.assertRaises(MOHReportDefinitionError, doc.save)

	def test_workload_rules_checked_at_save(self):
		"""A bad rule for a workload source (R4 `validate_rule`) is refused when the HIS Officer saves."""
		from onerc_healthcare.emr_reporting.doctype.moh_report.moh_report import MOHReportDefinitionError

		try:
			from onerc_healthcare.reporting.workload import validate_rule
		except ImportError:
			self.skipTest("workload.validate_rule not available")
		bad = definition(name="T-MOH RULE")
		bad["elements"] = [element("T-201", "Rx", "S", 1, source="Prescriptions", t_unknown_key=1)]
		self.assertEqual(definitions.sync_definitions([bad]), {"T-MOH RULE": "error"})
		doc = frappe.get_doc(MOH_REPORT_DOCTYPE, "T-MOH TEST")
		doc.append(
			"elements",
			{"code": "T-202", "label": "Rx", "source_type": "Prescriptions", "rule": '{"t_unknown_key": 1}'},
		)
		self.assertRaises(MOHReportDefinitionError, doc.save)
		doc.reload()
		# Gap elements (source and rule null, as R4 seeds them) are still accepted.
		doc.append("elements", {"code": "T-203", "label": "Gap", "availability": "Gap", "gap_note": "T-"})
		doc.save()

	def test_source_options_match_registry(self):
		df = frappe.get_meta("MOH Report Data Element").get_field("source_type")
		self.assertEqual([o for o in df.options.split("\n") if o], list(registry.SOURCE_TYPES))
		status = frappe.get_meta(MOH_REPORT_DOCTYPE).get_field("status")
		self.assertEqual(status.options.split("\n"), list(MOH_REPORT_STATUSES))


class TestEngine(ReportingTestCase):
	def evaluate(self, report="T-MOH TEST"):
		ctx = registry.SourceContext(
			report=report,
			from_date=self.from_date,
			to_date=self.to_date,
			age_bands=engine.age_bands_of(report),
		)
		return ctx, engine.evaluate_report(report, ctx)

	def test_cells_and_totals(self):
		ctx, result = self.evaluate()
		cells = result.cells
		expected = {
			# T-001, age bands
			("T-001", "<1", ""): 2,
			("T-001", "1-4", ""): 2,
			("T-001", "5+", ""): 1,
			("T-001", UNKNOWN_BAND, ""): 1,
			("T-001", "", ""): 6,
			# T-002, age band and sex
			("T-002", "<1", "Female"): 2,
			("T-002", "1-4", "Male"): 1,
			("T-002", "5+", "Male"): 1,
			("T-002", "5+", "Female"): 1,
			("T-002", "<1", ""): 2,
			("T-002", "1-4", ""): 1,
			("T-002", "5+", ""): 2,
			("T-002", "", "Female"): 3,
			("T-002", "", "Male"): 2,
			("T-002", "", ""): 5,
			# T-003, sex
			("T-003", "", "Female"): 2,
			("T-003", "", "Male"): 1,
			("T-003", "", ""): 3,
			# T-004, plain
			("T-004", "", ""): 4,
			# T-007 has no people: its total is 0 and no cell is stored
		}
		self.assertEqual(cells, expected)
		self.assertEqual(CALLS, [["T-001", "T-002", "T-003", "T-004", "T-007"]])  # Gap T-005 never evaluated
		self.assertEqual(set(result.unavailable), {"T-006", "T-008"})
		kinds = [e["kind"] for e in ctx.exceptions]
		self.assertEqual(kinds.count(engine.EXCEPTION_SOURCE_UNAVAILABLE), 2)
		test_exc = next(e for e in ctx.exceptions if e["kind"] == "Test")
		self.assertEqual(
			(test_exc["reference_doctype"], test_exc["reference_name"], test_exc["element"]),
			("Patient Visit", "T-PV-0001", "T-007"),
		)

	def test_engine_refuses_bad_cells(self):
		data = definition(
			name="T-MOH BADCELL",
			elements=[
				element("T-101", "Bad", "S", 1, "Age Band", people=[[3, "Male"]], bad_cell=True),
				element("T-102", "Negative", "S", 2, negative=True),
			],
		)
		definitions.sync_definitions([data])
		ctx, result = self.evaluate("T-MOH BADCELL")
		self.assertEqual(result.cells, {("T-101", "<1", ""): 1, ("T-101", "", ""): 1})
		self.assertEqual([e["kind"] for e in ctx.exceptions], [engine.EXCEPTION_ENGINE] * 2)

	def test_layout(self):
		run = self.generated_run()
		sections = engine.build_layout(engine.snapshot_of(run))
		self.assertEqual([s["title"] for s in sections], ["Section A", "Section B", "Section C"])
		age_block = sections[0]["blocks"][0]
		self.assertEqual([c[0] for c in age_block["columns"]], ["<1", "1-4", "5+", UNKNOWN_BAND, "Total"])
		self.assertEqual(age_block["rows"][0]["values"], [2, 2, 1, 1, 6])
		age_sex = sections[0]["blocks"][1]
		self.assertEqual(
			age_sex["rows"][0]["values"],
			# <1 M F, 1-4 M F, 5+ M F, Total M F All
			[0, 2, 1, 0, 1, 1, 2, 3, 5],
		)
		gap = next(r for r in sections[1]["blocks"][-1]["rows"] if r["code"] == "T-005")
		self.assertTrue(gap["blank"])
		self.assertEqual(gap["values"], [None])
		self.assertIn("T- not captured yet", gap["note"])


class TestRunLifecycle(ReportingTestCase):
	def test_transitions_are_introspectable(self):
		self.assertEqual(set(run_module.TRANSITIONS), {"create", "generate", "submit_run", "revise", "void"})
		for rule in run_module.TRANSITIONS.values():
			self.assertEqual(rule.roles, (ROLE_HIS,))
		self.assertEqual(run_module.TRANSITIONS["void"].required, ("reason",))

	def test_create(self):
		doc = self.new_run()
		self.assertEqual(doc.status, "Draft")
		self.assertEqual(
			doc.company, frappe.db.get_value("Healthcare Service Unit", self.facility, "company")
		)
		self.assertEqual(doc.period_label, period_label(self.from_date, self.to_date))
		self.assertEqual(
			[(r.from_status, r.to_status, r.action) for r in doc.log], [(None, "Draft", "create")]
		)
		for user in (self.mic, self.head_ops, self.reception):
			with self.assertRaises(frappe.PermissionError, msg=user):
				self.new_run(user=user, facility=get_facility(1))

	def test_create_refusals(self):
		with self.assertRaises(InvalidPeriodError):
			self.new_run(from_date=today(), to_date=add_days(today(), 1), period_type="Custom")
		ward = frappe.db.get_value("Healthcare Service Unit", {"is_group": 0}, "name")
		if ward:
			with self.assertRaises(frappe.ValidationError):
				self.new_run(facility=ward)
		definitions.sync_definitions([definition(name="T-MOH BLOCKED", status="Blocked", elements=[])])
		with self.assertRaises(ReportBlockedError):
			self.new_run(report="T-MOH BLOCKED")

	def test_one_active_run_per_period(self):
		first = self.new_run().name
		with self.assertRaises(DuplicateRunError):
			self.new_run()
		self.new_run(facility=get_facility(1))  # another facility
		other_start, other_end = month_period(self.from_date.year - 1, self.from_date.month)
		self.new_run(from_date=other_start, to_date=other_end)  # another period
		self.act(first, "void", reason="T- duplicate")
		self.assertEqual(self.new_run().status, "Draft")  # the period is free again

	def test_generate_and_regenerate(self):
		doc = self.generated_run()
		self.assertEqual(doc.status, "Generated")
		self.assertEqual(doc.generated_by, self.his)
		self.assertEqual(doc.compiled_by, self.his)
		self.assertEqual(doc.report_version, "t-1")
		self.assertTrue(doc.xlsx_file.startswith("/private/files/"))
		self.assertTrue(doc.pdf_file.startswith("/private/files/"))
		self.assertEqual(doc.exception_count, 3)  # two unavailable sources, one test exception
		snap = engine.snapshot_of(doc)
		self.assertIn("definition", snap["fingerprints"])
		self.assertNotIn("exceptions", snap)
		first_xlsx = doc.xlsx_file
		self.act(doc.name, "generate")  # regenerate while Generated
		doc.reload()
		self.assertEqual(doc.status, "Generated")
		self.assertEqual([r.action for r in doc.log], ["create", "generate", "generate"])
		self.assertEqual(doc.log[-1].comment, "Regenerated")
		self.assertNotEqual(doc.xlsx_file, first_xlsx)

	def test_generate_denied_by_role(self):
		name = self.new_run().name
		for user in (self.mic, self.head_ops, self.reception, self.moh_api):
			with self.assertRaises(frappe.PermissionError, msg=user):
				self.act(name, "generate", user=user)
		self.assertEqual(self.status(name), "Draft")

	def test_blocked_report_cannot_be_generated(self):
		name = self.new_run().name
		frappe.db.set_value(MOH_REPORT_DOCTYPE, "T-MOH TEST", {"status": "Blocked", "blocked_reason": "T-"})
		with self.assertRaises(ReportBlockedError):
			self.act(name, "generate")

	def test_submit_and_immutable(self):
		name = self.new_run().name
		with self.assertRaises(IllegalTransitionError):
			self.act(name, "submit_run")  # from Draft
		self.act(name, "generate")
		for user in (self.mic, self.head_ops):
			with self.assertRaises(frappe.PermissionError):
				self.act(name, "submit_run", user=user)
		self.act(name, "submit_run")
		doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
		self.assertEqual((doc.status, doc.submitted_by), ("Submitted", self.his))
		for action, reason in (("generate", None), ("submit_run", None), ("void", "T- reason")):
			with self.assertRaises(IllegalTransitionError, msg=action):
				self.act(name, action, reason=reason)
		with as_user(self.his):
			doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
			doc.narrative = "T- changed after submission"
			self.assertRaises(LockedFieldError, doc.save)
			doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
			doc.status = "Generated"
			self.assertRaises(LockedFieldError, doc.save)

	def test_protected_fields_and_log_while_generated(self):
		doc = self.generated_run()
		with as_user(self.his):
			doc = frappe.get_doc(MOH_RUN_DOCTYPE, doc.name)
			doc.snapshot = json.dumps({"cells": []})
			self.assertRaises(LockedFieldError, doc.save)
			doc = frappe.get_doc(MOH_RUN_DOCTYPE, doc.name)
			doc.log[0].comment = "T- rewritten"
			self.assertRaises(LockedFieldError, doc.save)
			doc = frappe.get_doc(MOH_RUN_DOCTYPE, doc.name)
			doc.from_date = add_days(doc.from_date, -1)
			self.assertRaises(frappe.ValidationError, doc.save)

	def test_narrative_edit_rebuilds_exports(self):
		doc = self.generated_run()
		old_pdf = doc.pdf_file
		with as_user(self.his):
			doc = frappe.get_doc(MOH_RUN_DOCTYPE, doc.name)
			doc.narrative = "T- Two health talks this month."
			doc.save()
		doc.reload()
		self.assertNotEqual(doc.pdf_file, old_pdf)
		sheet = load_sheet(doc.xlsx_file)
		self.assertIn("T- Two health talks this month.", [c.value for c in sheet["A"]])

	def test_revise_supersedes(self):
		name = self.new_run().name
		self.act(name, "generate")
		self.act(name, "submit_run")
		with self.assertRaises(IllegalTransitionError):
			self.act(name, "revise")  # reason required
		with self.assertRaises(frappe.PermissionError):
			self.act(name, "revise", user=self.mic, reason="T-")
		result = self.act(name, "revise", reason="T- late lab results")
		new = frappe.get_doc(MOH_RUN_DOCTYPE, result["new_run"])
		old = frappe.get_doc(MOH_RUN_DOCTYPE, name)
		self.assertEqual((old.status, old.superseded_by), ("Superseded", new.name))
		self.assertEqual((new.status, new.supersedes), ("Draft", name))
		self.assertEqual((new.report, new.facility, new.from_date), (old.report, old.facility, old.from_date))
		self.assertIn("T- late lab results", new.log[0].comment)
		with self.assertRaises(IllegalTransitionError):
			self.act(name, "revise", reason="T- again")  # only the latest run is revised
		with self.assertRaises(DuplicateRunError):
			self.new_run()  # the revision is the active run
		self.act(new.name, "generate")
		self.act(new.name, "submit_run")
		self.assertEqual(self.status(new.name), "Submitted")

	def test_void(self):
		draft = self.new_run().name
		with self.assertRaises(IllegalTransitionError):
			self.act(draft, "void")
		with self.assertRaises(IllegalTransitionError):
			self.act(draft, "void", reason="   ")
		with self.assertRaises(frappe.PermissionError):
			self.act(draft, "void", user=self.mic, reason="T-")
		self.act(draft, "void", reason="T- wrong facility")
		doc = frappe.get_doc(MOH_RUN_DOCTYPE, draft)
		self.assertEqual(
			(doc.status, doc.void_reason, doc.voided_by), ("Void", "T- wrong facility", self.his)
		)
		for action in ("generate", "submit_run", "revise", "void"):
			with self.assertRaises(IllegalTransitionError, msg=action):
				self.act(draft, action, reason="T-")
		generated = self.generated_run().name
		self.act(generated, "void", reason="T- regenerate later")
		self.assertEqual(self.status(generated), "Void")

	def test_available_actions(self):
		doc = self.new_run()
		labels = lambda user: [a["action"] for a in run_module.get_available_actions(doc, user)]  # noqa: E731
		self.assertEqual(labels(self.his), ["generate", "void"])
		self.assertEqual(labels(self.mic), [])
		self.assertEqual(labels(self.head_ops), [])

	def test_long_range_is_enqueued(self):
		end = getdate(today()) - timedelta(days=1)
		start = end - timedelta(days=120)
		name = self.new_run(from_date=start, to_date=end, period_type="Custom").name
		with patch("frappe.enqueue") as enqueue:
			result = self.act(name, "generate")
		self.assertTrue(result["queued"])
		enqueue.assert_called_once()
		args, kwargs = enqueue.call_args
		self.assertEqual(args[0], "onerc_healthcare.reporting.run.run_generation_job")
		self.assertEqual((kwargs["queue"], kwargs["run"]), ("long", name))
		doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
		self.assertEqual((doc.status, doc.generation_status), ("Draft", "Queued"))
		self.assertEqual([a["action"] for a in run_module.get_available_actions(doc, self.his)], ["void"])
		with self.assertRaises(IllegalTransitionError):
			self.act(name, "generate")  # already being generated
		with as_user(self.his):
			run_module.run_generation_job(name)  # what the worker runs
		doc.reload()
		self.assertEqual((doc.status, doc.generation_status), ("Generated", None))
		self.assertTrue(doc.pdf_file)

	def test_queued_job_failure_and_void(self):
		end = getdate(today()) - timedelta(days=1)
		name = self.new_run(from_date=end - timedelta(days=100), to_date=end, period_type="Custom").name
		with patch("frappe.enqueue"):
			self.act(name, "generate")
		with as_user(self.his), patch.object(engine, "evaluate_report", side_effect=RuntimeError("T- boom")):
			run_module.run_generation_job(name)
		doc = frappe.get_doc(MOH_RUN_DOCTYPE, name)
		self.assertEqual((doc.status, doc.generation_status), ("Draft", "Failed"))
		self.assertIn("T- boom", doc.generation_error)
		with patch("frappe.enqueue"):
			self.act(name, "generate")  # retry after a failure
		self.act(name, "void", reason="T- not needed")
		with as_user(self.his):
			run_module.run_generation_job(name)  # finds the run Void and stops
		self.assertEqual(self.status(name), "Void")

	def test_head_of_operations_reads_runs_without_exceptions(self):
		doc = self.generated_run()
		self.assertTrue(doc.exceptions)
		with as_user(self.head_ops):
			self.assertTrue(frappe.has_permission(MOH_RUN_DOCTYPE, "read", doc.name))
			seen = frappe.client.get(MOH_RUN_DOCTYPE, doc.name)
			self.assertFalse(seen.get("exceptions"))
			self.assertFalse(seen.get("exception_count"))  # removed (or zeroed) for permlevel 1
			self.assertEqual(seen["status"], "Generated")
		with as_user(self.mic):
			seen = frappe.client.get(MOH_RUN_DOCTYPE, doc.name)
			self.assertEqual(len(seen["exceptions"]), 3)
		with as_user(self.reception):
			self.assertFalse(frappe.has_permission(MOH_RUN_DOCTYPE, "read", doc.name))

	def test_permissions_have_no_delete(self):
		for doctype in (MOH_RUN_DOCTYPE, MOH_REPORT_DOCTYPE):
			for perm in frappe.get_meta(doctype).permissions:
				self.assertFalse(perm.delete, f"{doctype} {perm.role}")


def load_sheet(file_url: str):
	from openpyxl import load_workbook

	content = frappe.get_doc("File", {"file_url": file_url}).get_content()
	return load_workbook(io.BytesIO(content)).active


def find_row(sheet, code: str) -> list:
	for row in sheet.iter_rows(values_only=True):
		if row and row[0] == code:
			return list(row)
	raise AssertionError(f"row {code} not found")


class TestExports(ReportingTestCase):
	def test_xlsx(self):
		frappe.db.set_value(
			"Healthcare Service Unit",
			self.facility,
			{"krcs_facility_code": "T-FC1", "krcs_county": "T-Turkana", "krcs_sub_county": "T-Turkana West"},
			update_modified=False,
		)
		doc = self.generated_run()
		sheet = load_sheet(doc.xlsx_file)
		self.assertEqual(sheet["A1"].value, "PROVISIONAL — HIS Officer to confirm (OI-5)")
		labels = {row[0]: row[1] for row in sheet.iter_rows(values_only=True) if row and row[0]}
		self.assertEqual(labels["Facility code"], "T-FC1")
		self.assertEqual(labels["County"], "T-Turkana")
		self.assertEqual(labels["Sub-county"], "T-Turkana West")
		self.assertEqual(labels["Report version"], "t-1")
		self.assertEqual(labels["Compiled by"], "T-HIS Officer One")
		self.assertEqual(labels["Signature"], "______________________________")
		self.assertIn(period_label(self.from_date, self.to_date), labels["Period"])
		# T-001: <1, 1-4, 5+, Age unknown, Total
		self.assertEqual(find_row(sheet, "T-001")[1:7], ["Age only", 2, 2, 1, 1, 6])
		# T-002: <1 M F, 1-4 M F, 5+ M F, Total M F All
		self.assertEqual(find_row(sheet, "T-002")[2:11], [0, 2, 1, 0, 1, 1, 2, 3, 5])
		self.assertEqual(find_row(sheet, "T-003")[2:5], [1, 2, 3])  # Male, Female, Total
		self.assertEqual(find_row(sheet, "T-004")[2], 4)
		gap = find_row(sheet, "T-005")
		self.assertIsNone(gap[2])
		self.assertEqual(gap[3], "Gap: T- not captured yet")
		self.assertTrue(find_row(sheet, "T-006")[3].startswith("Not computed"))
		self.assertEqual(find_row(sheet, "T-007")[2], 0)

	def test_confirmed_report_has_no_banner(self):
		frappe.db.set_value(MOH_REPORT_DOCTYPE, "T-MOH TEST", "status", "Confirmed")
		doc = self.generated_run()
		self.assertEqual(doc.report_status, "Confirmed")
		sheet = load_sheet(doc.xlsx_file)
		self.assertEqual(sheet["A1"].value, "T-MOH TEST — T- Test Report")
		html = frappe.get_doc(MOH_RUN_DOCTYPE, doc.name).preview_html()
		self.assertNotIn("PROVISIONAL", html)

	def test_pdf(self):
		doc = self.generated_run()
		content = frappe.get_doc("File", {"file_url": doc.pdf_file}).get_content()
		self.assertTrue(content.startswith(b"%PDF"))
		file_row = frappe.db.get_value(
			"File",
			{"file_url": doc.pdf_file},
			["is_private", "attached_to_doctype", "attached_to_name"],
			as_dict=True,
		)
		self.assertEqual(
			(file_row.is_private, file_row.attached_to_doctype, file_row.attached_to_name),
			(1, MOH_RUN_DOCTYPE, doc.name),
		)
		html = doc.preview_html()
		self.assertIn("PROVISIONAL", html)
		self.assertIn("T- not captured yet", html)
		self.assertIn("Signature", html)


class TestAPI(ReportingTestCase):
	def setUp(self):
		super().setUp()
		self.run_name = self.generated_run().name

	def test_his_reads_values(self):
		with as_user(self.his):
			reports = {r["name"]: r for r in api.list_reports()}
			runs = api.list_runs(report="T-MOH TEST", facility=self.facility)
			values = api.get_report_values(self.run_name)
		self.assertEqual((reports["T-MOH TEST"]["elements"], reports["T-MOH TEST"]["gaps"]), (8, 1))
		self.assertEqual([r["name"] for r in runs], [self.run_name])
		self.assertTrue(values["provisional"])
		by_code = {e["code"]: e for e in values["elements"]}
		t1 = {(v["band"], v["sex"]): v["value"] for v in by_code["T-001"]["values"]}
		self.assertEqual(
			t1, {("<1", ""): 2, ("1-4", ""): 2, ("5+", ""): 1, (UNKNOWN_BAND, ""): 1, ("", ""): 6}
		)
		self.assertEqual(by_code["T-004"]["values"], [{"band": "", "sex": "", "value": 4}])
		self.assertEqual(by_code["T-007"]["values"], [{"band": "", "sex": "", "value": 0}])
		self.assertEqual(by_code["T-005"]["values"], [])
		self.assertIn("Gap", by_code["T-005"]["note"])
		self.assertEqual(by_code["T-006"]["values"], [])

	def test_values_come_from_the_snapshot(self):
		frappe.db.set_value(MOH_REPORT_DOCTYPE, "T-MOH TEST", "title", "T- changed later")
		with as_user(self.his):
			values = api.get_report_values(self.run_name)
		self.assertEqual(values["report"]["title"], "T- Test Report")

	def test_roles(self):
		for user in (self.moh_api, self.mic):
			with as_user(user):
				self.assertTrue(api.list_reports())
				self.assertEqual(len(api.list_runs(report="T-MOH TEST")), 1)
				self.assertEqual(api.get_report_values(self.run_name)["run"], self.run_name)
		for user in (self.head_ops, self.reception):
			with as_user(user):
				self.assertRaises(frappe.PermissionError, api.list_reports)
				self.assertRaises(frappe.PermissionError, api.list_runs)
				self.assertRaises(frappe.PermissionError, api.get_report_values, self.run_name)

	def test_only_generated_or_submitted_runs(self):
		draft = self.new_run(facility=get_facility(1)).name
		with as_user(self.his):
			self.assertRaises(frappe.DoesNotExistError, api.get_report_values, draft)
			self.assertNotIn(draft, [r["name"] for r in api.list_runs()])
		self.act(self.run_name, "void", reason="T-")
		with as_user(self.his):
			self.assertRaises(frappe.DoesNotExistError, api.get_report_values, self.run_name)

	def test_calls_are_logged(self):
		def count():
			return frappe.db.count(
				"Access Log Entry", {"user": self.moh_api, "reference_doctype": MOH_RUN_DOCTYPE}
			)

		before = count()
		with as_user(self.moh_api):
			api.list_reports()
			api.list_runs()
			api.get_report_values(self.run_name)
		self.assertEqual(count(), before + 3)
		entry = frappe.get_last_doc("Access Log Entry", filters={"user": self.moh_api})
		self.assertEqual((entry.action, entry.reference_name), ("Read (API)", self.run_name))

	def test_get_only_and_rate_limited(self):
		for fn in (api.list_reports, api.list_runs, api.get_report_values):
			self.assertIn(fn, frappe.whitelisted)
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["GET"])
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[run_module.apply_action], ["POST"])
