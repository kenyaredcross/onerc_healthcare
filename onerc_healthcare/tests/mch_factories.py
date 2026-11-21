# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""MCH test factories (W0; builders may add helpers at the end, never change these signatures).

users = mch_users()                      # {"clinician", "midwife", "mic", "triage", "mho", "his", "reception"}
mother = make_mother()                   # a T- woman of 25 with a UNHCR ID
child = make_child(age_days=120)         # a T- child born 120 days ago (exact DOB unless dob_estimated=1)
visit = make_mch_visit(child.name)       # an open Patient Visit (Registered) for the patient
preg = make_pregnancy(mother.name)       # an open ANC Pregnancy, inserted directly
rec = make_record(GROWTH_MONITORING_DOCTYPE, child.name, weight_kg=6.1)  # a draft record on a new open visit

Never commit.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, add_years, random_string, today

from onerc_healthcare.constants import (
	ANC_PREGNANCY_DOCTYPE,
	PREGNANCY_OPEN,
	ROLE_CLINICIAN,
	ROLE_HIS,
	ROLE_MHO,
	ROLE_MIC,
	ROLE_MIDWIFE,
	ROLE_RECEPTIONIST,
	ROLE_TRIAGE,
)
from onerc_healthcare.tests.factories import get_facility, make_patient, make_user
from onerc_healthcare.tests.visit_factories import make_visit

MCH_USERS = {
	"clinician": (ROLE_CLINICIAN,),
	"midwife": (ROLE_MIDWIFE,),
	"mic": (ROLE_MIC,),
	"triage": (ROLE_TRIAGE,),
	"mho": (ROLE_MHO,),
	"his": (ROLE_HIS,),
	"reception": (ROLE_RECEPTIONIST,),
}


def mch_users() -> dict[str, str]:
	"""One user per key: t-mch-<key>@test.krcs with exactly the listed roles."""
	return {key: make_user(f"mch-{key}", list(roles)) for key, roles in MCH_USERS.items()}


def make_mother(**kw):
	"""Insert a T- woman of 25 (UNHCR ID row added by `make_patient`)."""
	values = {"sex": "Female", "dob": add_years(today(), -25), "last_name": f"T-Mother{random_string(4)}"}
	values.update(kw)
	return make_patient(**values)


def make_child(age_days: int = 120, sex: str = "Male", dob_estimated: int = 0, **kw):
	"""Insert a T- child born `age_days` ago. `dob_estimated=1` marks the date of birth as estimated
	(`krcs_dob_estimated`, written directly after insert so the registration rules keep the exact dob)."""
	values = {"sex": sex, "dob": add_days(today(), -age_days), "last_name": f"T-Child{random_string(4)}"}
	values.update(kw)
	doc = make_patient(**values)
	if dob_estimated:
		frappe.db.set_value("Patient", doc.name, "krcs_dob_estimated", 1, update_modified=False)
		doc.reload()
	return doc


def make_mch_visit(patient: str, facility: str | None = None, **kw):
	"""An open (Registered) Patient Visit for `patient` (inserted with ignore_permissions)."""
	return make_visit(patient=patient, facility=facility or get_facility(0), **kw)


def make_pregnancy(patient: str | None = None, **kw):
	"""Insert an open ANC Pregnancy directly (ignore_permissions, ``flags.from_factory``) for a new mother unless
	`patient` is given. LMP 20 weeks ago unless given."""
	values = {
		"doctype": ANC_PREGNANCY_DOCTYPE,
		"patient": patient or make_mother().name,
		"facility": get_facility(0),
		"status": PREGNANCY_OPEN,
		"booking_date": today(),
		"lmp": add_days(today(), -140),
		"gravida": 2,
		"para": "1",
	}
	values.update(kw)
	doc = frappe.get_doc(values)
	doc.flags.from_factory = True
	doc.insert(ignore_permissions=True)
	return doc


def make_record(doctype: str, patient: str, visit: str | None = None, submit: bool = False, **kw):
	"""Insert a draft MCH record (`MCH_RECORD_DOCTYPES`) for `patient` on `visit` (default: a new open visit),
	with ignore_permissions; `submit=True` submits it too. Builders' validation runs as usual."""
	values = {"doctype": doctype, "patient": patient, "visit": visit or make_mch_visit(patient).name}
	values.update(kw)
	doc = frappe.get_doc(values)
	doc.insert(ignore_permissions=True)
	if submit:
		doc.submit()
	return doc
