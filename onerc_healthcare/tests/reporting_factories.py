# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Builders for the MOH reconciliation dataset (SDD §9.3, TST-13). Never commit.

Records are made through the real flows, as the staff would make them:

- registration (`make_visit`, as server code so a past ``visit_date`` is kept and the attendance rule
  runs on that date), triage vital signs and routing (`engine.transition(..., system=True)`);
- consultations through `clinical.consultation` (start, diagnoses, lab orders, complete) as a clinician;
- referrals through `clinical.referral.create_referral(send=True)`;
- laboratory results through Marley's sample collection, result entry and approval (the real submit);
- Dispensing Records submitted by a pharmacist, Service Delivered and Community Activity Records by
  clinicians, voids and cancels by the Medical In-Charge, a patient merge by the HIS Officer.

The flows stamp today's date and time, so each builder moves its own timestamps to the scenario day with
``db.set_value`` (visit stage times, encounter date, approval time, dispensing time, referral sent time).
Every name is ``T-`` prefixed.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time

import frappe
from frappe.utils import flt, getdate

from onerc_healthcare.constants import (
	COMMUNITY_ACTIVITY_DOCTYPE,
	DISPENSING_DOCTYPE,
	ENCOUNTER_DOCTYPE,
	REFERRAL_DOCTYPE,
	REFERRAL_FACILITY_DOCTYPE,
	SERVICE_DELIVERED_DOCTYPE,
	VISIT_DOCTYPE,
)
from onerc_healthcare.tests.clinical_factories import as_user
from onerc_healthcare.tests.factories import make_patient
from onerc_healthcare.tests.visit_factories import make_visit, make_visit_users, make_vital_signs
from onerc_healthcare.visit import engine

REFERRAL_FACILITY_NAME = "T-Recon Lodwar County Referral Hospital"
DUPLICATE_REASON_FIELD = "krcs_duplicate_override_reason"
MEDICATION_FIELDS = {
	"Paracetamol 500 mg Tablet": ("BID", "5 Day"),
	"Amoxicillin 500 mg Capsule": ("TID", "5 Day"),
}
VISIT_STAGE_TIMES = (
	"arrived_at",
	"status_changed_at",
	"triage_started_at",
	"routed_at",
	"consultation_started_at",
)


def at(day, hour: int = 9) -> datetime:
	return datetime.combine(getdate(day), time(hour, 0))


def cv(code: str) -> str:
	"""Code Value name of an ICD-11 code."""
	return f"{code}-ICD-11"


def referral_facility() -> str:
	if not frappe.db.exists(REFERRAL_FACILITY_DOCTYPE, {"facility_name": REFERRAL_FACILITY_NAME}):
		frappe.get_doc(
			{
				"doctype": REFERRAL_FACILITY_DOCTYPE,
				"facility_name": REFERRAL_FACILITY_NAME,
				"facility_type": "Hospital",
				"county": "Turkana",
			}
		).insert(ignore_permissions=True)
	return frappe.db.get_value(REFERRAL_FACILITY_DOCTYPE, {"facility_name": REFERRAL_FACILITY_NAME}, "name")


