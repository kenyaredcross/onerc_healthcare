# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Shared constants: the W0 contract. Workstreams import from here and never edit it.

Never hard-code a role name, status string or department name elsewhere in the app.
"""

# Operations (BRD BR-8 / C-7, SDD v0.1 §3.2): one Frappe site per operation.
# A site's operation comes from site config `krcs_operation`.
OPERATION_KALOBEYEI = "kalobeyei"
OPERATION_DADAAB = "dadaab"
OPERATIONS = (OPERATION_KALOBEYEI, OPERATION_DADAAB)

# Roles (SDD §6.1)
ROLE_RECEPTIONIST = "Nurse Receptionist"
ROLE_TRIAGE = "Triage Nurse"
ROLE_CLINICIAN = "Clinical Officer"  # also covers Medical Officer
ROLE_MHO = "Mental Health Officer"
ROLE_LAB = "Laboratory Technician"
ROLE_PHARMACIST = "Pharmacist"
ROLE_HIS = "HIS Officer"
ROLE_MIC = "Medical In-Charge"
ROLE_HEAD_OPS = "Head of Operations"
ROLE_SYSADMIN = "EMR System Administrator"
ROLE_OUTREACH = "Outreach Team Member"

ALL_ROLES = (
	ROLE_RECEPTIONIST,
	ROLE_TRIAGE,
	ROLE_CLINICIAN,
	ROLE_MHO,
	ROLE_LAB,
	ROLE_PHARMACIST,
	ROLE_HIS,
	ROLE_MIC,
	ROLE_HEAD_OPS,
	ROLE_SYSADMIN,
	ROLE_OUTREACH,
)

# Roles that handle patient-level clinical content.
CLINICAL_ROLES = (
	ROLE_RECEPTIONIST,
	ROLE_TRIAGE,
	ROLE_CLINICIAN,
	ROLE_MHO,
	ROLE_LAB,
	ROLE_PHARMACIST,
	ROLE_MIC,
	ROLE_OUTREACH,
)

# Patient Visit statuses (FR-7)
VISIT_STATUS_REGISTERED = "Registered"
VISIT_STATUS_IN_TRIAGE = "In Triage"
VISIT_STATUS_ROUTED = "Routed to Department"
VISIT_STATUS_IN_CONSULTATION = "In Consultation"
VISIT_STATUS_AWAITING_LAB = "Awaiting Laboratory"
VISIT_STATUS_AWAITING_PHARMACY = "Awaiting Pharmacy"
VISIT_STATUS_DISCHARGED = "Discharged"
VISIT_STATUS_REFERRED = "Referred"

VISIT_STATUSES = (
	VISIT_STATUS_REGISTERED,
	VISIT_STATUS_IN_TRIAGE,
	VISIT_STATUS_ROUTED,
	VISIT_STATUS_IN_CONSULTATION,
	VISIT_STATUS_AWAITING_LAB,
	VISIT_STATUS_AWAITING_PHARMACY,
	VISIT_STATUS_DISCHARGED,
	VISIT_STATUS_REFERRED,
)
VISIT_CLOSED_STATUSES = (VISIT_STATUS_DISCHARGED, VISIT_STATUS_REFERRED)
VISIT_OPEN_STATUSES = tuple(s for s in VISIT_STATUSES if s not in VISIT_CLOSED_STATUSES)

# Medical Departments (BR-3)
DEPT_RECEPTION = "Reception"
DEPT_TRIAGE = "Triage"
DEPT_OUTPATIENT = "Outpatient"
DEPT_MCH = "Maternal and Child Health"
DEPT_MENTAL_HEALTH = "Mental Health"
DEPT_NUTRITION = "Nutrition"
DEPT_LABORATORY = "Laboratory"
DEPT_PHARMACY = "Pharmacy"
DEPT_HIS = "Health Information"

DEPARTMENTS = (
	DEPT_RECEPTION,
	DEPT_TRIAGE,
	DEPT_OUTPATIENT,
	DEPT_MCH,
	DEPT_MENTAL_HEALTH,
	DEPT_NUTRITION,
	DEPT_LABORATORY,
	DEPT_PHARMACY,
	DEPT_HIS,
)

# Patient identifier types (FR-1 to FR-3, C-6). Primary types satisfy the
# "at least one primary identifier unless walk-in" rule.
IDENTIFIER_UNHCR = "UNHCR ID"
IDENTIFIER_POL = "Proof of Life Number"
IDENTIFIER_ALIEN = "Alien ID"
IDENTIFIER_NATIONAL = "National ID"
IDENTIFIER_FACILITY = "Facility-generated"
IDENTIFIER_RATION = "Ration Card Number"

IDENTIFIER_TYPES = (
	{"name": IDENTIFIER_UNHCR, "is_primary": 1},
	{"name": IDENTIFIER_POL, "is_primary": 1},
	{"name": IDENTIFIER_ALIEN, "is_primary": 1},
	{"name": IDENTIFIER_NATIONAL, "is_primary": 0},
	{"name": IDENTIFIER_FACILITY, "is_primary": 0},
	{"name": IDENTIFIER_RATION, "is_primary": 0},
)
PRIMARY_IDENTIFIER_TYPES = tuple(t["name"] for t in IDENTIFIER_TYPES if t["is_primary"])

# Patient field contract (A1 creates these; others may read them)
PATIENT_IDENTIFIERS_FIELD = "krcs_identifiers"  # Table -> Patient Identifier
PATIENT_WALK_IN_FIELD = "krcs_is_walk_in"
PATIENT_IDENTIFIER_DOCTYPE = "Patient Identifier"
PATIENT_IDENTIFIER_TYPE_DOCTYPE = "Patient Identifier Type"

# DocTypes whose records may never be deleted (NFR-9). Patient Medical Record is
# excluded on purpose: Marley deletes those rows internally on cancel.
CLINICAL_DOCTYPES = (
	"Patient",
	"Patient Encounter",
	"Vital Signs",
	"Observation",
	"Sample Collection",
	"Diagnostic Report",
	"Lab Test",
	"Medication Request",
	"Service Request",
	"Inpatient Record",
	"Clinical Procedure",
	"Clinical Note",
	"Medication Administration",
	"Discharge Summary",
	"Patient Visit",
)

VISIT_DOCTYPE = "Patient Visit"
