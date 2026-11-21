# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MCH demo data (integrator), called by `setup.demo.seed_demo`. Fictional people only; re-runnable: a patient
who already has the record is left as she is. Records go through the real controllers as the demo staff.

- Nyandeng Chol, 23, pregnant: an MCH visit, a booked ANC Pregnancy and one submitted ANC Contact.
- Baby Furaha (the newborn of the maternity demo): an MCH visit with BCG and OPV 0 and a growth record.
- Achol Deng, about 4 months: BCG at birth only, so Penta 1 / OPV 1 / PCV 1 / Rota 1 are overdue; a growth record.
- Akuol Garang, 3 (already routed to MCH by the main demo): a growth record on her open visit.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, nowdate

from onerc_healthcare.constants import (
	ANC_CONTACT_DOCTYPE,
	ANC_PREGNANCY_DOCTYPE,
	DEPT_MCH,
	GROWTH_MONITORING_DOCTYPE,
	IDENTIFIER_UNHCR,
	IMMUNISATION_DOCTYPE,
	PATIENT_MOTHER_FIELD,
	VISIT_OPEN_STATUSES,
)

ROUTE_MCH = f"routed:{DEPT_MCH}"

PREGNANT = {
	"first": "Nyandeng",
	"last": "Chol",
	"sex": "Female",
	"age": 23,
	"ids": [(IDENTIFIER_UNHCR, "KBY-25A07731")],
	"facility": 0,
	"stage": ROUTE_MCH,
	"complaint": "Antenatal clinic: second pregnancy, about 24 weeks",
	"level": "Non-urgent",
	"vitals": ("36.7", "84", "18", "112", "70"),
}
INFANT = {
	"first": "Achol",
	"last": "Deng",
	"sex": "Female",
	"age_days": 122,
	"ids": [(IDENTIFIER_UNHCR, "KBY-26C00118")],
	"facility": 0,
	"stage": ROUTE_MCH,
	"complaint": "Child welfare clinic: missed immunisations",
	"level": "Non-urgent",
	"vitals": ("36.8", "132", "34", "", ""),
}
CHILD_VISIT = {"stage": ROUTE_MCH, "level": "Non-urgent", "vitals": ("36.9", "138", "40", "", "")}


def _open_visit(patient: str) -> str | None:
	return frappe.db.get_value(
		"Patient Visit", {"patient": patient, "status": ("in", VISIT_OPEN_STATUSES)}, "name"
	)


def _has(doctype: str, patient: str) -> bool:
	return bool(frappe.db.exists(doctype, {"patient": patient, "docstatus": ("<", 2)}))


def _visit(spec: dict, patient: str, users: dict, facility: str) -> str:
	from onerc_healthcare.setup.demo import _run_visit

	return _open_visit(patient) or _run_visit(spec, patient, users, facility)


def _insert(doc: dict, user: str, submit: bool = True) -> str:
	frappe.set_user(user)
	try:
		d = frappe.get_doc(doc).insert()
		if submit:
			d.submit()
		return d.name
	finally:
		frappe.set_user("Administrator")


def _ensure_infant(spec: dict, receptionist: str, facility: str) -> str:
	itype, value = spec["ids"][0]
	existing = frappe.db.get_value(
		"Patient Identifier",
		{"parenttype": "Patient", "identifier_type": itype, "identifier_value": value},
		"parent",
	)
	if existing:
		return existing
	frappe.set_user(receptionist)
	try:
		return (
			frappe.get_doc(
				{
					"doctype": "Patient",
					"first_name": spec["first"],
					"last_name": spec["last"],
					"sex": spec["sex"],
					"dob": add_days(nowdate(), -spec["age_days"]),
					"krcs_registration_facility": facility,
					"krcs_identifiers": [
						{
							"identifier_type": itype,
							"identifier_value": value,
							"verification_status": "Verified",
							"verified_on": nowdate(),
							"verification_source": "Demo data (fictional)",
						}
					],
				}
			)
			.insert()
			.name
		)
	finally:
		frappe.set_user("Administrator")