class ReconBuilder:
	"""Builds records through the real flows and remembers them by scenario key (``P01``, ``V01``...)."""

	def __init__(self, facilities: list[str]):
		self.facilities = facilities
		self.users = make_visit_users()
		self.patients: dict[str, str] = {}
		self.visits: dict[str, str] = {}
		self.encounters: dict[str, str] = {}
		self.referrals: dict[str, str] = {}

	# ------------------------------------------------------------------------------------- patients

	def patient(self, key: str, sex: str, dob=None, estimated: tuple | None = None, **kw) -> str:
		"""A T- patient. `estimated`: (years, months, estimated_on) instead of an exact DOB (D52)."""
		values = {"first_name": f"T-Recon{key}", "last_name": f"Case{key}", "sex": sex, **kw}
		if estimated:
			years, months, on = estimated
			values.update(
				{
					"dob": None,
					"krcs_estimated_age_years": years,
					"krcs_estimated_age_months": months,
					"krcs_dob_estimated_on": on,
				}
			)
		else:
			values["dob"] = dob
		self.patients[key] = make_patient(**values).name
		return self.patients[key]

	# ---------------------------------------------------------------------------------------- visits

	def register(
		self,
		key: str,
		patient: str,
		day,
		facility: int = 0,
		outreach: bool = False,
		smartcare: bool = False,
		referred_in: bool = False,
	) -> str:
		"""Register a visit on `day` (server insert keeps the date; the attendance rule runs on it)."""
		kw = {"visit_date": getdate(day), "arrived_at": at(day, 8)}
		if outreach:
			from onerc_healthcare.tests.outreach_factories import make_session

			if not getattr(self, "outreach_session", None):
				self.outreach_session = make_session(facility=self.facilities[facility]).name
			kw.update({"is_outreach": 1, "outreach_session": self.outreach_session})
		if smartcare:
			kw["seen_in_smartcare"] = 1
		if referred_in:
			kw.update({"referred_in": 1, "referred_in_from": referral_facility()})
		with as_user(self.users["reception"]):
			doc = make_visit(
				patient=self.patients[patient], facility=self.facilities[facility], system=True, **kw
			)
		self.visits[key] = doc.name
		return doc.name

	def triage(self, visit: str, department: str | None) -> None:
		"""Start triage, submit triage vital signs and route to `department` (None: stay in triage)."""
		name = self.visits[visit]
		engine.transition(name, "start_triage", system=True)
		make_vital_signs(frappe.db.get_value(VISIT_DOCTYPE, name, "patient"), visit=name, submit=True)
		if department:
			engine.transition(name, "route", system=True, department=department)

	def close(self, visit: str, reason: str, user: str = "reception", outcome: str | None = None) -> None:
		with as_user(self.users[user]):
			engine.apply_action(
				self.visits[visit], "close_visit", closure_reason=reason, comment="T- closed", outcome=outcome
			)

	def backdate(self, visit: str, day, closed_on=None) -> None:
		"""Move the visit's stage, close and outcome times from today to the scenario days."""
		name = self.visits[visit]
		row = frappe.db.get_value(
			VISIT_DOCTYPE, name, [*VISIT_STAGE_TIMES, "closed_at", "outcome_at"], as_dict=True
		)
		values = {f: at(day, 9) for f in VISIT_STAGE_TIMES if row.get(f)}
		values["arrived_at"] = at(day, 8)
		for field in ("closed_at", "outcome_at"):
			if row.get(field):
				values[field] = at(closed_on or day, 15)
		frappe.db.set_value(VISIT_DOCTYPE, name, values, update_modified=False)

	# ---------------------------------------------------------------------------------- consultation

	def consult(
		self,
		key: str,
		visit: str,
		day,
		dx=(),
		drugs=(),
		labs=(),
		user: str = "clinician",
	) -> str:
		"""Start the consultation as `user`, record diagnoses (code, case type, primary), prescriptions and
		lab orders, and date the encounter `day`. Lab rows are sent to the laboratory."""
		from onerc_healthcare.clinical.consultation import send_lab_orders, start_consultation

		with as_user(self.users[user]):
			encounter = start_consultation(self.visits[visit])["encounter"]
		doc = frappe.get_doc(ENCOUNTER_DOCTYPE, encounter)
		for code, case_type, primary in dx:
			doc.append("krcs_diagnoses", {"code": cv(code), "case_type": case_type, "is_primary": primary})
		for medication in drugs:
			dosage, period = MEDICATION_FIELDS[medication]
			doc.append(
				"drug_prescription",
				{
					"medication": medication,
					"drug_code": medication,
					"dosage": dosage,
					"period": period,
					"dosage_form": frappe.db.get_value("Medication", medication, "dosage_form"),
				},
			)
		for template in labs:
			doc.append("lab_test_prescription", {"observation_template": template})
		doc.save(ignore_permissions=True)
		frappe.db.set_value(
			ENCOUNTER_DOCTYPE, encounter, "encounter_date", getdate(day), update_modified=False
		)
		if labs:
			with as_user(self.users[user]):
				send_lab_orders(encounter)
		self.encounters[key] = encounter
		return encounter

	def complete(
		self, encounter: str, outcome: str | None = None, reason: str | None = None, user: str = "clinician"
	) -> None:
		from onerc_healthcare.clinical.consultation import complete_consultation

		with as_user(self.users[user]):
			complete_consultation(self.encounters[encounter], reason=reason, outcome=outcome)

	def refer(
		self,
		key: str,
		encounter: str,
		day,
		to_department: str | None = None,
		external: bool = False,
		target_visit: str | None = None,
		user: str = "clinician",
	) -> str | None:
		"""Send a referral from the consultation on `day`. An internal referral opens the target visit,
		registered as `target_visit` and dated `day`."""
		from onerc_healthcare.clinical.referral import create_referral

		with as_user(self.users[user]):
			result = create_referral(
				self.encounters[encounter],
				"External" if external else "Internal",
				"T- referral",
				to_department=to_department,
				to_facility=referral_facility() if external else None,
				send=True,
			)
		self.referrals[key] = result["name"]
		frappe.db.set_value(REFERRAL_DOCTYPE, result["name"], "sent_at", at(day, 11), update_modified=False)
		target = frappe.db.get_value(REFERRAL_DOCTYPE, result["name"], "target_visit")
		if target and target_visit:
			self.visits[target_visit] = target
			frappe.db.set_value(VISIT_DOCTYPE, target, "visit_date", getdate(day), update_modified=False)
		return target

	# ------------------------------------------------------------------------------------ laboratory

	def orders(self, encounter: str) -> dict[str, str]:
		"""{template: service request} of the encounter's lab orders."""
		rows = frappe.get_all(
			"Service Request",
			filters={"order_group": self.encounters[encounter], "docstatus": 1},
			fields=["name", "template_dn"],
		)
		return {r.template_dn: r.name for r in rows}

	def collect(self, encounter: str) -> None:
		"""Collect every open sample of the encounter (Marley's sample collection, as the lab technician)."""
		from healthcare.healthcare.doctype.sample_collection.sample_collection import create_observation

		collections = frappe.get_all(
			"Sample Collection",
			filters={"reference_name": self.encounters[encounter], "docstatus": ["!=", 2]},
			pluck="name",
		)
		rows = frappe.get_all(
			"Observation Sample Collection",
			filters={"parent": ["in", collections or [""]], "status": "Open"},
			fields=["*"],
			order_by="creation asc",
		)
		for row in rows:
			with as_user(self.users["lab"]):
				create_observation(selected=json.dumps([row], default=str), sample_collection=row.parent)

	def result(self, encounter: str, template: str, value: str, approved_on=None) -> str:
		"""Enter the result of the encounter's `template` order (every leaf of a panel) and, with
		`approved_on`, approve it (the real submit) and date the approval that day. Returns the root."""
		from healthcare.healthcare.doctype.observation.observation import (
			record_observation_result,
			set_observation_status,
		)

		sr = self.orders(encounter)[template]
		root = frappe.db.get_value(
			"Observation",
			{"service_request": sr, "parent_observation": ["is", "not set"], "docstatus": 0},
			"name",
			order_by="creation desc",
		)
		leaves = frappe.get_all(
			"Observation",
			filters={"parent_observation": root, "has_component": 0, "docstatus": 0},
			pluck="name",
		) or [root]
		with as_user(self.users["lab"]):
			record_observation_result(json.dumps([{"observation": o, "result": value} for o in leaves]))
			if approved_on:
				set_observation_status(root, "Approved")
		if approved_on:
			for name in [
				root,
				*frappe.get_all("Observation", filters={"parent_observation": root}, pluck="name"),
			]:
				if frappe.db.get_value("Observation", name, "docstatus") == 1:
					frappe.db.set_value(
						"Observation", name, "time_of_approval", at(approved_on, 10), update_modified=False
					)
		return root

	# -------------------------------------------------------------------------------------- pharmacy

	def lines(self, encounter: str) -> dict[str, str]:
		"""{medication: Medication Request} released when the consultation completed."""
		rows = frappe.get_all(
			"Medication Request",
			filters={"order_group": self.encounters[encounter], "docstatus": 1},
			fields=["name", "medication"],
		)
		return {r.medication: r.name for r in rows}

	def dispense(
		self, encounter: str, medication: str, day, part: float = 1.0, not_dispensed: bool = False
	) -> str:
		"""Submit a Dispensing Record as the pharmacist for `part` of the line's quantity (or Not Dispensed)."""
		mr = self.lines(encounter)[medication]
		total = flt(frappe.db.get_value("Medication Request", mr, "total_dispensable_quantity"))
		values = {"doctype": DISPENSING_DOCTYPE, "medication_request": mr}
		if not_dispensed:
			values.update({"outcome": "Not Dispensed", "not_dispensed_reason": "T- out of stock", "final": 1})
		else:
			done = flt(frappe.db.get_value("Medication Request", mr, "krcs_qty_dispensed"))
			quantity = total - done if part >= 1 else max(1, int(total * part))
			values.update({"outcome": "Dispensed", "quantity": quantity})
		with as_user(self.users["pharmacist"]):
			doc = frappe.get_doc(values).insert()
			doc.submit()
		frappe.db.set_value(DISPENSING_DOCTYPE, doc.name, "dispensed_at", at(day, 14), update_modified=False)
		return doc.name

	# ----------------------------------------------------------------------- services and community

	def service(self, visit: str, service_type: str, day, user: str = "clinician") -> str:
		with as_user(self.users[user]):
			doc = frappe.get_doc(
				{
					"doctype": SERVICE_DELIVERED_DOCTYPE,
					"visit": self.visits[visit],
					"service_type": service_type,
					"service_date": getdate(day),
				}
			).insert()
		return doc.name

	def void_service(self, name: str) -> None:
		from onerc_healthcare.community.services import void_service

		with as_user(self.users["mic"]):
			void_service(name, "T- recorded on the wrong patient")

	def activity(
		self,
		activity_type: str,
		day,
		facility: int = 0,
		male: int = 0,
		female: int = 0,
		total: int = 0,
		submit: bool = True,
	) -> str:
		with as_user(self.users["clinician"]):
			doc = frappe.get_doc(
				{
					"doctype": COMMUNITY_ACTIVITY_DOCTYPE,
					"activity_type": activity_type,
					"activity_date": getdate(day),
					"facility": self.facilities[facility],
					"location": "T-Recon Village 3 meeting point",
					"topic": "T- hygiene and malaria prevention",
					"participants_male": male,
					"participants_female": female,
					"participants_total": total,
				}
			).insert()
			if submit:
				doc.submit()
		return doc.name

	def cancel_activity(self, name: str) -> None:
		from onerc_healthcare.community.activities import cancel_activity

		with as_user(self.users["mic"]):
			cancel_activity(name, "T- entered twice")

	# ---------------------------------------------------------------------------------- registration

	def merge(self, source: str, target: str) -> dict:
		from onerc_healthcare.registration.api import merge_patients

		with as_user(self.users["his"]):
			return merge_patients(
				self.patients[source], self.patients[target], "T- same person registered twice"
			)


def march(day: int) -> date:
	return date(2025, 3, day)


__all__ = ["ReconBuilder", "at", "cv", "march", "referral_facility"]
