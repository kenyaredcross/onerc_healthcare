# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Pharmacy: catalogue, dispensing, line states, visit events and worklist (FR-19..22, BR-6, C-5, SDD §5.5)."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import add_days, today

from onerc_healthcare.constants import (
	DEPT_OUTPATIENT,
	DISPENSING_DOCTYPE,
	ENCOUNTER_DOCTYPE,
	REPORT_PHARMACY_WORKLIST,
	ROLE_PHARMACIST,
	VISIT_DOCTYPE,
	VISIT_STATUS_AWAITING_LAB,
	VISIT_STATUS_AWAITING_PHARMACY,
	VISIT_STATUS_DISCHARGED,
)
from onerc_healthcare.emr_pharmacy.report.pharmacy_worklist import pharmacy_worklist
from onerc_healthcare.pharmacy import catalogue, dispensing
from onerc_healthcare.pharmacy import setup as pharmacy_setup
from onerc_healthcare.pharmacy.dispensing import (
	LINE_CLOSED,
	LINE_DISPENSED,
	LINE_NOT_DISPENSED,
	LINE_PARTLY,
	LINE_PENDING,
	LINE_STATES,
	MR_DOCTYPE,
	LineResolvedError,
	OverDispenseError,
	compute_line_state,
)
from onerc_healthcare.pharmacy.tasks import is_pharmacy_complete
from onerc_healthcare.tests.base import EMRTestCase
from onerc_healthcare.tests.factories import get_facility, make_patient, make_user
from onerc_healthcare.tests.visit_factories import make_visit, make_visit_users
from onerc_healthcare.visit import engine, tasks
from onerc_healthcare.visit.patients import InactivePatientError

PARA = "Paracetamol 500 mg Tablet"
AMOX = "Amoxicillin 500 mg Capsule"
IBU = "Ibuprofen 400 mg Tablet"


