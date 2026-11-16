# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Phase 5M test factories (W0; builders may add helpers at the end, never change these signatures).

users = maternity_users()        # {"midwife", "clinician", "mic", "his", "reception", "mho"}
mother = make_maternity_patient()                  # a T- woman of 25 with a UNHCR ID
adm = make_admission(mother.name)                  # Maternity Admission, inserted directly (status Admitted)
bed = make_bed(ward_for_facility(get_facility()))  # a T- Bed under the facility's Maternity Ward

`make_admission` bypasses `maternity.admission.admit` (M1): no visit is closed and no bed is occupied. It is
for the partograph and delivery tests until M1's API exists; admission tests use the API.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_years, now_datetime, random_string, today

from onerc_healthcare.constants import (
	ADMISSION_ADMITTED,
	MATERNITY_ADMISSION_DOCTYPE,
	ROLE_CLINICIAN,
	ROLE_HIS,
	ROLE_MHO,
	ROLE_MIC,
	ROLE_MIDWIFE,
	ROLE_RECEPTIONIST,
)
from onerc_healthcare.tests.factories import get_company_name, get_facility, make_patient, make_user

MATERNITY_USERS = {
	"midwife": (ROLE_MIDWIFE,),
	"clinician": (ROLE_CLINICIAN,),
	"mic": (ROLE_MIC,),
	"his": (ROLE_HIS,),
	"reception": (ROLE_RECEPTIONIST,),
	"mho": (ROLE_MHO,),
}


def maternity_users() -> dict[str, str]:
	"""One user per key: t-mat-<key>@test.krcs with exactly the listed roles."""
	return {key: make_user(f"mat-{key}", list(roles)) for key, roles in MATERNITY_USERS.items()}


def make_maternity_patient(**kw):
	"""Insert a T- woman of 25 (UNHCR ID row added by `make_patient`)."""
	values = {"sex": "Female", "dob": add_years(today(), -25), "last_name": f"T-Mother{random_string(4)}"}
	values.update(kw)
	return make_patient(**values)


def make_admission(
	patient: str | None = None,
	facility: str | None = None,
	ward: str | None = None,
	bed: str | None = None,
	status: str = ADMISSION_ADMITTED,
	**kw,
):
	"""Insert a Maternity Admission directly (ignore_permissions, ``flags.from_factory``) for a new mother
	unless `patient` is given. The status log gets one "admit" row. No visit is closed, no bed is occupied."""
	from onerc_healthcare.maternity.setup import ward_for_facility

	facility = facility or get_facility()
	now = now_datetime()
	values = {
		"doctype": MATERNITY_ADMISSION_DOCTYPE,
		"patient": patient or make_maternity_patient().name,
		"facility": facility,
		"ward": ward or ward_for_facility(facility),
		"bed": bed,
		"status": status,
		"admitted_at": now,
		"admitted_by": frappe.session.user,
		"status_log": [
			{
				"from_status": None,
				"to_status": status,
				"action": "admit",
				"user": frappe.session.user,
				"timestamp": now,
				"comment": "T- factory",
			}
		],
	}
	values.update(kw)
	doc = frappe.get_doc(values)
	doc.flags.from_factory = True
	doc.insert(ignore_permissions=True)
	return doc


def make_bed(ward: str, name: str | None = None) -> str:
	"""Insert a T- Bed (Healthcare Service Unit, type Bed, Vacant) under `ward`. Returns its name."""
	doc = frappe.get_doc(
		{
			"doctype": "Healthcare Service Unit",
			"healthcare_service_unit_name": name or f"T-Bed-{random_string(6)}",
			"parent_healthcare_service_unit": ward,
			"company": frappe.db.get_value("Healthcare Service Unit", ward, "company") or get_company_name(),
			"is_group": 0,
			"service_unit_type": "Bed",
		}
	)
	doc.insert(ignore_permissions=True)
	if doc.occupancy_status != "Vacant":
		frappe.db.set_value("Healthcare Service Unit", doc.name, "occupancy_status", "Vacant")
	return doc.name
