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
# Phase 3: API-only reader of MOH aggregates (no desk access, no users until the DHIS2 link, OI-5).
ROLE_MOH_API = "MOH Data Reader"

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
	ROLE_MOH_API,
)
# Roles created without desk access (API only).
NO_DESK_ROLES = (ROLE_MOH_API,)

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
	# Phase 2 (W0 contract)
	"Referral",
	"Dispensing Record",
	"Patient Consent",
	"Data Access Request",
	"Screening Record",
	"Visitor Record",
	"Access Log Entry",
	# Phase 3 (W0 contract)
	"MOH Report Run",
	"Service Delivered",
	"Community Activity Record",
)

VISIT_DOCTYPE = "Patient Visit"

# ---------------------------------------------------------------------------
# Phase 2 contract (W0). Builders import these and never edit them.
# ---------------------------------------------------------------------------
ENCOUNTER_DOCTYPE = "Patient Encounter"
ENCOUNTER_DIAGNOSIS_DOCTYPE = "EMR Encounter Diagnosis"  # B1 child table
REFERRAL_DOCTYPE = "Referral"  # B1
REFERRAL_FACILITY_DOCTYPE = "Referral Facility"  # B1 (provisional until OI-3)
DISPENSING_DOCTYPE = "Dispensing Record"  # B3
SCREENING_DOCTYPE = "Screening Record"  # B3
SCREENING_PARAMETER_DOCTYPE = "Screening Parameter"  # B3
VISITOR_DOCTYPE = "Visitor Record"  # B3
CONSENT_DOCTYPE = "Patient Consent"  # B4
CONSENT_STATEMENT_DOCTYPE = "Consent Statement"  # B4
DATA_ACCESS_REQUEST_DOCTYPE = "Data Access Request"  # B4
ACCESS_LOG_DOCTYPE = "Access Log Entry"  # B4

# Appointment Type seeded by B1 so Marley's mandatory Patient Encounter.appointment_type is satisfied.
DEFAULT_APPOINTMENT_TYPE = "KRCS Consultation"

# Encounter <-> visit link fields
ENCOUNTER_VISIT_FIELD = "krcs_visit"  # Patient Encounter -> Patient Visit (B1 creates it)
VISIT_ENCOUNTER_FIELD = "encounter"  # Patient Visit -> Patient Encounter (W0)

# Close Visit (decision 2026-11-13): reason categories
VISIT_CLOSURE_REASONS = (
	"Left before being seen",
	"Left before completion",
	"Superseded by a new visit",
	"Registered in error",
	"Died",
	"Other",
)

# Records whose desk opens are written to the Access Log Entry (FR-40, NFR-7; B4)
ACCESS_LOGGED_DOCTYPES = (
	"Patient",
	"Patient Encounter",
	"Vital Signs",
	"Observation",
	"Diagnostic Report",
	"Medication Request",
	"Service Request",
	"Patient Visit",
	"Referral",
	"Dispensing Record",
	"Patient Consent",
	"Data Access Request",
	"Screening Record",
	"Service Delivered",  # Phase 3
)

# Modules of this app (our own DocTypes live in these; security setup leaves their JSON perms alone)
APP_MODULES = (
	"Onerc Healthcare",
	"EMR Registration",
	"EMR Visit",
	"EMR Security",
	"EMR Clinical",
	"EMR Laboratory",
	"EMR Pharmacy",
	"EMR Front Desk",
	"EMR Data Protection",
	"EMR Reporting",
	"EMR Community",
)

# Script/query reports the Phase 2 builders create (exact names; the sidebar links them)
REPORT_LAB_WORKLIST = "Laboratory Worklist"  # B2 (FR-16)
REPORT_PHARMACY_WORKLIST = "Pharmacy Worklist"  # B3 (FR-20)
REPORT_DAILY_ENTRY = "Daily Facility Entry"  # B3 (BR-13)
REPORT_CONSENT_EXCEPTIONS = "Consent Exceptions"  # B4 (FR-37)
REPORT_ACCESS_AUDIT = "Access Audit"  # B4 (FR-40)

# ---------------------------------------------------------------------------
# Phase 3 contract (W0): MOH reporting and data-capture gaps. Builders import these and never edit them.
# ---------------------------------------------------------------------------
# R1 framework
MOH_REPORT_DOCTYPE = "MOH Report"  # definition (name = report code, e.g. "MOH 705A")
MOH_DATA_ELEMENT_DOCTYPE = "MOH Report Data Element"  # child of MOH Report
MOH_AGE_BAND_DOCTYPE = "MOH Report Age Band"  # child of MOH Report
MOH_RUN_DOCTYPE = "MOH Report Run"  # one generated report for a facility and period
MOH_RUN_LOG_DOCTYPE = "MOH Report Run Log"  # child of MOH Report Run (append-only)
# R2 diagnosis mapping
MOH_CATEGORY_DOCTYPE = "MOH Disease Category"
MOH_MAPPING_DOCTYPE = "MOH Diagnosis Mapping"
ICD11_BLOCK_DOCTYPE = "ICD-11 Block"
# R3 capture
SERVICE_DELIVERED_DOCTYPE = "Service Delivered"
SERVICE_TYPE_DOCTYPE = "Visit Service Type"
COMMUNITY_ACTIVITY_DOCTYPE = "Community Activity Record"
COMMUNITY_ACTIVITY_TYPE_DOCTYPE = "Community Activity Type"

