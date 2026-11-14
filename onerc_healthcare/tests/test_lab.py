# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""B2 Laboratory: lab orders to results (FR-15..FR-18, BR-5, SDD §5.4). Never commit.

Consultations go through B1's flow when it is importable (`clinical.consultation.start_consultation` /
`send_lab_orders`, which calls `lab.orders.ensure_observations`); otherwise the same steps run here (draft
encounter linked to the visit, Marley's `make_service_request`, `request_lab`, `ensure_observations`).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.permissions import add_permission, update_permission_property
from frappe.utils import add_days, today
from healthcare.healthcare.doctype.observation.observation import (
	approve_all_observations,
	record_observation_result,
	set_observation_status,
)
from healthcare.healthcare.doctype.sample_collection.sample_collection import create_observation as collect
from healthcare.healthcare.doctype.service_request.service_request import (
	make_observation as marley_make_observation,
)

from onerc_healthcare.constants import (
	DEPT_OUTPATIENT,
	REPORT_LAB_WORKLIST,
	ROLE_LAB,
	VISIT_DOCTYPE,
	VISIT_STATUS_AWAITING_LAB,
	VISIT_STATUS_DISCHARGED,
	VISIT_STATUS_IN_CONSULTATION,
)
from onerc_healthcare.emr_laboratory.report.laboratory_worklist import laboratory_worklist
from onerc_healthcare.lab import api, events, guards, setup, tasks
from onerc_healthcare.lab.guards import make_observation
from onerc_healthcare.lab.orders import ensure_observations
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import get_facility
from onerc_healthcare.tests.visit_factories import (
	make_submitted_encounter,
	make_visit,
	make_visit_users,
	make_vital_signs,
)
from onerc_healthcare.visit.engine import transition
from onerc_healthcare.visit.tasks import raise_event

MRDT = "Malaria Rapid Diagnostic Test"
HB = "Haemoglobin"
CBC = "Full Haemogram (CBC)"
BEDSIDE = "T-Lab Bedside Test"  # created in the test: no sample required

# Permission changes this workstream needs on Marley DocTypes (krcs_security_fixups_v3, applied by the
# integrator). The tests grant them inside the savepoint, as the fix-up will.
LAB_FIXUPS = {"Specimen": ("read", "write", "create"), "Observation": ("cancel",)}


def b1_endpoint(name: str):
	"""B1's consultation endpoint (`clinical.consultation.<name>`), or None while it is not in place."""
	try:
		return frappe.get_attr(f"onerc_healthcare.clinical.consultation.{name}")
	except (ImportError, AttributeError):
		return None


# Visit-lock hooks reported for hooks.py (Phase 2 review M6); the tests add them while unwired.
LOCK_HOOKS = {
	"before_submit": "onerc_healthcare.lab.events.observation_before_submit",
	"before_cancel": "onerc_healthcare.lab.events.observation_before_cancel",
}


@contextmanager
def observation_lock_hooks():
	"""Make sure the Observation before_submit / before_cancel visit-lock hooks are active."""
	events = frappe.get_doc_hooks().setdefault("Observation", {})
	added = []
	for event, path in LOCK_HOOKS.items():
		hooks = events.setdefault(event, [])
		if path not in hooks:
			hooks.append(path)
			added.append((event, path))
	try:
		yield
	finally:
		for event, path in added:
			events[event].remove(path)


@contextmanager
def capture_sql():
	"""Record every SQL statement sent through frappe.db.sql."""
	queries: list[str] = []
	original = frappe.db.sql

	def spy(query, *args, **kwargs):
		queries.append(str(query))
		return original(query, *args, **kwargs)

	with patch.object(frappe.db, "sql", side_effect=spy):
		yield queries


def first_index(queries: list[str], *needles: str) -> int:
	for i, query in enumerate(queries):
		low = " ".join(query.lower().split())
		if all(n.lower() in low for n in needles):
			return i
	return -1


def grant_lab_fixups() -> None:
	for doctype, ptypes in LAB_FIXUPS.items():
		if not frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": ROLE_LAB, "permlevel": 0}):
			add_permission(doctype, ROLE_LAB, 0)
		for ptype in ptypes:
			update_permission_property(doctype, ROLE_LAB, 0, ptype, 1, validate=False)
		frappe.clear_cache(doctype=doctype)


def make_bedside_template() -> str:
	if not frappe.db.exists("Observation Template", BEDSIDE):
		frappe.get_doc(
			{
				"doctype": "Observation Template",
				"observation": BEDSIDE,
				"observation_category": "Laboratory",
				"permitted_data_type": "Text",
				"sample_collection_required": 0,
				"is_billable": 0,
			}
		).insert(ignore_permissions=True)
	return BEDSIDE


class LabTestCase(EMRTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users = make_visit_users()

	def setUp(self):
		super().setUp()
		grant_lab_fixups()
		self.practitioner = self.clinician_practitioner()

	def tearDown(self):
		super().tearDown()
		for doctype in LAB_FIXUPS:
			frappe.clear_cache(doctype=doctype)

	@contextmanager
	def as_user(self, key: str):
		frappe.set_user(self.users[key])
		try:
			yield
		finally:
			frappe.set_user("Administrator")

	# -- builders ------------------------------------------------------------------------------------

	def clinician_practitioner(self) -> str:
		"""A practitioner whose user is the test clinician (user_id written directly: no User Permission)."""
		user = self.users["clinician"]
		name = frappe.db.get_value("Healthcare Practitioner", {"user_id": user}, "name")
		if name:
			return name
		doc = frappe.get_doc(
			{"doctype": "Healthcare Practitioner", "first_name": "T-LabClin", "status": "Active"}
		)
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		frappe.db.set_value("Healthcare Practitioner", doc.name, "user_id", user)
		return doc.name

	def visit_in_consultation(self, facility: str | None = None):
		"""A visit In Consultation with its draft encounter (B1's `start_consultation` when available)."""
		visit = make_visit(facility=facility)
		transition(visit.name, "start_triage", system=True)
		make_vital_signs(visit.patient, visit=visit.name, submit=True)
		transition(visit.name, "route", system=True, department=DEPT_OUTPATIENT)
		start = b1_endpoint("start_consultation")
		with self.as_user("clinician"):
			if start:
				start(visit.name)
			else:
				transition(visit.name, "start_consultation")
		visit = frappe.get_doc(VISIT_DOCTYPE, visit.name)
		self.assertEqual(visit.status, VISIT_STATUS_IN_CONSULTATION)
		return visit

	def open_encounter(self, visit):
		name = frappe.db.get_value(VISIT_DOCTYPE, visit.name, "encounter")
		if name:
			return frappe.get_doc("Patient Encounter", name)
		enc = make_submitted_encounter(visit.patient, visit=visit.name, submit=False)
		enc.practitioner = self.practitioner
		enc.flags.ignore_mandatory = True
		enc.save(ignore_permissions=True)
		return enc

	def add_lab_rows(self, enc, templates: list[str]) -> set:
		enc.reload()
		before = {row.service_request for row in enc.lab_test_prescription if row.service_request}
		for template in templates:
			enc.append("lab_test_prescription", {"observation_template": template})
		enc.flags.ignore_mandatory = True
		enc.save(ignore_permissions=True)
		return before

	def new_orders(self, enc, before: set) -> list[str]:
		enc.reload()
		return [
			r.service_request
			for r in enc.lab_test_prescription
			if r.service_request and r.service_request not in before
		]

	def make_encounter(self, visit, templates: list[str]):
		"""Orders created by Marley's `make_service_request` only (nothing provisioned for the lab)."""
		enc = self.open_encounter(visit)
		before = self.add_lab_rows(enc, templates)
		enc.make_service_request()
		enc.flags.ignore_mandatory = True
		enc.save(ignore_permissions=True)
		return enc, self.new_orders(enc, before)

	def send_lab_orders(self, visit, templates: list[str]):
		enc = self.open_encounter(visit)
		return enc, self.add_orders(enc, templates)

	def add_orders(self, enc, templates: list[str]) -> list[str]:
		"""Lab rows sent by the clinician: B1's `send_lab_orders` when available, else its steps here
		(Marley's orders, `request_lab`, `ensure_observations`)."""
		before = self.add_lab_rows(enc, templates)
		send = b1_endpoint("send_lab_orders")
		if send:
			with self.as_user("clinician"):
				send(enc.name)
			return self.new_orders(enc, before)
		enc.make_service_request()
		enc.flags.ignore_mandatory = True
		enc.save(ignore_permissions=True)
		new = self.new_orders(enc, before)
		raise_event(frappe.db.get_value(VISIT_DOCTYPE, {"encounter": enc.name}, "name"), "request_lab")
		for sr in new:
			ensure_observations(sr)
		return new

	# -- lab actions ---------------------------------------------------------------------------------

	def sample_rows(self, encounter: str) -> list:
		collections = frappe.get_all(
			"Sample Collection", filters={"reference_name": encounter, "docstatus": ["!=", 2]}, pluck="name"
		)
		return frappe.get_all(
			"Observation Sample Collection",
			filters={"parent": ["in", collections or [""]]},
			fields=["*"],
			order_by="creation asc",
		)

	def collect_all(self, encounter: str, user: str = "lab") -> None:
		for row in self.sample_rows(encounter):
			if row.status != "Open":
				continue
			with self.as_user(user):
				collect(selected=json.dumps([row], default=str), sample_collection=row.parent)

	def root(self, sr: str, docstatus: int | None = None) -> str:
		filters = {"service_request": sr, "parent_observation": ["is", "not set"], "docstatus": ["!=", 2]}
		if docstatus is not None:
			filters["docstatus"] = docstatus
		return frappe.db.get_value("Observation", filters, "name", order_by="creation desc")

	def leaves(self, root: str) -> list[str]:
		return frappe.get_all(
			"Observation",
			filters={"parent_observation": root, "docstatus": ["!=", 2], "has_component": 0},
			pluck="name",
		)

	def enter(self, observations: list[str], result: str, user: str = "lab") -> None:
		with self.as_user(user):
			record_observation_result(
				json.dumps([{"observation": o, "result": result} for o in observations])
			)

	def approve(self, observation: str, user: str = "lab") -> None:
		with self.as_user(user):
			set_observation_status(observation, "Approved")

	def reject(self, observation: str, user: str = "lab") -> None:
		with self.as_user(user):
			set_observation_status(observation, "Rejected", reason="T- haemolysed sample")

	def result_single(self, sr: str, result: str = "Negative") -> str:
		obs = self.root(sr)
		self.enter([obs], result)
		self.approve(obs)
		return obs

	def visit(self, name: str):
		return frappe.get_doc(VISIT_DOCTYPE, name)

	def notifications(self, key: str, text: str) -> list:
		return frappe.get_all(
			"Notification Log",
			filters={"for_user": self.users[key], "subject": ["like", f"%{text}%"]},
			fields=["subject", "document_type", "document_name", "type"],
		)


class TestEnsureObservations(LabTestCase):
	def test_two_orders_both_provisioned(self):
		"""W0 defect fixed: every lab order of an encounter gets its sample row; one report (FR-15/16)."""
		visit = self.visit_in_consultation()
		enc, srs = self.send_lab_orders(visit, [MRDT, HB, CBC])
		rows = self.sample_rows(enc.name)
		self.assertEqual(sorted(r.service_request for r in rows), sorted(srs))
		self.assertEqual(len({r.parent for r in rows}), 1, "one draft Sample Collection per encounter")
		cbc_row = next(r for r in rows if r.observation_template == CBC)
		self.assertTrue(cbc_row.component_observation_parent)
		self.assertTrue(cbc_row.has_component)
		self.assertEqual(self.root(srs[2]), cbc_row.component_observation_parent)
		reports = frappe.get_all(
			"Diagnostic Report", filters={"docname": enc.name}, fields=["sample_collection"]
		)
		self.assertEqual(len(reports), 1)
		self.assertEqual(reports[0].sample_collection, rows[0].parent)

	def test_idempotent(self):
		visit = self.visit_in_consultation()
		enc, srs = self.send_lab_orders(visit, [MRDT, CBC])
		counts = (len(self.sample_rows(enc.name)), frappe.db.count("Observation", {"patient": visit.patient}))
		for sr in srs:
			ensure_observations(sr)
		self.assertEqual(
			(len(self.sample_rows(enc.name)), frappe.db.count("Observation", {"patient": visit.patient})),
			counts,
		)
		self.assertEqual(frappe.db.count("Diagnostic Report", {"docname": enc.name}), 1)

	def test_marley_still_stops_after_first_order(self):
		"""Regression marker for the W0 spike: when Marley fixes it, this fails and the workaround can go."""
		visit = self.visit_in_consultation()
		enc, srs = self.make_encounter(visit, [MRDT, HB])
		marley_make_observation(srs[0])
		self.assertEqual(marley_make_observation(srs[1])[1], "Diagnostic Report")
		self.assertEqual([r.service_request for r in self.sample_rows(enc.name)], [srs[0]])

	def test_later_order_reuses_open_collection_then_starts_new_one(self):
		visit = self.visit_in_consultation()
		enc, _srs = self.send_lab_orders(visit, [MRDT])
		first = self.sample_rows(enc.name)[0].parent
		(sr2,) = self.add_orders(enc, [HB])
		self.assertEqual({r.parent for r in self.sample_rows(enc.name)}, {first})
		self.collect_all(enc.name)
		frappe.get_doc("Sample Collection", first).submit()
		(sr3,) = self.add_orders(enc, [CBC])
		row3 = next(r for r in self.sample_rows(enc.name) if r.service_request == sr3)
		self.assertNotEqual(row3.parent, first)
		self.assertEqual(frappe.db.count("Diagnostic Report", {"docname": enc.name}), 1)
		self.assertTrue(sr2)

	def test_non_lab_and_unsubmitted_orders_ignored(self):
		visit = self.visit_in_consultation()
		enc, srs = self.make_encounter(visit, [MRDT])
		frappe.db.set_value("Service Request", srs[0], "status", "revoked-Request Status")
		ensure_observations(srs[0])
		self.assertEqual(self.sample_rows(enc.name), [])

	def test_same_structure_as_marley_first_order(self):
		"""ensure_observations builds what make_observation builds for an encounter's first order."""
		make_bedside_template()
		for template in (MRDT, CBC, BEDSIDE):
			with self.subTest(template=template):
				marley_enc, marley_srs = self.make_encounter(self.visit_in_consultation(), [template])
				marley_make_observation(marley_srs[0])
				ours_enc, ours_srs = self.make_encounter(self.visit_in_consultation(), [template])
				ensure_observations(ours_srs[0])
				self.assertEqual(self.structure(ours_enc), self.structure(marley_enc))

	def structure(self, enc) -> dict:
		def component_templates(value):
			return sorted(c["observation_template"] for c in json.loads(value)) if value else []

		rows = [
			(
				r.observation_template,
				r.sample,
				r.sample_type,
				r.status,
				r.has_component,
				bool(r.service_request),
				bool(r.component_observation_parent),
				component_templates(r.component_observations),
			)
			for r in self.sample_rows(enc.name)
		]
		observations = frappe.get_all(
			"Observation",
			filters={"patient": enc.patient},
			fields=["observation_template", "parent_observation", "reference_doctype", "service_request"],
		)
		templates = {
			o.name: o.observation_template
			for o in frappe.get_all(
				"Observation", filters={"patient": enc.patient}, fields=["name", "observation_template"]
			)
		}
		obs = sorted(
			(
				o.observation_template,
				templates.get(o.parent_observation),
				o.reference_doctype,
				bool(o.service_request),
			)
			for o in observations
		)
		reports = frappe.get_all(
			"Diagnostic Report", filters={"docname": enc.name}, fields=["ref_doctype", "sample_collection"]
		)
		return {
			"rows": sorted(rows),
			"observations": obs,
			"reports": [(r.ref_doctype, bool(r.sample_collection)) for r in reports],
		}


class TestMakeObservationOverride(LabTestCase):
	def test_roles(self):
		visit = self.visit_in_consultation()
		_enc, srs = self.make_encounter(visit, [MRDT, HB])
		for key in ("pharmacist", "reception", "triage", "his", "sysadmin"):
			with self.subTest(user=key), self.as_user(key), self.assertRaises(frappe.PermissionError):
				make_observation(srs[0])
		with self.as_user("lab"):
			created = make_observation(srs[0])
			again = make_observation(srs[0])
		self.assertEqual(created[1], "Sample Collection")
		self.assertEqual(tuple(again), (*created, "New"))
		with self.as_user("clinician"):
			second = make_observation(srs[1])
		self.assertEqual(second, created, "the second order joins the same Sample Collection")

	def test_requires_lab_order(self):
		visit = self.visit_in_consultation()
		_enc, srs = self.make_encounter(visit, [MRDT])
		frappe.db.set_value("Service Request", srs[0], "template_dt", "Clinical Procedure Template")
		with self.as_user("lab"), self.assertRaises(frappe.ValidationError):
			make_observation(srs[0])


class TestLabFlow(LabTestCase):
	def test_collect_result_approve_as_lab_technician(self):
		"""FR-17/BR-5: the Lab Technician, with only the role's permissions, collects, enters and approves.
		The visit stays Awaiting Laboratory until the last result, then lab_results_ready fires."""
		visit = self.visit_in_consultation()
		enc, (mrdt, cbc) = self.send_lab_orders(visit, [MRDT, CBC])
		self.assertEqual(self.visit(visit.name).status, VISIT_STATUS_AWAITING_LAB)
		self.collect_all(enc.name)
		self.assertTrue(frappe.db.exists("Specimen", {"patient": visit.patient}))
		self.assertEqual({r.status for r in self.sample_rows(enc.name)}, {"Collected"})

		self.result_single(mrdt)
		self.assertEqual(self.visit(visit.name).status, VISIT_STATUS_AWAITING_LAB)
		self.assertFalse(tasks.is_lab_complete(visit.name))

		root = self.root(cbc)
		self.enter(self.leaves(root), "5.1")
		report = frappe.db.get_value("Diagnostic Report", {"docname": enc.name}, "name")
		with self.as_user("lab"):
			approve_all_observations(report)
		doc = self.visit(visit.name)
		self.assertEqual(doc.status, VISIT_STATUS_IN_CONSULTATION)
		self.assertEqual(doc.lab_open, 0)
		self.assertEqual(doc.status_log[-1].action, "lab_results_ready")
		self.assertEqual(sum(1 for r in doc.status_log if r.action == "lab_results_ready"), 1)
		self.assertTrue(tasks.is_lab_complete(visit.name))

	def test_clinician_notified_once_per_test(self):
		"""FR-17: 'Lab result ready: <test> for <patient>', linked to the encounter, once per test."""
		visit = self.visit_in_consultation()
		enc, (mrdt, cbc) = self.send_lab_orders(visit, [MRDT, CBC])
		self.collect_all(enc.name)
		self.result_single(mrdt)
		ready = self.notifications("clinician", "Lab result ready")
		self.assertEqual(len(ready), 1)
		self.assertEqual(ready[0].subject, f"Lab result ready: {MRDT} for {visit.patient_name}")
		self.assertEqual((ready[0].document_type, ready[0].document_name), ("Patient Encounter", enc.name))
		self.assertEqual(ready[0].type, "Alert")

		root = self.root(cbc)
		self.enter(self.leaves(root), "5.1")
		self.approve(root)  # panel approved first, then its components (Marley's order)
		self.assertEqual(len(self.notifications("clinician", f"Lab result ready: {CBC}")), 1)
		self.assertFalse(self.notifications("pharmacist", "Lab result"))

	def test_panel_approved_component_by_component(self):
		"""Approving each component (the panel itself stays draft) completes the test."""
		visit = self.visit_in_consultation()
		enc, (cbc,) = self.send_lab_orders(visit, [CBC])
		self.collect_all(enc.name)
		root = self.root(cbc)
		leaves = self.leaves(root)
		self.enter(leaves, "4.2")
		for leaf in leaves[:-1]:
			self.approve(leaf)
		self.assertEqual(self.visit(visit.name).status, VISIT_STATUS_AWAITING_LAB)
		self.approve(leaves[-1])
		self.assertEqual(frappe.db.get_value("Observation", root, "docstatus"), 0)
		self.assertEqual(self.visit(visit.name).status, VISIT_STATUS_IN_CONSULTATION)
		self.assertEqual(len(self.notifications("clinician", f"Lab result ready: {CBC}")), 1)

	def test_reject_and_redo(self):
		"""A rejected result puts the visit back to Awaiting Laboratory; the redo brings it back."""
		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		self.collect_all(enc.name)
		first = self.result_single(mrdt)
		self.assertEqual(self.visit(visit.name).status, VISIT_STATUS_IN_CONSULTATION)

		self.reject(first)
		doc = self.visit(visit.name)
		self.assertEqual(doc.status, VISIT_STATUS_AWAITING_LAB)
		self.assertEqual(doc.lab_open, 1)
		self.assertEqual(doc.status_log[-1].action, "request_lab")
		self.assertIn("rejected; repeat pending", doc.status_log[-1].comment)
		self.assertEqual(len(self.notifications("clinician", "Lab result withdrawn")), 1)
		self.assertFalse(self.notifications("mic", "Lab result withdrawn"))

		redo = self.root(mrdt)
		self.assertNotEqual(redo, first)
		self.assertEqual(frappe.db.get_value("Observation", redo, "docstatus"), 0)
		rows = laboratory_worklist.execute({})[1]
		self.assertEqual(
			next(r for r in rows if r["service_request"] == mrdt)["result_status"], "Repeat pending"
		)

		self.enter([redo], "Positive - P. falciparum")
		self.approve(redo)
		self.assertEqual(self.visit(visit.name).status, VISIT_STATUS_IN_CONSULTATION)
		self.assertEqual(len(self.notifications("clinician", "Lab result ready")), 2)

	def test_reject_component_of_draft_panel(self):
		"""Panel approved component by component; rejecting one component reopens the lab task."""
		visit = self.visit_in_consultation()
		enc, (cbc,) = self.send_lab_orders(visit, [CBC])
		self.collect_all(enc.name)
		root = self.root(cbc)
		leaves = self.leaves(root)
		self.enter(leaves, "4.2")
		for leaf in leaves:
			self.approve(leaf)
		self.assertEqual(self.visit(visit.name).status, VISIT_STATUS_IN_CONSULTATION)
		self.reject(leaves[0])
		self.assertEqual(self.visit(visit.name).status, VISIT_STATUS_AWAITING_LAB)

	def test_result_after_visit_closed(self):
		"""A result after Close Visit is kept; the visit does not move and gets a no-op Comment."""
		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		self.collect_all(enc.name)
		with self.as_user("clinician"):
			transition(visit.name, "close_visit", comment="T- patient left", closure_reason="Other")
		self.assertEqual(self.visit(visit.name).status, VISIT_STATUS_DISCHARGED)

		obs = self.result_single(mrdt)
		self.assertEqual(frappe.db.get_value("Observation", obs, "docstatus"), 1)
		doc = self.visit(visit.name)
		self.assertEqual((doc.status, doc.lab_open), (VISIT_STATUS_DISCHARGED, 0))
		self.assertNotIn("lab_results_ready", [r.action for r in doc.status_log])
		self.assertTrue(
			frappe.db.exists(
				"Comment",
				{
					"reference_doctype": VISIT_DOCTYPE,
					"reference_name": visit.name,
					"content": ["like", "%lab_results_ready%"],
				},
			)
		)
		self.assertEqual(len(self.notifications("clinician", "Lab result ready")), 1)

		self.reject(obs)
		doc = self.visit(visit.name)
		self.assertEqual((doc.status, doc.lab_open), (VISIT_STATUS_DISCHARGED, 0))
		self.assertEqual(len(self.notifications("mic", "Lab result withdrawn")), 1)
		self.assertEqual(len(self.notifications("clinician", "Lab result withdrawn")), 1)

	def test_service_request_cancel_completes_lab_task(self):
		visit = self.visit_in_consultation()
		enc, (mrdt, hb) = self.send_lab_orders(visit, [MRDT, HB])
		self.collect_all(enc.name)
		self.result_single(mrdt)
		self.assertEqual(self.visit(visit.name).status, VISIT_STATUS_AWAITING_LAB)
		frappe.get_doc("Service Request", hb).cancel()
		doc = self.visit(visit.name)
		self.assertEqual(doc.status, VISIT_STATUS_IN_CONSULTATION)
		self.assertEqual(doc.status_log[-1].action, "lab_results_ready")
		self.assertIn(hb, doc.status_log[-1].comment)

	def test_sample_collection_cancel_keeps_report(self):
		"""Marley's on_cancel would delete the Diagnostic Report; the guard refuses deletes (NFR-9). The
		results of the cancelled collection can still be entered and approved."""
		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		self.collect_all(enc.name)
		collection = frappe.get_doc("Sample Collection", self.sample_rows(enc.name)[0].parent)
		collection.submit()
		report = frappe.db.get_value("Diagnostic Report", {"docname": enc.name}, "name")
		self.assertEqual(
			frappe.db.get_value("Diagnostic Report", report, "sample_collection"), collection.name
		)
		collection.cancel()
		self.assertEqual(frappe.db.get_value("Sample Collection", collection.name, "docstatus"), 2)
		self.assertTrue(frappe.db.exists("Diagnostic Report", report))
		self.assertFalse(frappe.db.get_value("Diagnostic Report", report, "sample_collection"))
		obs = self.root(mrdt)
		self.assertEqual(
			frappe.db.get_value("Observation", obs, ["reference_doctype", "reference_docname"]),
			("Patient Encounter", enc.name),
		)
		self.result_single(mrdt)
		self.assertEqual(self.visit(visit.name).status, VISIT_STATUS_IN_CONSULTATION)
		self.assertEqual(frappe.db.get_value("Diagnostic Report", report, "status"), "Approved")

	def test_is_lab_complete_without_encounter(self):
		self.assertFalse(tasks.is_lab_complete(make_visit().name))


class TestEncounterResults(LabTestCase):
	def test_permissions(self):
		"""FR-18: the clinician reads results in the encounter; Pharmacist and Lab Technician cannot."""
		visit = self.visit_in_consultation()
		enc, _srs = self.send_lab_orders(visit, [MRDT])
		with self.as_user("clinician"):
			self.assertEqual(len(api.get_encounter_results(enc.name)), 1)
		with self.as_user("mic"):
			self.assertEqual(len(api.get_encounter_results(enc.name)), 1)
		for key in ("pharmacist", "lab", "sysadmin"):
			with self.subTest(user=key), self.as_user(key), self.assertRaises(frappe.PermissionError):
				api.get_encounter_results(enc.name)

	def test_content(self):
		visit = self.visit_in_consultation()
		enc, (mrdt, hb, _cbc) = self.send_lab_orders(visit, [MRDT, HB, CBC])
		self.collect_all(enc.name)
		self.result_single(mrdt)
		self.enter([self.root(hb)], "13.5")
		with self.as_user("clinician"):
			results = {r["test"]: r for r in api.get_encounter_results(enc.name)}
		done = results[MRDT]
		self.assertTrue(done["approved"])
		self.assertEqual(done["status"], tasks.RESULT_APPROVED)
		self.assertTrue(done["approved_at"])
		self.assertEqual(done["results"][0]["result"], "Negative")
		self.assertEqual(done["sample_status"], "Collected")

		pending = results[HB]
		self.assertFalse(pending["approved"])
		self.assertEqual(pending["status"], tasks.RESULT_AWAITING_APPROVAL)
		self.assertIsNone(pending["results"][0]["result"], "unapproved values are not released")
		self.assertEqual(pending["results"][0]["unit"], "g/dL")

		panel = results[CBC]
		self.assertEqual(panel["status"], tasks.RESULT_AWAITING_RESULT)
		self.assertEqual(len(panel["results"]), 10)
		self.assertEqual({r["panel"] for r in panel["results"]}, {CBC})


class TestLaboratoryWorklist(LabTestCase):
	def rows_for(self, srs, **filters) -> dict:
		rows = laboratory_worklist.execute(filters)[1]
		return {r["service_request"]: r for r in rows if r["service_request"] in srs}

	def test_rows_and_statuses(self):
		"""FR-16: pending requests with sample and result status from the lab records."""
		visit = self.visit_in_consultation()
		enc, srs = self.send_lab_orders(visit, [MRDT, CBC])
		rows = self.rows_for(srs)
		self.assertEqual(set(rows), set(srs))
		mrdt = rows[srs[0]]
		self.assertEqual(mrdt["visit"], visit.name)
		self.assertEqual(mrdt["queue_number"], visit.queue_number)
		self.assertEqual(mrdt["sample_status"], tasks.SAMPLE_AWAITING)
		self.assertEqual(mrdt["result_status"], tasks.RESULT_AWAITING_SAMPLE)
		self.assertTrue(mrdt["sample_collection"])
		self.assertEqual(rows[srs[1]]["observation"], self.root(srs[1]))

		self.collect_all(enc.name)
		rows = self.rows_for(srs)
		self.assertEqual(rows[srs[0]]["sample_status"], tasks.SAMPLE_COLLECTED_LABEL)
		self.assertEqual(rows[srs[0]]["result_status"], tasks.RESULT_AWAITING_RESULT)
		self.enter(self.leaves(self.root(srs[1]))[:3], "4.0")
		self.assertEqual(self.rows_for(srs)[srs[1]]["result_status"], f"{tasks.RESULT_PARTIAL} (3/10)")

		self.result_single(srs[0])
		self.assertEqual(set(self.rows_for(srs)), {srs[1]}, "an approved request leaves the worklist")

	def test_status_never_from_service_request(self):
		visit = self.visit_in_consultation()
		_enc, srs = self.send_lab_orders(visit, [MRDT])
		frappe.db.set_value("Service Request", srs[0], "status", "completed-Request Status")
		self.assertIn(srs[0], self.rows_for(srs))

	def test_filters(self):
		visit = self.visit_in_consultation(facility=get_facility(0))
		_enc, srs = self.send_lab_orders(visit, [MRDT])
		self.assertIn(srs[0], self.rows_for(srs, facility=get_facility(0)))
		other = next((f for f in _facilities() if f != get_facility(0)), "T-No Such Facility")
		self.assertFalse(self.rows_for(srs, facility=other))
		self.assertFalse(self.rows_for(srs, from_date=add_days(today(), 1), to_date=add_days(today(), 5)))
		self.assertFalse(self.rows_for(srs, from_date=add_days(today(), -5), to_date=add_days(today(), -1)))
		self.assertIn(srs[0], self.rows_for(srs, from_date=today(), to_date=today()))

	def test_run_is_access_logged(self):
		"""Phase 2 review M4 (FR-40, NFR-7): a worklist run writes an Access Log Entry."""
		with self.as_user("lab"):
			laboratory_worklist.execute({"facility": get_facility(0)})
		entry = frappe.get_all(
			"Access Log Entry",
			filters={
				"user": self.users["lab"],
				"reference_doctype": "Report",
				"reference_name": REPORT_LAB_WORKLIST,
			},
			fields=["action", "details"],
		)
		self.assertEqual(len(entry), 1)
		self.assertEqual(entry[0].action, "Report")
		self.assertIn("facility", entry[0].details)

	def test_report_roles(self):
		report = frappe.get_doc("Report", REPORT_LAB_WORKLIST)
		self.assertEqual(report.module, "EMR Laboratory")
		self.assertEqual(report.is_standard, "Yes")
		self.assertEqual({r.role for r in report.roles}, {ROLE_LAB, "Medical In-Charge"})
		for key, allowed in (("lab", True), ("mic", True), ("pharmacist", False), ("clinician", False)):
			with self.subTest(user=key), self.as_user(key):
				self.assertEqual(bool(report.is_permitted()), allowed)


def _facilities() -> list[str]:
	from onerc_healthcare.setup.operations import get_facilities

	return list(get_facilities())


class TestLockOrder(LabTestCase):
	"""Phase 2 review M6: lock order Visit -> Observation, so concurrent approvals cannot deadlock."""

	def test_visit_locked_before_observation_written_on_approve(self):
		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		self.collect_all(enc.name)
		obs = self.root(mrdt)
		self.enter([obs], "Negative")
		with observation_lock_hooks(), capture_sql() as queries, self.as_user("lab"):
			set_observation_status(obs, "Approved")
		visit_lock = first_index(queries, "from `tabPatient Visit`", "for update")
		write = first_index(queries, "update `tabObservation` set")
		self.assertGreaterEqual(visit_lock, 0)
		self.assertGreaterEqual(write, 0)
		self.assertLess(visit_lock, write)
		self.assertGreaterEqual(first_index(queries, "from `tabObservation`", "for update skip locked"), 0)
		self.assertEqual(self.visit(visit.name).status, VISIT_STATUS_IN_CONSULTATION)

	def test_visit_locked_before_observation_written_on_reject(self):
		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		self.collect_all(enc.name)
		obs = self.result_single(mrdt)
		with observation_lock_hooks(), capture_sql() as queries:
			self.reject(obs)
		visit_lock = first_index(queries, "from `tabPatient Visit`", "for update")
		write = first_index(queries, "update `tabObservation` set")
		self.assertGreaterEqual(visit_lock, 0)
		self.assertLess(visit_lock, write)
		self.assertEqual(self.visit(visit.name).status, VISIT_STATUS_AWAITING_LAB)

	def test_row_locked_elsewhere_counts_as_not_done(self):
		"""A root skipped by SKIP LOCKED (an approval in flight) is not done."""
		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		self.collect_all(enc.name)
		obs = self.result_single(mrdt)
		self.assertTrue(tasks.EncounterLab(enc.name, lock=True).is_done(mrdt))
		with patch.object(tasks.EncounterLab, "_locked_rows", return_value=[]):
			self.assertFalse(tasks.EncounterLab(enc.name, lock=True).is_done(mrdt))
		self.assertTrue(tasks.EncounterLab(enc.name).is_done(mrdt), "an unlocked read is unaffected")
		self.assertTrue(obs)

	def assert_visit_locked_first(self, queries: list[str], *writes: tuple[str, ...]) -> None:
		visit_lock = first_index(queries, "from `tabPatient Visit`", "for update")
		self.assertGreaterEqual(visit_lock, 0, "the visit is locked")
		for needles in writes:
			at = first_index(queries, *needles)
			self.assertGreaterEqual(at, 0, f"expected a query with {needles}")
			self.assertLess(visit_lock, at, f"visit lock must precede {needles}")

	def panel_ready(self):
		visit = self.visit_in_consultation()
		enc, (cbc,) = self.send_lab_orders(visit, [CBC])
		self.collect_all(enc.name)
		root = self.root(cbc)
		self.enter(self.leaves(root), "4.4")
		report = frappe.db.get_value("Diagnostic Report", {"docname": enc.name}, "name")
		return visit, root, report

	OBS_LOCKED = ("from `tabObservation`", "for update")
	OBS_WRITE = ("update `tabObservation` set",)

	def test_bulk_approve_locks_visit_first(self):
		visit, _root, report = self.panel_ready()
		with observation_lock_hooks(), capture_sql() as queries, self.as_user("lab"):
			guards.approve_all_observations(report)
		self.assert_visit_locked_first(queries, self.OBS_LOCKED, self.OBS_WRITE)
		self.assertEqual(self.visit(visit.name).status, VISIT_STATUS_IN_CONSULTATION)

	def test_panel_status_locks_visit_first(self):
		visit, root, _report = self.panel_ready()
		with observation_lock_hooks(), capture_sql() as queries, self.as_user("lab"):
			guards.set_observation_status(root, "Approved")
		self.assert_visit_locked_first(queries, self.OBS_LOCKED, self.OBS_WRITE)
		self.assertEqual(frappe.db.get_value("Observation", root, "docstatus"), 1)
		self.assertEqual(self.visit(visit.name).status, VISIT_STATUS_IN_CONSULTATION)

	def test_bulk_reject_locks_visit_first(self):
		visit, _root, report = self.panel_ready()
		with self.as_user("lab"):
			guards.approve_all_observations(report)
		with observation_lock_hooks(), capture_sql() as queries, self.as_user("lab"):
			result = guards.reject_all_observations(report, "T- clotted sample")
		self.assertTrue(result["rejected"])
		self.assert_visit_locked_first(queries, self.OBS_LOCKED, self.OBS_WRITE)
		self.assertEqual(self.visit(visit.name).status, VISIT_STATUS_AWAITING_LAB)

	def test_hook_lock_is_nowait_and_reports_busy(self):
		"""A submit outside the endpoints already holds its Observation row: it must not wait on the visit."""
		from onerc_healthcare.lab.events import VisitBusyError

		visit = self.visit_in_consultation()
		enc, (mrdt,) = self.send_lab_orders(visit, [MRDT])
		self.collect_all(enc.name)
		obs = self.root(mrdt)
		self.enter([obs], "Negative")
		with observation_lock_hooks(), capture_sql() as queries, self.as_user("lab"):
			frappe.get_doc("Observation", obs).submit()
		self.assertGreaterEqual(first_index(queries, "from `tabPatient Visit`", "for update nowait"), 0)

		real_get_value = frappe.db.get_value

		def busy(doctype, *args, **kwargs):
			if doctype == VISIT_DOCTYPE and kwargs.get("for_update") and kwargs.get("wait") is False:
				raise frappe.QueryTimeoutError("Lock wait timeout exceeded")
			return real_get_value(doctype, *args, **kwargs)

		with observation_lock_hooks(), patch.object(frappe.db, "get_value", side_effect=busy):
			with self.assertRaises(VisitBusyError):
				events.observation_before_cancel(frappe.get_doc("Observation", obs))

	def test_endpoint_permissions(self):
		_visit, root, report = self.panel_ready()
		for key in ("reception", "clinician", "pharmacist"):
			with self.subTest(user=key), self.as_user(key):
				with self.assertRaises(frappe.PermissionError):
					guards.set_observation_status(root, "Approved")
				with self.assertRaises(frappe.PermissionError):
					guards.approve_all_observations(report)
				with self.assertRaises(frappe.PermissionError):
					guards.reject_all_observations(report, "T- reason")
		with self.as_user("lab"), self.assertRaises(frappe.ValidationError):
			guards.set_observation_status(root, "Final")

	def test_sample_collection_cancel_locks_visit_first(self):
		visit = self.visit_in_consultation()
		enc, _srs = self.send_lab_orders(visit, [MRDT])
		self.collect_all(enc.name)
		collection = frappe.get_doc("Sample Collection", self.sample_rows(enc.name)[0].parent)
		collection.submit()
		with capture_sql() as queries:
			collection.cancel()
		self.assert_visit_locked_first(
			queries, ("update `tabDiagnostic Report` set",), self.OBS_LOCKED, self.OBS_WRITE
		)


class TestMarleyMappingGuards(LabTestCase):
	"""Phase 2 review M8: Marley's Service Request mapping endpoints leak order and patient details."""

	def test_receptionist_refused(self):
		visit = self.visit_in_consultation()
		_enc, (sr,) = self.make_encounter(visit, [MRDT])
		calls = {
			"make_lab_test": lambda: guards.make_lab_test(sr),
			"make_clinical_procedure": lambda: guards.make_clinical_procedure(sr),
			"make_appointment": lambda: guards.make_appointment(sr),
		}
		for name, call in calls.items():
			with (
				self.subTest(endpoint=name),
				self.as_user("reception"),
				self.assertRaises(frappe.PermissionError),
			):
				call()

	def test_legacy_routes_administrator_only(self):
		visit = self.visit_in_consultation()
		_enc, (sr,) = self.make_encounter(visit, [MRDT])
		for key in ("lab", "clinician"):
			with self.subTest(user=key), self.as_user(key):
				with self.assertRaises(frappe.PermissionError):
					guards.make_lab_test(sr)
				with self.assertRaises(frappe.PermissionError):
					guards.make_clinical_procedure(sr)
		self.assertEqual(guards.make_lab_test(sr).service_request, sr)

	def test_make_appointment_enforces_permissions(self):
		visit = self.visit_in_consultation()
		_enc, (sr,) = self.make_encounter(visit, [MRDT])
		with self.as_user("pharmacist"), self.assertRaises(frappe.PermissionError):
			guards.make_appointment(sr, ignore_permissions=True)
		self.assertEqual(guards.make_appointment(sr).service_request, sr)


class TestLabSetup(LabTestCase):
	def test_install_idempotent(self):
		"""LAB-07: no paper request print; C-5: the catalogue consumes no stock."""
		frappe.db.set_value("Print Format", "Lab Test Print", "disabled", 0, update_modified=False)
		frappe.db.set_value("Observation Template", MRDT, "consume_stock", 1, update_modified=False)
		setup.install()
		setup.install()
		self.assertEqual(frappe.db.get_value("Print Format", "Lab Test Print", "disabled"), 1)
		self.assertEqual(frappe.db.get_value("Observation Template", MRDT, "consume_stock"), 0)
		self.assertIn(CBC, setup.catalogue_templates())
		self.assertIn("Urine pH", setup.catalogue_templates())