def seed_mch(users: dict, facility: str) -> dict:
	from onerc_healthcare.setup.demo import _ensure_patient, _find_patient

	out = {}
	# 1. A pregnant mother: booking and the first contact
	mother = _ensure_patient(PREGNANT, users["reception"], facility)
	if not _has(ANC_CONTACT_DOCTYPE, mother):
		visit = _visit(PREGNANT, mother, users, facility)
		if not frappe.db.exists(ANC_PREGNANCY_DOCTYPE, {"patient": mother}):
			_insert(
				{
					"doctype": ANC_PREGNANCY_DOCTYPE,
					"patient": mother,
					"facility": facility,
					"booking_visit": visit,
					"booking_date": nowdate(),
					"lmp": add_days(nowdate(), -168),
					"gravida": 2,
					"para": "1",
					"blood_group": "O",
					"rhesus": "Positive",
					"hiv_status": "Negative",
					"syphilis": "Non-reactive",
					"haemoglobin": 11.2,
				},
				users["midwife"],
				submit=False,
			)
		_insert(
			{
				"doctype": ANC_CONTACT_DOCTYPE,
				"patient": mother,
				"visit": visit,
				"weight_kg": 58.5,
				"bp_systolic": 112,
				"bp_diastolic": 70,
				"fundal_height_cm": 24,
				"presentation": "Cephalic",
				"foetal_heart_rate": 142,
				"urine_protein": "Nil",
				"td_dose": "Td2",
				"iptp_dose": "IPTp1",
				"iron_folate": 1,
				"llin_given": 1,
				"counselling": "Birth preparedness, danger signs, nutrition",
				"next_visit_date": add_days(nowdate(), 28),
			},
			users["midwife"],
		)
	out["pregnant_mother"] = mother

	# 2. Baby Furaha (the maternity demo newborn): birth doses and a growth record
	furaha = _find_patient({"ids": [(IDENTIFIER_UNHCR, "KBY-25M00402")], "first": "", "last": ""})
	baby = frappe.db.get_value("Patient", {PATIENT_MOTHER_FIELD: furaha}, "name") if furaha else None
	if baby and not _has(IMMUNISATION_DOCTYPE, baby):
		visit = _visit({**CHILD_VISIT, "complaint": "Newborn: birth immunisations"}, baby, users, facility)
		_insert(
			{
				"doctype": IMMUNISATION_DOCTYPE,
				"patient": baby,
				"visit": visit,
				"doses": [
					{"vaccine": "BCG", "batch_number": "BCG-DEMO-01", "site": "Left upper arm"},
					{"vaccine": "OPV 0", "batch_number": "OPV-DEMO-01", "site": "Oral"},
				],
			},
			users["clinician"],
		)
		_insert(
			{"doctype": GROWTH_MONITORING_DOCTYPE, "patient": baby, "visit": visit, "weight_kg": 3.3},
			users["triage"],
		)
	out["newborn"] = baby

	# 3. An infant with overdue doses
	infant = _ensure_infant(INFANT, users["reception"], facility)
	if not _has(IMMUNISATION_DOCTYPE, infant):
		visit = _visit(INFANT, infant, users, facility)
		born = add_days(nowdate(), -INFANT["age_days"])
		_insert(
			{
				"doctype": IMMUNISATION_DOCTYPE,
				"patient": infant,
				"visit": visit,
				"given_on": add_days(born, 1),
				"notes": "BCG given at birth at the health post (card seen). Penta, OPV, PCV and Rota missed.",
				"doses": [{"vaccine": "BCG", "batch_number": "BCG-DEMO-02", "site": "Left upper arm"}],
			},
			users["clinician"],
		)
		_insert(
			{
				"doctype": GROWTH_MONITORING_DOCTYPE,
				"patient": infant,
				"visit": visit,
				"weight_kg": 5.1,
				"length_height_cm": 59.5,
				"measurement_position": "Lying (length)",
				"muac_cm": 12.8,
			},
			users["triage"],
		)
	out["infant"] = infant

	# 4. Akuol Garang (main demo, routed to MCH): a growth record on her open visit
	akuol = _find_patient({"ids": [(IDENTIFIER_UNHCR, "KBY-25B00931")], "first": "", "last": ""})
	visit = _open_visit(akuol) if akuol else None
	if visit and not _has(GROWTH_MONITORING_DOCTYPE, akuol):
		_insert(
			{
				"doctype": GROWTH_MONITORING_DOCTYPE,
				"patient": akuol,
				"visit": visit,
				"weight_kg": 11.4,
				"length_height_cm": 92.0,
				"measurement_position": "Standing (height)",
				"muac_cm": 13.6,
			},
			users["triage"],
		)
	out["child"] = akuol
	return out