class PharmacyTestCase(EMRTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.users = make_visit_users()
		cls.users["pharmacist2"] = make_user("pv-pharmacist2", [ROLE_PHARMACIST])

	@contextmanager
	def as_user(self, key: str):
		frappe.set_user(self.users[key])
		try:
			yield
		finally:
			frappe.set_user("Administrator")

	def prescribed_visit(self, drugs=((PARA, "BID", "5 Day"),), lab=False):
		"""A visit through B1's consultation flow: start_consultation -> drug rows -> complete_consultation.

		Marley creates the Medication Requests on submit and B1 raises `prescribe` (Awaiting Pharmacy).
		With `lab`, a laboratory task is open when the consultation is completed.
		"""
		from onerc_healthcare.clinical.consultation import complete_consultation, start_consultation

		patient = make_patient().name
		visit = make_visit(patient=patient).name
		engine.transition(visit, engine.ACTION_START_TRIAGE)
		engine.transition(
			visit, engine.ACTION_ROUTE, department=DEPT_OUTPATIENT, without_vitals_reason="T- test routing"
		)
		encounter = start_consultation(visit)["encounter"]
		if drugs:
			enc = frappe.get_doc(ENCOUNTER_DOCTYPE, encounter)
			for medication, dosage, period in drugs:
				enc.append(
					"drug_prescription",
					{
						"medication": medication,
						"drug_code": medication,
						"dosage": dosage,
						"period": period,
						"dosage_form": frappe.db.get_value("Medication", medication, "dosage_form"),
					},
				)
			enc.save()
		if lab:
			tasks.raise_event(visit, engine.ACTION_REQUEST_LAB)
		complete_consultation(
			encounter, reason="T- results pending" if lab else None, no_diagnosis_reason="T- test"
		)
		mrs = {
			r.medication: r.name
			for r in frappe.get_all(
				MR_DOCTYPE, filters={"order_group": encounter, "docstatus": 1}, fields=["name", "medication"]
			)
		}
		return frappe._dict(patient=patient, visit=visit, encounter=encounter, mrs=mrs)

	def dispense(self, mr: str, qty=None, outcome="Dispensed", final=0, submit=True, user="pharmacist", **kw):
		with self.as_user(user):
			doc = frappe.get_doc(
				{
					"doctype": DISPENSING_DOCTYPE,
					"medication_request": mr,
					"outcome": outcome,
					"quantity": qty,
					"final": final,
					**kw,
				}
			).insert()
			if submit:
				doc.submit()
		return doc

	def total(self, mr: str) -> float:
		return frappe.db.get_value(MR_DOCTYPE, mr, "total_dispensable_quantity")

	def line(self, mr: str) -> frappe._dict:
		return frappe.db.get_value(
			MR_DOCTYPE, mr, ["krcs_dispense_status", "krcs_qty_dispensed", "status"], as_dict=True
		)

	def visit(self, name: str) -> frappe._dict:
		return frappe.db.get_value(VISIT_DOCTYPE, name, ["status", "pharmacy_open", "lab_open"], as_dict=True)


class TestCatalogue(PharmacyTestCase):
	def test_catalogue_seeded_non_stock_without_prices(self):
		"""OI-9 / C-5: every catalogue medicine is an enabled non-stock Item with no Item Price."""
		data = catalogue.load_catalogue()
		names = [m["name"] for m in data["medications"]]
		self.assertGreaterEqual(len(names), 20)
		self.assertIn("OI-9", data["provisional_flag"])
		for name in names:
			med = frappe.db.get_value(
				"Medication", name, ["is_orderable", "disabled", "price_list"], as_dict=True
			)
			self.assertTrue(med, name)
			self.assertEqual((med.is_orderable, med.disabled), (1, 0), name)
			item = frappe.db.get_value(
				"Item", name, ["is_stock_item", "disabled", "is_sales_item", "item_group"], as_dict=True
			)
			self.assertEqual((item.is_stock_item, item.disabled, item.is_sales_item), (0, 0, 0), name)
			self.assertEqual(item.item_group, data["item_group"])
			row = frappe.db.get_value(
				"Medication Linked Item",
				{"parent": name},
				["item", "is_billable", "change_in_item"],
				as_dict=True,
			)
			self.assertEqual((row.item, row.is_billable, row.change_in_item), (name, 1, 0), name)
		self.assertFalse(frappe.db.exists("Item Price", {"item_code": ["in", names]}))
		self.assertFalse(frappe.db.exists("Bin", {"item_code": ["in", names]}))

	def test_catalogue_seed_is_idempotent(self):
		before = (
			frappe.db.count("Medication"),
			frappe.db.count("Item"),
			frappe.db.count("Medication Linked Item"),
		)
		self.assertEqual(catalogue.seed_catalogue(), [])
		pharmacy_setup.install()
		after = (
			frappe.db.count("Medication"),
			frappe.db.count("Item"),
			frappe.db.count("Medication Linked Item"),
		)
		self.assertEqual(before, after)

	def test_admin_edit_survives_seed(self):
		frappe.db.set_value("Medication", PARA, "default_prescription_dosage", "QID")
		catalogue.seed_catalogue()
		self.assertEqual(frappe.db.get_value("Medication", PARA, "default_prescription_dosage"), "QID")

	def test_saving_a_medication_creates_no_price_and_keeps_item_enabled(self):
		"""Marley's Medication.on_update must not create an Item Price or disable the Item (C-5)."""
		frappe.get_doc("Medication", PARA).save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Item", PARA, "disabled"), 0)
		self.assertEqual(frappe.db.get_value("Item", PARA, "is_stock_item"), 0)
		self.assertFalse(frappe.db.exists("Item Price", {"item_code": PARA}))

	def test_clinician_can_pick_catalogue_items_in_drug_row(self):
		"""FR-19: Marley's drug search finds catalogue items; disabled Items are filtered out."""
		from healthcare.healthcare.doctype.patient_encounter.patient_encounter import (
			get_medications,
			get_medications_query,
		)

		patient = make_patient().name
		filters = {"medication": PARA, "patient": patient}
		with self.as_user("clinician"):
			rows = get_medications_query("Item", "", "name", 0, 20, filters)
			self.assertIn(PARA, [r[0] for r in rows])
			self.assertEqual([r.item for r in get_medications(PARA)], [PARA])
			found = get_medications_query("Item", "Paracetamol", "name", 0, 50, {"patient": patient})
			self.assertIn(PARA, [r[0] for r in found])
		frappe.db.set_value("Item", PARA, "disabled", 1)
		with self.as_user("clinician"):
			self.assertNotIn(PARA, [r[0] for r in get_medications_query("Item", "", "name", 0, 20, filters)])

	def test_dispensable_item_query(self):
		with self.as_user("pharmacist"):
			rows = catalogue.dispensable_item_query("Item", "", "name", 0, 50, {"medication": AMOX})
			self.assertEqual([r[0] for r in rows], [AMOX])
		with self.as_user("sysadmin"), self.assertRaises(frappe.PermissionError):
			catalogue.dispensable_item_query("Item", "", "name", 0, 50, {})

	def test_custom_fields_on_medication_request(self):
		meta = frappe.get_meta(MR_DOCTYPE)
		status = meta.get_field("krcs_dispense_status")
		self.assertEqual(status.options.split("\n"), list(LINE_STATES))
		self.assertEqual(status.default, LINE_PENDING)
		self.assertTrue(status.read_only)
		self.assertTrue(meta.get_field("krcs_qty_dispensed").read_only)


class TestLineStates(PharmacyTestCase):
	def test_compute_line_state_table(self):
		"""Every row of the line-state table (introspectable for S1)."""
		cases = [
			(10, 0, False, LINE_PENDING),
			(10, 4, False, LINE_PARTLY),
			(10, 10, False, LINE_DISPENSED),
			(10, 10, True, LINE_DISPENSED),
			(10, 0, True, LINE_NOT_DISPENSED),
			(10, 4, True, LINE_CLOSED),
		]
		for total, dispensed, final, expected in cases:
			self.assertEqual(compute_line_state(total, dispensed, final), expected, (total, dispensed, final))
		self.assertEqual(set(dispensing.OPEN_LINE_STATES), {LINE_PENDING, LINE_PARTLY})
		self.assertEqual(
			{k: v.marley_status for k, v in LINE_STATES.items()},
			{
				LINE_PENDING: "active-Medication Request Status",
				LINE_PARTLY: "active-Medication Request Status",
				LINE_DISPENSED: "completed-Medication Request Status",
				LINE_NOT_DISPENSED: "stopped-Medication Request Status",
				LINE_CLOSED: "completed-Medication Request Status",
			},
		)
		for state in LINE_STATES.values():
			self.assertTrue(frappe.db.exists("Code Value", state.marley_status), state.marley_status)

	def test_new_line_is_pending(self):
		v = self.prescribed_visit()
		self.assertEqual(self.line(v.mrs[PARA]).krcs_dispense_status, LINE_PENDING)
		self.assertEqual(self.total(v.mrs[PARA]), 10)  # BID x 5 days

	def test_pending_to_dispensed_closes_visit(self):
		"""FR-21: full dispensing resolves the line and raises `dispensed` (visit Discharged)."""
		v = self.prescribed_visit()
		self.assertEqual(self.visit(v.visit).status, VISIT_STATUS_AWAITING_PHARMACY)
		doc = self.dispense(v.mrs[PARA], qty=10)
		self.assertEqual(doc.dispensed_by, self.users["pharmacist"])
		self.assertTrue(doc.dispensed_at)
		self.assertEqual((doc.dispensed_item, doc.is_substitution), (PARA, 0))
		self.assertEqual((doc.visit, doc.encounter, doc.patient), (v.visit, v.encounter, v.patient))
		self.assertEqual(
			self.line(v.mrs[PARA]),
			{
				"krcs_dispense_status": LINE_DISPENSED,
				"krcs_qty_dispensed": 10,
				"status": "completed-Medication Request Status",
			},
		)
		self.assertEqual(
			self.visit(v.visit), {"status": VISIT_STATUS_DISCHARGED, "pharmacy_open": 0, "lab_open": 0}
		)

	def test_partial_then_rest(self):
		v = self.prescribed_visit()
		mr = v.mrs[PARA]
		self.dispense(mr, qty=4)
		self.assertEqual(self.line(mr).krcs_dispense_status, LINE_PARTLY)
		self.assertEqual(self.line(mr).krcs_qty_dispensed, 4)
		self.assertEqual(self.line(mr).status, "active-Medication Request Status")
		self.assertEqual(self.visit(v.visit).status, VISIT_STATUS_AWAITING_PHARMACY)
		second = self.dispense(mr, qty=6)
		self.assertEqual(second.previously_dispensed, 4)
		self.assertEqual(self.line(mr).krcs_dispense_status, LINE_DISPENSED)
		self.assertEqual(self.visit(v.visit).status, VISIT_STATUS_DISCHARGED)

	def test_partial_then_final_close(self):
		v = self.prescribed_visit()
		mr = v.mrs[PARA]
		self.dispense(mr, qty=4)
		self.dispense(mr, qty=2, final=1)
		line = self.line(mr)
		self.assertEqual((line.krcs_dispense_status, line.krcs_qty_dispensed), (LINE_CLOSED, 6))
		self.assertEqual(line.status, "completed-Medication Request Status")
		self.assertEqual(self.visit(v.visit).status, VISIT_STATUS_DISCHARGED)

	def test_first_record_final_with_partial_quantity_closes(self):
		v = self.prescribed_visit()
		self.dispense(v.mrs[PARA], qty=3, final=1)
		self.assertEqual(self.line(v.mrs[PARA]).krcs_dispense_status, LINE_CLOSED)

	def test_not_dispensed_requires_reason(self):
		v = self.prescribed_visit()
		mr = v.mrs[PARA]
		with self.assertRaises(frappe.ValidationError):
			self.dispense(mr, outcome="Not Dispensed")
		doc = self.dispense(mr, outcome="Not Dispensed", qty=5, not_dispensed_reason="T- out of stock")
		self.assertEqual((doc.final, doc.quantity, doc.dispensed_item), (1, 0, None))
		self.assertEqual(
			self.line(mr),
			{
				"krcs_dispense_status": LINE_NOT_DISPENSED,
				"krcs_qty_dispensed": 0,
				"status": "stopped-Medication Request Status",
			},
		)
		self.assertEqual(self.visit(v.visit).status, VISIT_STATUS_DISCHARGED)

	def test_partly_dispensed_then_not_dispensed_closes(self):
		v = self.prescribed_visit()
		mr = v.mrs[PARA]
		self.dispense(mr, qty=4)
		self.dispense(mr, outcome="Not Dispensed", not_dispensed_reason="T- stock ran out")
		self.assertEqual(self.line(mr).krcs_dispense_status, LINE_CLOSED)

	def test_resolved_line_accepts_no_more_records(self):
		for setup in (
			lambda mr: self.dispense(mr, qty=10),
			lambda mr: self.dispense(mr, outcome="Not Dispensed", not_dispensed_reason="T- reason"),
			lambda mr: self.dispense(mr, qty=1, final=1),
		):
			v = self.prescribed_visit()
			mr = v.mrs[PARA]
			setup(mr)
			with self.assertRaises(LineResolvedError):
				self.dispense(mr, qty=1)
			with self.assertRaises(LineResolvedError):
				self.dispense(mr, outcome="Not Dispensed", not_dispensed_reason="T- reason")

	def test_quantity_required(self):
		v = self.prescribed_visit()
		for qty in (None, 0, -1):
			with self.assertRaises(frappe.ValidationError):
				self.dispense(v.mrs[PARA], qty=qty)

	def test_over_dispensing_blocked(self):
		"""Decision 2026-11-13: dispensing more than prescribed is blocked."""
		v = self.prescribed_visit()
		mr = v.mrs[PARA]
		with self.assertRaises(OverDispenseError):
			self.dispense(mr, qty=11)
		self.dispense(mr, qty=8)
		with self.assertRaises(OverDispenseError):
			self.dispense(mr, qty=3)
		# A draft does not count; the total is re-checked at submit.
		draft_a = self.dispense(mr, qty=2, submit=False)
		draft_b = self.dispense(mr, qty=2, submit=False)
		with self.as_user("pharmacist"):
			draft_a.submit()
			with self.assertRaises(LineResolvedError):
				draft_b.reload()
				draft_b.submit()
		self.assertEqual(self.line(mr).krcs_qty_dispensed, 10)

	def test_multiple_lines_event_only_when_all_resolved(self):
		v = self.prescribed_visit(drugs=((PARA, "BID", "5 Day"), (AMOX, "TID", "5 Day")))
		self.dispense(v.mrs[PARA], qty=10)
		self.assertEqual(self.visit(v.visit).status, VISIT_STATUS_AWAITING_PHARMACY)
		self.assertFalse(is_pharmacy_complete(v.visit))
		self.dispense(v.mrs[AMOX], outcome="Not Dispensed", not_dispensed_reason="T- allergy reported")
		self.assertTrue(is_pharmacy_complete(v.visit))
		self.assertEqual(self.visit(v.visit).status, VISIT_STATUS_DISCHARGED)

	def test_lab_pending_goes_to_awaiting_lab(self):
		v = self.prescribed_visit(lab=True)
		self.assertEqual(self.visit(v.visit).status, VISIT_STATUS_AWAITING_PHARMACY)
		self.dispense(v.mrs[PARA], qty=10)
		self.assertEqual(
			self.visit(v.visit), {"status": VISIT_STATUS_AWAITING_LAB, "pharmacy_open": 0, "lab_open": 1}
		)


class TestSubstitution(PharmacyTestCase):
	def test_substitution_requires_reason_and_keeps_prescribed(self):
		"""FR-22: the original line stays, the substitute is linked, both show in the history."""
		v = self.prescribed_visit()
		mr = v.mrs[PARA]
		with self.assertRaises(frappe.ValidationError):
			self.dispense(mr, qty=10, dispensed_medication=IBU)
		doc = self.dispense(
			mr, qty=10, dispensed_medication=IBU, substitution_reason="T- paracetamol out of stock"
		)
		self.assertEqual(
			(
				doc.prescribed_medication,
				doc.prescribed_item,
				doc.dispensed_medication,
				doc.dispensed_item,
				doc.is_substitution,
			),
			(PARA, PARA, IBU, IBU, 1),
		)
		self.assertEqual(frappe.db.get_value(MR_DOCTYPE, mr, "medication"), PARA)
		self.assertEqual(self.line(mr).krcs_dispense_status, LINE_DISPENSED)
		history = dispensing.dispensing_history(v.encounter)
		self.assertEqual(len(history), 1)
		self.assertEqual(
			(history[0].prescribed_item, history[0].dispensed_item, history[0].is_substitution),
			(PARA, IBU, 1),
		)

	def test_substitute_by_item_only(self):
		v = self.prescribed_visit()
		doc = self.dispense(v.mrs[PARA], qty=5, dispensed_item=AMOX, substitution_reason="T- reason")
		self.assertEqual((doc.dispensed_medication, doc.is_substitution), (AMOX, 1))

	def test_same_medicine_is_no_substitution(self):
		v = self.prescribed_visit()
		doc = self.dispense(v.mrs[PARA], qty=5, dispensed_medication=PARA, substitution_reason="T- ignored")
		self.assertEqual((doc.dispensed_item, doc.is_substitution, doc.substitution_reason), (PARA, 0, None))

	def test_item_must_match_medication_and_be_enabled(self):
		v = self.prescribed_visit()
		with self.assertRaises(frappe.ValidationError):
			self.dispense(
				v.mrs[PARA], qty=5, dispensed_medication=IBU, dispensed_item=AMOX, substitution_reason="x"
			)
		frappe.db.set_value("Item", IBU, "disabled", 1)
		with self.assertRaises(frappe.ValidationError):
			self.dispense(v.mrs[PARA], qty=5, dispensed_item=IBU, substitution_reason="x")


class TestDispensingRules(PharmacyTestCase):
	def test_request_must_be_active(self):
		v = self.prescribed_visit()
		mr = v.mrs[PARA]
		frappe.db.set_value(MR_DOCTYPE, mr, "status", "on-hold-Medication Request Status")
		with self.assertRaises(frappe.ValidationError):
			self.dispense(mr, qty=1)
		frappe.db.set_value(MR_DOCTYPE, mr, "docstatus", 2)
		with self.assertRaises(frappe.ValidationError):
			self.dispense(mr, qty=1)

	def test_inactive_patient_refused(self):
		v = self.prescribed_visit()
		frappe.db.set_value("Patient", v.patient, "status", "Disabled")
		with self.assertRaises(InactivePatientError):
			self.dispense(v.mrs[PARA], qty=1)

	def test_server_sets_prescription_fields(self):
		v = self.prescribed_visit()
		doc = self.dispense(
			v.mrs[PARA],
			qty=1,
			submit=False,
			patient=make_patient().name,
			prescribed_item=AMOX,
			dispensed_by=self.users["clinician"],
		)
		self.assertEqual((doc.patient, doc.prescribed_item, doc.prescribed_quantity), (v.patient, PARA, 10))
		self.assertFalse(doc.dispensed_by)

	def test_dispensing_after_visit_closed(self):
		"""Visit closed with pending lines: nothing changes; the line stays on the worklist and can be dispensed."""
		v = self.prescribed_visit()
		engine.transition(v.visit, engine.ACTION_CLOSE_VISIT, closure_reason="Other", comment="T- left")
		rows = pharmacy_worklist.execute({})[1]
		row = next(r for r in rows if r["medication_request"] == v.mrs[PARA])
		self.assertEqual(row["visit_status"], VISIT_STATUS_DISCHARGED)
		self.dispense(v.mrs[PARA], qty=10)
		self.assertEqual(self.line(v.mrs[PARA]).krcs_dispense_status, LINE_DISPENSED)
		self.assertEqual(self.visit(v.visit).status, VISIT_STATUS_DISCHARGED)
		self.assertTrue(
			frappe.db.exists(
				"Comment",
				{
					"reference_doctype": VISIT_DOCTYPE,
					"reference_name": v.visit,
					"content": ["like", "%dispensed%"],
				},
			)
		)


class TestCancel(PharmacyTestCase):
	def test_cancel_rules(self):
		"""Pharmacist only, own record, reason required; the line is recomputed."""
		v = self.prescribed_visit(drugs=((PARA, "BID", "5 Day"), (AMOX, "TID", "5 Day")))
		mr = v.mrs[PARA]
		doc = self.dispense(mr, qty=10)
		self.assertEqual(self.line(mr).krcs_dispense_status, LINE_DISPENSED)
		with self.as_user("pharmacist2"), self.assertRaises(frappe.PermissionError):
			dispensing.cancel_dispensing(doc.name, "T- wrong patient")
		with self.as_user("clinician"), self.assertRaises(frappe.PermissionError):
			dispensing.cancel_dispensing(doc.name, "T- wrong patient")
		with self.as_user("pharmacist"):
			with self.assertRaises(frappe.ValidationError):
				dispensing.cancel_dispensing(doc.name, "  ")
			doc.reload()
			with self.assertRaises(frappe.ValidationError):
				doc.cancel()  # standard cancel without a reason
			result = dispensing.cancel_dispensing(doc.name, "T- recorded on the wrong line")
		self.assertEqual(result["docstatus"], 2)
		self.assertEqual(result["line_status"], LINE_PENDING)
		self.assertEqual(
			frappe.db.get_value(DISPENSING_DOCTYPE, doc.name, "cancel_reason"),
			"T- recorded on the wrong line",
		)
		self.assertEqual(
			self.line(mr),
			{
				"krcs_dispense_status": LINE_PENDING,
				"krcs_qty_dispensed": 0,
				"status": "active-Medication Request Status",
			},
		)
		self.assertEqual(self.visit(v.visit).status, VISIT_STATUS_AWAITING_PHARMACY)

	def test_cancel_reopens_line_states(self):
		"""Cancel transitions: Dispensed -> Partly, Closed -> Partly, Not Dispensed -> Pending."""
		v = self.prescribed_visit(
			drugs=((PARA, "BID", "5 Day"), (AMOX, "TID", "5 Day"), (IBU, "TID", "3 Day"))
		)
		a = v.mrs[PARA]
		self.dispense(a, qty=4)
		last = self.dispense(a, qty=6)
		with self.as_user("pharmacist"):
			dispensing.cancel_dispensing(last.name, "T- reason")
		self.assertEqual(self.line(a).krcs_dispense_status, LINE_PARTLY)
		b = v.mrs[AMOX]
		self.dispense(b, qty=3)
		final = self.dispense(b, qty=1, final=1)
		self.assertEqual(self.line(b).krcs_dispense_status, LINE_CLOSED)
		with self.as_user("pharmacist"):
			dispensing.cancel_dispensing(final.name, "T- reason")
		self.assertEqual(self.line(b).krcs_dispense_status, LINE_PARTLY)
		c = v.mrs[IBU]
		nd = self.dispense(c, outcome="Not Dispensed", not_dispensed_reason="T- reason")
		with self.as_user("pharmacist"):
			dispensing.cancel_dispensing(nd.name, "T- reason")
		self.assertEqual(self.line(c).krcs_dispense_status, LINE_PENDING)

	def test_cancel_refused_once_visit_closed(self):
		v = self.prescribed_visit()
		doc = self.dispense(v.mrs[PARA], qty=10)
		self.assertEqual(self.visit(v.visit).status, VISIT_STATUS_DISCHARGED)
		with self.as_user("pharmacist"), self.assertRaises(frappe.ValidationError):
			dispensing.cancel_dispensing(doc.name, "T- reason")
		self.assertEqual(self.line(v.mrs[PARA]).krcs_dispense_status, LINE_DISPENSED)

	def test_cancel_reopens_pharmacy_task(self):
		"""Visit still open (lab pending) with the pharmacy task closed: a cancel raises `prescribe`."""
		v = self.prescribed_visit(lab=True)
		doc = self.dispense(v.mrs[PARA], qty=10)
		self.assertEqual(
			self.visit(v.visit), {"status": VISIT_STATUS_AWAITING_LAB, "pharmacy_open": 0, "lab_open": 1}
		)
		with self.as_user("pharmacist"):
			dispensing.cancel_dispensing(doc.name, "T- reason")
		self.assertEqual(
			self.visit(v.visit), {"status": VISIT_STATUS_AWAITING_PHARMACY, "pharmacy_open": 1, "lab_open": 1}
		)

	def test_draft_cannot_be_cancelled(self):
		v = self.prescribed_visit()
		doc = self.dispense(v.mrs[PARA], qty=1, submit=False)
		with self.as_user("pharmacist"), self.assertRaises(frappe.ValidationError):
			dispensing.cancel_dispensing(doc.name, "T- reason")


class TestTaskChecker(PharmacyTestCase):
	def test_is_pharmacy_complete(self):
		self.assertFalse(is_pharmacy_complete(make_visit().name))  # no encounter
		empty = self.prescribed_visit(drugs=())
		self.assertTrue(is_pharmacy_complete(empty.visit))  # submitted, nothing prescribed
		self.assertEqual(self.visit(empty.visit).status, VISIT_STATUS_DISCHARGED)
		v = self.prescribed_visit()
		self.assertFalse(is_pharmacy_complete(v.visit))

	def test_reconciler_raises_dispensed(self):
		"""Registered as TASK_CHECKERS["pharmacy"], the reconciler closes a task missed by the event."""
		v = self.prescribed_visit()
		# Resolve the line behind the event's back (simulates a missed event).
		frappe.db.set_value(MR_DOCTYPE, v.mrs[PARA], "krcs_dispense_status", LINE_DISPENSED)
		self.assertEqual(self.visit(v.visit).status, VISIT_STATUS_AWAITING_PHARMACY)
		with patch.dict(
			tasks.TASK_CHECKERS, {"pharmacy": "onerc_healthcare.pharmacy.tasks.is_pharmacy_complete"}
		):
			self.assertEqual(tasks.reconcile_visit(v.visit), [engine.ACTION_DISPENSED])
		self.assertEqual(self.visit(v.visit).status, VISIT_STATUS_DISCHARGED)


class TestWorklist(PharmacyTestCase):
	def rows(self, **filters):
		return [r for r in pharmacy_worklist.execute(filters)[1]]

	def test_worklist_rows(self):
		"""FR-20: open lines with patient, medicine, dose, frequency, duration and quantities."""
		v = self.prescribed_visit(drugs=((PARA, "BID", "5 Day"), (AMOX, "TID", "5 Day")))
		facility = frappe.db.get_value(VISIT_DOCTYPE, v.visit, "facility")
		rows = {r["medication_request"]: r for r in self.rows(facility=facility, date=today())}
		para = rows[v.mrs[PARA]]
		self.assertEqual(para["patient"], v.patient)
		self.assertEqual(para["visit"], v.visit)
		self.assertEqual(para["medication"], PARA)
		self.assertEqual(para["dose"], "500 Milligram")
		self.assertEqual((para["frequency"], para["duration"]), ("BID", "5 Day"))
		self.assertEqual((para["prescribed_qty"], para["dispensed_qty"], para["remaining_qty"]), (10, 0, 10))
		self.assertEqual(para["action"], "Dispense")
		self.assertIn(v.mrs[AMOX], rows)
		self.dispense(v.mrs[PARA], qty=4)
		para = next(r for r in self.rows(facility=facility) if r["medication_request"] == v.mrs[PARA])
		self.assertEqual(
			(para["dispensed_qty"], para["remaining_qty"], para["dispense_status"]), (4, 6, LINE_PARTLY)
		)
		self.dispense(v.mrs[PARA], qty=6)
		self.assertNotIn(v.mrs[PARA], [r["medication_request"] for r in self.rows(facility=facility)])
		# Filters
		self.assertNotIn(v.mrs[AMOX], [r["medication_request"] for r in self.rows(facility=get_facility(1))])
		self.assertNotIn(v.mrs[AMOX], [r["medication_request"] for r in self.rows(date=add_days(today(), 1))])

	def test_worklist_roles(self):
		from frappe.desk.query_report import run

		v = self.prescribed_visit()
		for key in ("pharmacist", "mic"):
			with self.as_user(key):
				result = run(REPORT_PHARMACY_WORKLIST, filters={})
				self.assertIn(v.mrs[PARA], [r["medication_request"] for r in result["result"]])
		for key in ("clinician", "reception", "sysadmin", "hoo"):
			with self.as_user(key), self.assertRaises(frappe.PermissionError):
				run(REPORT_PHARMACY_WORKLIST, filters={})


class TestPermissions(PharmacyTestCase):
	def test_docperms(self):
		"""Pharmacist rwcs + cancel; CO, MHO, HIS, MIC read; no delete; System Administrator none."""
		path = os.path.join(
			frappe.get_app_path("onerc_healthcare"),
			"emr_pharmacy",
			"doctype",
			"dispensing_record",
			"dispensing_record.json",
		)
		with open(path) as f:
			perms = {p["role"]: p for p in json.load(f)["permissions"]}
		self.assertEqual(
			set(perms),
			{"Pharmacist", "Clinical Officer", "Mental Health Officer", "HIS Officer", "Medical In-Charge"},
		)
		for role, p in perms.items():
			self.assertFalse(p.get("delete"), role)
			self.assertFalse(p.get("amend"), role)
			if role == "Pharmacist":
				self.assertTrue(all(p.get(k) for k in ("read", "write", "create", "submit", "cancel")))
			else:
				self.assertTrue(p.get("read"))
				self.assertFalse(any(p.get(k) for k in ("write", "create", "submit", "cancel")), role)

	def test_role_access(self):
		v = self.prescribed_visit()
		doc = self.dispense(v.mrs[PARA], qty=2)
		for key in ("clinician", "mho", "his", "mic"):
			with self.as_user(key):
				self.assertTrue(frappe.has_permission(DISPENSING_DOCTYPE, "read", doc=doc.name), key)
				with self.assertRaises(frappe.PermissionError):
					self.dispense(v.mrs[PARA], qty=1, user=key)
		for key in ("sysadmin", "reception", "lab", "hoo"):
			with self.as_user(key):
				self.assertFalse(frappe.has_permission(DISPENSING_DOCTYPE, "read", doc=doc.name), key)

	def test_never_deleted(self):
		v = self.prescribed_visit()
		doc = self.dispense(v.mrs[PARA], qty=2, submit=False)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc(DISPENSING_DOCTYPE, doc.name, ignore_permissions=True)
