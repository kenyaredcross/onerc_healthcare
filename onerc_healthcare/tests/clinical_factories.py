# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""B1 test helpers: consulting visits, consultations through the real endpoints, ICD-11 codes. Never commit."""

from __future__ import annotations

from contextlib import contextmanager

import frappe

from onerc_healthcare.constants import (
	DEPT_OUTPATIENT,
	ENCOUNTER_DOCTYPE,
	VISIT_DOCTYPE,
	VISIT_STATUS_ROUTED,
)
from onerc_healthcare.tests.visit_factories import make_visit, make_visit_users, make_vital_signs
from onerc_healthcare.visit.engine import transition

# A lab test from the provisional catalogue (Observation Template, sample collection required).
LAB_TEST = "Fasting Blood Sugar"
LAB_TEST_2 = "Erythrocyte Sedimentation Rate"
# ICD-11 codes from the bundled release: leaf, parent (non-leaf), and a chapter X extension code.
LEAF_CODE = "1A00-ICD-11"  # Cholera
LEAF_CODE_2 = "1A01-ICD-11"
PARENT_CODE = "1A03-ICD-11"  # has children 1A03.0 ...


@contextmanager
def as_user(user: str):
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user("Administrator")


def users() -> dict[str, str]:
	"""{key: user} for every visit role (t-pv-<key>@test.krcs)."""
	return make_visit_users()


def routed_visit(department: str = DEPT_OUTPATIENT, patient: str | None = None):
	"""A visit Routed to `department`, with submitted triage vital signs (chief complaint set)."""
	visit = make_visit(patient=patient)
	transition(visit.name, "start_triage", system=True)
	make_vital_signs(visit.patient, visit=visit.name, submit=True)
	visit = transition(visit.name, "route", system=True, department=department)
	assert visit.status == VISIT_STATUS_ROUTED
	return visit


def start(visit: str, user: str) -> str:
	"""Start the consultation of `visit` as `user` through the endpoint. Returns the encounter name."""
	from onerc_healthcare.clinical.consultation import start_consultation

	with as_user(user):
		return start_consultation(visit)["encounter"]


def chapter_x_code() -> str:
	return frappe.db.get_value("Code Value", {"code_system": "ICD-11", "krcs_chapter": "X"}, "name")


def add_diagnosis(encounter: str, code: str = LEAF_CODE, primary: int = 1, case_type: str = "New"):
	doc = frappe.get_doc(ENCOUNTER_DOCTYPE, encounter)
	doc.append("krcs_diagnoses", {"code": code, "is_primary": primary, "case_type": case_type})
	doc.save(ignore_permissions=True)
	return doc


def add_lab_row(encounter: str, test: str = LAB_TEST):
	doc = frappe.get_doc(ENCOUNTER_DOCTYPE, encounter)
	doc.append("lab_test_prescription", {"observation_template": test})
	doc.save(ignore_permissions=True)
	return doc


def get_test_item() -> str:
	"""A non-stock, enabled T- drug Item (C-5: never a stock or billable item)."""
	code = "T-Paracetamol 500 mg Tablet"
	if not frappe.db.exists("Item", code):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_name": code,
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"include_item_in_manufacturing": 0,
			}
		).insert(ignore_permissions=True)
	return code


def add_drug_row(encounter: str):
	doc = frappe.get_doc(ENCOUNTER_DOCTYPE, encounter)
	doc.append(
		"drug_prescription",
		{
			"drug_code": get_test_item(),
			"dosage": frappe.db.get_value("Prescription Dosage", {}, "name"),
			"period": frappe.db.get_value("Prescription Duration", {}, "name"),
			"dosage_form": frappe.db.get_value("Dosage Form", {}, "name"),
		},
	)
	doc.save(ignore_permissions=True)
	return doc


def make_submitted_encounter(patient: str, visit: str | None = None, link: bool = True, submit: bool = True):
	"""Same contract as `visit_factories.make_submitted_encounter`, but valid under the B1 encounter guards.

	The visit must be In Consultation. Submits as server code (`flags.krcs_via_consultation`) with a
	no-diagnosis reason, without touching the visit status.
	"""
	from onerc_healthcare.tests.visit_factories import get_test_appointment_type, get_test_practitioner
	from onerc_healthcare.visit.engine import link_encounter

	doc = frappe.get_doc(
		{
			"doctype": ENCOUNTER_DOCTYPE,
			"patient": patient,
			"practitioner": get_test_practitioner(),
			"appointment_type": get_test_appointment_type(),
			"company": frappe.db.get_value(VISIT_DOCTYPE, visit, "company") if visit else None,
			"krcs_visit": visit,
			"krcs_no_diagnosis_reason": "T- factory encounter",
		}
	)
	doc.insert(ignore_permissions=True)
	if submit:
		doc.flags.krcs_via_consultation = True
		doc.submit()
		doc.flags.krcs_via_consultation = False
	if visit and link:
		link_encounter(visit, doc.name)
	return doc


def visit_status(visit: str) -> str:
	return frappe.db.get_value(VISIT_DOCTYPE, visit, "status")


# D63: a Mental Health consultation completes only with the risk ratings and mood (clinical/mh_note.py).
MINIMUM_MH_NOTE = {
	"krcs_mh_suicide_risk": "None identified",
	"krcs_mh_harm_to_others": "None identified",
	"krcs_mh_mood": "T- euthymic",
}


def fill_mh_note(encounter: str, **values) -> None:
	"""Give a draft Mental Health encounter the minimum note (plus `values`) so it can be completed. Does nothing
	for other departments or a note that is already filled in."""
	doc = frappe.get_doc(ENCOUNTER_DOCTYPE, encounter)
	if doc.docstatus != 0 or doc.get("krcs_department") != "Mental Health":
		return
	missing = {k: v for k, v in MINIMUM_MH_NOTE.items() if not doc.get(k)}
	if not (missing or values):
		return
	doc.update({**missing, **values})
	doc.save(ignore_permissions=True)


__all__ = [
	"LAB_TEST",
	"LAB_TEST_2",
	"LEAF_CODE",
	"LEAF_CODE_2",
	"MINIMUM_MH_NOTE",
	"PARENT_CODE",
	"add_diagnosis",
	"add_drug_row",
	"add_lab_row",
	"as_user",
	"chapter_x_code",
	"fill_mh_note",
	"get_test_item",
	"make_submitted_encounter",
	"routed_visit",
	"start",
	"users",
	"visit_status",
]