# Report definitions (MOH Report names). Provisional until the official forms arrive (OI-5).
MOH_705A = "MOH 705A"  # outpatient morbidity, under 5
MOH_705B = "MOH 705B"  # outpatient morbidity, 5 and over
MOH_717 = "MOH 717"  # facility workload
MOH_711_REHAB = "MOH 711 Rehabilitation and Social Work"
# Blocked: definitions exist without data elements until OI-5 / OI-24.
MOH_711_RH = "MOH 711 RH-MCH"
MOH_747A = "MOH 747A"
MOH_643B = "MOH 643B"
MH_CLINIC = "Mental Health Clinic Report"
MOH_REPORT_STATUSES = ("Active - Provisional", "Confirmed", "Blocked")

# Script reports (exact names; the sidebar links them)
REPORT_MOH_UNMAPPED = "MOH Unmapped Diagnoses"  # R2
REPORT_MOH_COVERAGE = "MOH Mapping Coverage"  # R2

# Visit outcome (Patient Visit.outcome, set by the engine on the closing action)
OUTCOME_DISCHARGED = "Discharged home"
OUTCOME_ADMITTED = "Admitted"
OUTCOME_REFERRED_OUT = "Referred out"
OUTCOME_REFERRED_INTERNAL = "Referred internally"
OUTCOME_DIED = "Died"
OUTCOME_LEFT = "Left without completing"
OUTCOME_NOT_APPLICABLE = "Not applicable"
VISIT_OUTCOMES = (
	OUTCOME_DISCHARGED,
	OUTCOME_ADMITTED,
	OUTCOME_REFERRED_OUT,
	OUTCOME_REFERRED_INTERNAL,
	OUTCOME_DIED,
	OUTCOME_LEFT,
	OUTCOME_NOT_APPLICABLE,
)
# Roles that may record "Died" (a clinical judgement); the system may too.
DIED_ROLES = (ROLE_CLINICIAN, ROLE_MHO, ROLE_MIC)

# Attendance (MOH 717 "new" vs "re-attendance"): first visit to this facility in the calendar year.
ATTENDANCE_NEW = "New"
ATTENDANCE_REVISIT = "Re-attendance"
ATTENDANCE_TYPES = (ATTENDANCE_NEW, ATTENDANCE_REVISIT)
ATTENDANCE_BASIS_RULE = "Calendar-year rule"
ATTENDANCE_BASIS_RECEPTION = "Reception override"
ATTENDANCE_BASIS_HIS = "HIS correction"
ATTENDANCE_BASES = (ATTENDANCE_BASIS_RULE, ATTENDANCE_BASIS_RECEPTION, ATTENDANCE_BASIS_HIS)

# Patient Visit fields (W0 creates them; engine-protected after insert)
VISIT_ATTENDANCE_TYPE_FIELD = "attendance_type"
VISIT_ATTENDANCE_BASIS_FIELD = "attendance_basis"
VISIT_SEEN_IN_SMARTCARE_FIELD = "seen_in_smartcare"
VISIT_ATTENDANCE_NOTE_FIELD = "attendance_note"
VISIT_OUTCOME_FIELD = "outcome"
VISIT_REFERRED_IN_FIELD = "referred_in"
VISIT_REFERRED_IN_FROM_FIELD = "referred_in_from"

# Patient DOB estimate (R3 creates these custom fields). Convention: when only an age is known, Patient.dob
# = estimate date minus the age (years and months, same day of month) and the record is flagged.
PATIENT_DOB_ESTIMATED_FIELD = "krcs_dob_estimated"  # Check
PATIENT_ESTIMATED_AGE_YEARS_FIELD = "krcs_estimated_age_years"  # Int, as told at registration
PATIENT_ESTIMATED_AGE_MONTHS_FIELD = "krcs_estimated_age_months"  # Int 0-11
PATIENT_DOB_ESTIMATED_ON_FIELD = "krcs_dob_estimated_on"  # Date the age was given

# User field for the "compiled by" line of MOH reports (W0 creates it)
USER_DESIGNATION_FIELD = "krcs_designation"

# Visit closure reasons that remove the visit from every MOH count (not a real attendance)
REPORT_EXCLUDED_CLOSURE_REASONS = ("Registered in error", "Superseded by a new visit")
