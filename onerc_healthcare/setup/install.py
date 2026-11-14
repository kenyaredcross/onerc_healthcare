# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""after_install / after_migrate entry points.

Seeders are idempotent: create what is missing, never overwrite what an admin has edited,
never commit (Frappe commits at the end of install/migrate).
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from onerc_healthcare.constants import ALL_ROLES, DEPARTMENTS, NO_DESK_ROLES, USER_DESIGNATION_FIELD
from onerc_healthcare.setup.lab import seed_lab
from onerc_healthcare.setup.operations import facility_unit_name, get_operation, get_operation_config
from onerc_healthcare.setup.wizard import ensure_setup_complete

# Billing-free Healthcare Settings (BRD C-5). Applied on first install only, so an admin's later
# edits survive migrate. lab_test_approval_required stays off until OI-9 is decided.
HEALTHCARE_SETTINGS = {
	"patient_name_by": "Naming Series",
	"link_customer_to_patient": 0,
	"collect_registration_fee": 0,
	"show_payment_popup": 0,
	"enable_free_follow_ups": 0,
	"process_service_request_only_if_paid": 0,
	"create_lab_test_on_si_submit": 0,
	"create_observation_on_si_submit": 0,
	"allow_discharge_despite_unbilled_services": 1,
	"automatically_generate_billable": 0,
	"do_not_bill_inpatient_encounters": 1,
	"send_registration_msg": 0,
	"send_appointment_confirmation": 0,
	"send_appointment_reminder": 0,
	"collect_payment": 0,
}
# Link fields set only if the Code Value exists (Marley names them "<value>-<system>").
HEALTHCARE_SETTINGS_LINKS = {
	"default_intent": ("Code Value", "Order-Intent"),
	"default_priority": ("Code Value", "Routine-Priority"),
}

HSU_CUSTOM_FIELDS = {
	"Healthcare Service Unit": [
		{
			"fieldname": "krcs_facility_code",
			"label": "Facility Code",
			"fieldtype": "Data",
			"insert_after": "healthcare_service_unit_name",
			"description": "Internal provisional code; MFR code pending OI-3",
		},
		{
			"fieldname": "krcs_county",
			"label": "County",
			"fieldtype": "Data",
			"insert_after": "krcs_facility_code",
		},
		{
			"fieldname": "krcs_sub_county",
			"label": "Sub-County",
			"fieldtype": "Data",
			"insert_after": "krcs_county",
		},
	]
}


# Default key recording which operation's first-time setup (wizard, seeding, billing-free settings)
# has run on this site. Lets a site installed before `krcs_operation` was set finish on migrate.
INITIAL_SETUP_KEY = "krcs_initial_setup"


def after_install() -> None:
	op = get_operation()
	if op:
		initial_setup(op)
	else:
		print(
			"onerc_healthcare: site config krcs_operation is not set (kalobeyei | dadaab); "
			"skipping operation setup. Set it and run `bench migrate` to complete it."
		)
	ensure_roles()
	_install_workstreams()


def after_migrate() -> None:
	op = get_operation()
	if op:
		if frappe.db.get_default(INITIAL_SETUP_KEY) != op:
			initial_setup(op)
		else:
			seed_operation(op)
	ensure_roles()
	_install_workstreams()


# Phase 3: the "compiled by" line of MOH reports needs the user's job title.
USER_CUSTOM_FIELDS = {
	"User": [
		{
			"fieldname": USER_DESIGNATION_FIELD,
			"label": "Designation (KRCS)",
			"fieldtype": "Data",
			"insert_after": "last_name",
			"description": "Job title printed on MOH reports this user compiles, e.g. HIS Officer.",
		}
	]
}


def initial_setup(op: str) -> None:
	"""First-time setup for the site's operation: wizard, seeding and billing-free settings (C-5)."""
	ensure_setup_complete(op)
	company = get_operation_config(op)["company_name"]
	if not frappe.db.exists("Company", company):
		frappe.throw(
			f"Setup is complete but company {company!r} for operation {op!r} does not exist. "
			"Check site config krcs_operation, or create the company, then run `bench migrate`."
		)
	seed_operation(op)
	apply_healthcare_settings()
	frappe.db.set_default(INITIAL_SETUP_KEY, op)


def ensure_roles() -> None:
	"""Create the SDD §6.1 roles (desk access on). Permissions and role settings are owned by A3."""
	for role in ALL_ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc(
				{"doctype": "Role", "role_name": role, "desk_access": 0 if role in NO_DESK_ROLES else 1}
			).insert(ignore_permissions=True)


