# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Demo users, patients and visits for hands-on testing of a DEV site. Never run on a live site.

Every person, name and identifier here is FICTIONAL. Emails use the reserved ``.test`` domain, so no mail
can be delivered. Clinical records cannot be deleted (NFR-9), so what this creates stays on the site.

    bench --site kalobeyei.localhost execute onerc_healthcare.setup.demo.seed_demo \
        --kwargs '{"site": "kalobeyei.localhost"}'

Re-running is safe: users and patients that already exist (by email / identifier) are reused, and a
patient that already has an open visit gets no new one. Every step runs as the role user who would do it
in real life, through the normal permission checks and the visit engine.
"""

import frappe
from frappe.utils import add_days, add_years, nowdate, nowtime

from onerc_healthcare.constants import (
	IDENTIFIER_ALIEN,
	IDENTIFIER_NATIONAL,
	IDENTIFIER_POL,
	IDENTIFIER_UNHCR,
	ROLE_CLINICIAN,
	ROLE_HEAD_OPS,
	ROLE_HIS,
	ROLE_LAB,
	ROLE_MHO,
	ROLE_MIC,
	ROLE_PHARMACIST,
	ROLE_RECEPTIONIST,
	ROLE_SYSADMIN,
	ROLE_TRIAGE,
	VISIT_OPEN_STATUSES,
)
from onerc_healthcare.setup.operations import get_facilities, get_operation

DEMO_PASSWORD = "Kalobeyei-Demo#2026"
DOMAIN = "krcs-demo.test"

# (key, first, last, role). HIS Officer and EMR System Administrator have 2FA (D16): they need an
# authenticator app on first login.
STAFF = (
	("reception", "Achieng", "Otieno", ROLE_RECEPTIONIST),
	("triage", "Ekai", "Lokwang", ROLE_TRIAGE),
	("clinician", "Wanjiru", "Kamau", ROLE_CLINICIAN),
	("mho", "Moraa", "Nyaboke", ROLE_MHO),
	("lab", "Lomuria", "Ekiru", ROLE_LAB),
	("pharmacist", "Peter", "Ngugi", ROLE_PHARMACIST),
	("mic", "Esther", "Nakiru", ROLE_MIC),
	("headops", "John", "Mutua", ROLE_HEAD_OPS),
	("his", "Grace", "Wambui", ROLE_HIS),
	("sysadmin", "Samuel", "Kiprono", ROLE_SYSADMIN),
)

# Patients: fictional names typical of the Kalobeyei population (South Sudanese, Congolese, Burundian
# refugees and Turkana host community). `stage` is how far the demo visit goes:
#   registered | triage | routed:<dept> | consultation:<dept> | lab:<dept> | none
PATIENTS = (
	{
		"first": "Nyakuoth",
		"last": "Gatluak",
		"sex": "Female",
		"age": 27,
		"ids": [(IDENTIFIER_UNHCR, "KBY-24C01187")],
		"facility": 0,
		"stage": "registered",
	},
	{
		"first": "Deng",
		"last": "Majok",
		"sex": "Male",
		"age": 41,
		"ids": [(IDENTIFIER_UNHCR, "KBY-23A04512"), (IDENTIFIER_POL, "POL-558213")],
		"facility": 0,
		"stage": "triage",
		"complaint": "Fever and joint pains for three days",
		"level": "Urgent",
		"vitals": ("38.6", "104", "22", "126", "82"),
	},
	{
		"first": "Akuol",
		"last": "Garang",
		"sex": "Female",
		"age": 3,
		"ids": [(IDENTIFIER_UNHCR, "KBY-25B00931")],
		"facility": 0,
		"stage": "routed:Maternal and Child Health",
		"complaint": "Diarrhoea since yesterday, poor feeding",
		"level": "Urgent",
		"vitals": ("37.9", "128", "32", "", ""),
	},
	{
		"first": "Amani",
		"last": "Bahati",
		"sex": "Female",
		"age": 34,
		"ids": [(IDENTIFIER_UNHCR, "KBY-22D07745")],
		"facility": 0,
		"stage": "routed:Outpatient",
		"complaint": "Cough and chest pain for one week",
		"level": "Non-urgent",
		"vitals": ("37.4", "88", "20", "118", "76"),
	},
	{
		"first": "Jean",
		"last": "Ndayishimiye",
		"sex": "Male",
		"age": 52,
		"ids": [(IDENTIFIER_ALIEN, "AL-0049921")],
		"facility": 0,
		"stage": "consultation:Outpatient",
		"complaint": "Headache and dizziness, known hypertensive",
		"level": "Non-urgent",
		"vitals": ("36.9", "80", "18", "164", "98"),
	},
	{
		"first": "Achol",
		"last": "Mabior",
		"sex": "Female",
		"age": 23,
		"ids": [(IDENTIFIER_UNHCR, "KBY-24A02260")],
		"facility": 0,
		"stage": "routed:Mental Health",
		"complaint": "Poor sleep and low mood for two months",
		"level": "Non-urgent",
		"vitals": ("36.7", "76", "16", "112", "70"),
	},
	{
		"first": "Lokol",
		"last": "Ekai",
		"sex": "Male",
		"age": 45,
		"walk_in": True,
		"ids": [(IDENTIFIER_NATIONAL, "28411907")],
		"facility": 1,
		"stage": "lab:Outpatient",
		"complaint": "Fever and vomiting, suspected malaria",
		"level": "Urgent",
		"vitals": ("39.1", "110", "24", "110", "68"),
	},
	{
		"first": "Mary",
		"last": "Akuru",
		"sex": "Female",
		"age": 30,
		"walk_in": True,
		"ids": [],
		"facility": 1,
		"stage": "registered",
	},
	{
		"first": "Gatwech",
		"last": "Kuol",
		"sex": "Male",
		"age": 8,
		"ids": [(IDENTIFIER_UNHCR, "KBY-25C00402")],
		"facility": 1,
		"stage": "routed:Nutrition",
		"complaint": "Weight loss, referred from community screening",
		"level": "Urgent",
		"vitals": ("36.8", "102", "24", "", ""),
	},
	{
		"first": "Nyandit",
		"last": "Chuol",
		"sex": "Female",
		"age": 61,
		"ids": [(IDENTIFIER_UNHCR, "KBY-21F05518")],
		"facility": 1,
		"stage": "none",
	},
)


def seed_demo(site: str) -> dict:
	"""Create the demo staff and patients on `site` (must equal the current site, as a safety check)."""
	if site != frappe.local.site:
		frappe.throw(f"Refusing: pass site={frappe.local.site!r} to confirm this is the site to seed.")
	if not get_operation():
		frappe.throw("Site has no krcs_operation; seed a configured operation site only.")
	frappe.set_user("Administrator")
	users = {key: _ensure_user(key, first, last, role) for key, first, last, role in STAFF}
	facilities = get_facilities()
	created = []
	for spec in PATIENTS:
		patient = _ensure_patient(spec, users["reception"], facilities[spec["facility"]])
		visit = _run_visit(spec, patient, users, facilities[spec["facility"]])
		created.append(
			{
				"patient": patient,
				"name": f"{spec['first']} {spec['last']}",
				"visit": visit,
				"stage": spec["stage"],
			}
		)
	frappe.set_user("Administrator")
	frappe.db.commit()
	return {
		"password": DEMO_PASSWORD,
		"users": {frappe.db.get_value("User", u, "full_name"): u for u in users.values()},
		"patients": created,
	}


def _ensure_user(key: str, first: str, last: str, role: str) -> str:
	email = f"{first}.{last}@{DOMAIN}".lower()
	if not frappe.db.exists("User", email):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first,
				"last_name": last,
				"send_welcome_email": 0,
				"new_password": DEMO_PASSWORD,
				"roles": [{"role": role}],
			}
		)
		user.flags.no_welcome_mail = True
		user.insert(ignore_permissions=True)
	return email


def _find_patient(spec: dict) -> str | None:
	if spec["ids"]:
		itype, value = spec["ids"][0]
		return frappe.db.get_value(
			"Patient Identifier",
			{"parenttype": "Patient", "identifier_type": itype, "identifier_value": value},
			"parent",
		)
	return frappe.db.get_value("Patient", {"first_name": spec["first"], "last_name": spec["last"]}, "name")


def _ensure_patient(spec: dict, receptionist: str, facility: str) -> str:
	existing = _find_patient(spec)
	if existing:
		return existing
	frappe.set_user(receptionist)
	doc = frappe.get_doc(
		{
			"doctype": "Patient",
			"first_name": spec["first"],
			"last_name": spec["last"],
			"sex": spec["sex"],
			"dob": add_days(add_years(nowdate(), -spec["age"]), -37 * spec["age"]),
			"krcs_is_walk_in": 1 if spec.get("walk_in") else 0,
			"krcs_registration_facility": facility,
			"krcs_identifiers": [
				{
					"identifier_type": t,
					"identifier_value": v,
					"verification_status": "Verified",
					"verified_on": nowdate(),
					"verification_source": "Demo data (fictional)",
				}
				for t, v in spec["ids"]
			],
		}
	).insert()
	frappe.set_user("Administrator")
	return doc.name


def _run_visit(spec: dict, patient: str, users: dict, facility: str) -> str | None:
	from onerc_healthcare.visit.engine import transition

	if spec["stage"] == "none":
		return None
	open_visit = frappe.db.get_value(
		"Patient Visit", {"patient": patient, "status": ("in", VISIT_OPEN_STATUSES)}, "name"
	)
	if open_visit:
		return open_visit
	frappe.set_user(users["reception"])
	visit = (
		frappe.get_doc({"doctype": "Patient Visit", "patient": patient, "facility": facility}).insert().name
	)
	if spec["stage"] == "registered":
		frappe.set_user("Administrator")
		return visit

	temperature, pulse, rr, systolic, diastolic = spec["vitals"]
	frappe.set_user(users["triage"])
	vitals = frappe.get_doc(
		{
			"doctype": "Vital Signs",
			"patient": patient,
			"signs_date": nowdate(),
			"signs_time": nowtime(),
			"temperature": temperature,
			"pulse": pulse,
			"respiratory_rate": rr,
			"bp_systolic": systolic,
			"bp_diastolic": diastolic,
			"krcs_visit": visit,
			"krcs_triage_level": spec["level"],
			"krcs_chief_complaint": spec["complaint"],
		}
	).insert()
	vitals.submit()  # starts triage on the visit
	if spec["stage"] == "triage":
		frappe.set_user("Administrator")
		return visit

	stage, department = spec["stage"].split(":", 1)
	transition(visit, "route", department=department)
	if stage in ("consultation", "lab"):
		clinician = users["mho"] if department == "Mental Health" else users["clinician"]
		frappe.set_user(clinician)
		transition(visit, "start_consultation")
	if stage == "lab":
		frappe.set_user("Administrator")
		transition(visit, "request_lab", system=True, comment="Malaria RDT and full haemogram requested")
	frappe.set_user("Administrator")
	return visit