def seed_operation(op: str) -> None:
	cfg = get_operation_config(op)
	create_custom_fields(HSU_CUSTOM_FIELDS, ignore_validate=True)
	_seed_facilities(cfg)
	_seed_departments()
	_seed_inpatient_unit_types()
	seed_lab(cfg)


def _seed_facilities(cfg: dict) -> None:
	abbr, company = cfg["abbr"], cfg["company_name"]
	root = f"All Healthcare Service Units - {abbr}"
	if not frappe.db.exists("Healthcare Service Unit", root):
		# Marley's Company after_insert normally creates this; be safe if it did not run.
		frappe.get_doc(
			{
				"doctype": "Healthcare Service Unit",
				"healthcare_service_unit_name": "All Healthcare Service Units",
				"is_group": 1,
				"company": company,
			}
		).insert(ignore_permissions=True)
	for facility in cfg["facilities"]:
		name = facility_unit_name(facility, abbr)
		if frappe.db.exists("Healthcare Service Unit", name):
			continue
		frappe.get_doc(
			{
				"doctype": "Healthcare Service Unit",
				"healthcare_service_unit_name": facility["name"],
				"parent_healthcare_service_unit": root,
				"company": company,
				"is_group": 1,
				"krcs_facility_code": facility["code"],
				"krcs_county": cfg["county"],
			}
		).insert(ignore_permissions=True)


# Inpatient unit types (ORG-25). Non-billable (C-5); Marley requires appointments or occupancy, and
# Ward/Bed units hold inpatients. Wards and beds themselves are created per facility in Wave 2 (OI-18).
INPATIENT_UNIT_TYPES = ("Ward", "Bed")


def _seed_inpatient_unit_types() -> None:
	for unit_type in INPATIENT_UNIT_TYPES:
		if not frappe.db.exists("Healthcare Service Unit Type", unit_type):
			frappe.get_doc(
				{
					"doctype": "Healthcare Service Unit Type",
					"service_unit_type": unit_type,
					"inpatient_occupancy": 1,
					"allow_appointments": 0,
					"is_billable": 0,
				}
			).insert(ignore_permissions=True)


def _seed_departments() -> None:
	for dept in DEPARTMENTS:
		if not frappe.db.exists("Medical Department", dept):
			frappe.get_doc({"doctype": "Medical Department", "department": dept}).insert(
				ignore_permissions=True
			)


def apply_healthcare_settings() -> None:
	settings = frappe.get_single("Healthcare Settings")
	settings.update(HEALTHCARE_SETTINGS)
	for field, (doctype, name) in HEALTHCARE_SETTINGS_LINKS.items():
		if frappe.db.exists(doctype, name):
			settings.set(field, name)
	settings.flags.ignore_mandatory = True
	settings.save(ignore_permissions=True)


def _install_workstreams() -> None:
	from onerc_healthcare import desk_sidebar
	from onerc_healthcare.clinical import setup as clinical_setup
	from onerc_healthcare.coding import setup as coding_setup
	from onerc_healthcare.frontdesk import setup as frontdesk_setup
	from onerc_healthcare.lab import setup as lab_setup
	from onerc_healthcare.naming import ensure_naming_settings
	from onerc_healthcare.pharmacy import setup as pharmacy_setup
	from onerc_healthcare.privacy import setup as privacy_setup
	from onerc_healthcare.registration import setup as registration_setup
	from onerc_healthcare.security import setup as security_setup
	from onerc_healthcare.visit import setup as visit_setup

	registration_setup.install()
	visit_setup.install()
	# Phase 2 workstreams (B1 clinical, B2 lab, B3 pharmacy + front desk, B4 privacy)
	clinical_setup.install()
	lab_setup.install()
	pharmacy_setup.install()
	frontdesk_setup.install()
	privacy_setup.install()
	# Security last among the workstreams: it applies perms/archive fields over everything that exists.
	security_setup.install()
	coding_setup.install()
	# Phase 3 (R1 framework, R2 705 mapping, R4 workload definitions, R3 community masters). The report
	# definitions need R1's DocTypes, so the framework goes first.
	create_custom_fields(USER_CUSTOM_FIELDS, ignore_validate=True)
	from onerc_healthcare.community import setup as community_setup
	from onerc_healthcare.reporting import mapping as reporting_mapping
	from onerc_healthcare.reporting import setup as reporting_setup
	from onerc_healthcare.reporting import workload as reporting_workload

	reporting_setup.install()
	reporting_mapping.install()
	reporting_workload.install()
	community_setup.install()
	ensure_naming_settings()
	desk_sidebar.install()
