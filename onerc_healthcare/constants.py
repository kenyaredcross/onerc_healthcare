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
# Phase 5M (ROL-24, D60): labour ward. Admits, keeps the partograph, records deliveries.
ROLE_MIDWIFE = "Midwife"

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
	ROLE_MIDWIFE,
)
# Roles created without desk access (API only).
# Phase 4: the Outreach Team Member works only in the outreach app (/outreach), never the desk (ROL-22).
NO_DESK_ROLES = (ROLE_MOH_API, ROLE_OUTREACH)

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
	ROLE_MIDWIFE,
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
DEPT_MATERNITY = "Maternity"  # Phase 5M: routable; Marley ships the Medical Department too

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
	DEPT_MATERNITY,
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
	# Phase 4 (W0 contract)
	"Outreach Session",
	"Outreach Sync Batch",
	"Outreach Sync Record",
	"Sync Conflict",
	# Phase 5M (W0 contract)
	"Maternity Admission",
	"Partograph",
	"Delivery Record",
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
	"Outreach Sync Record",  # Phase 4: holds offline clinical payloads
	"Sync Conflict",  # Phase 4: both versions of a patient's data
	"Maternity Admission",  # Phase 5M
	"Partograph",  # Phase 5M
	"Delivery Record",  # Phase 5M
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
	"EMR Outreach",
	"EMR Maternity",
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
DIED_ROLES = (ROLE_CLINICIAN, ROLE_MHO, ROLE_MIC, ROLE_MIDWIFE)  # Midwife: Phase 5M (maternal death)

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


# ---------------------------------------------------------------------------
# Phase 4 contract (W0): medical outreach (SDD §5.6, FR-23..26, NFR-11/18, DR-3). Builders import these and
# never edit them. The wire format is in docs/OUTREACH_PROTOCOL.md.
# ---------------------------------------------------------------------------
# O1 sessions, devices, package
CATCHMENT_AREA_DOCTYPE = "Catchment Area"
CATCHMENT_LOCATION_DOCTYPE = "Catchment Area Location"  # child: residence_location (subtree included)
OUTREACH_SESSION_DOCTYPE = "Outreach Session"
OUTREACH_TEAM_DOCTYPE = "Outreach Session Member"  # child: user, is_lead
OUTREACH_DEPARTMENT_DOCTYPE = "Outreach Session Department"  # child: department offered
OUTREACH_DEVICE_DOCTYPE = "Outreach Device"
OUTREACH_GRANT_DOCTYPE = "Outreach Device Grant"
OUTREACH_SETTINGS_DOCTYPE = "EMR Outreach Settings"  # Single
# O2 sync and conflicts
OUTREACH_BATCH_DOCTYPE = "Outreach Sync Batch"
OUTREACH_RECORD_DOCTYPE = "Outreach Sync Record"
SYNC_CONFLICT_DOCTYPE = "Sync Conflict"

# Outreach Session lifecycle
SESSION_PLANNED = "Planned"
SESSION_ACTIVE = "Active"  # package downloaded at least once
SESSION_RETURNED = "Returned"  # team back; records may still be syncing
SESSION_CLOSED = "Closed"
SESSION_CANCELLED = "Cancelled"
SESSION_STATUSES = (SESSION_PLANNED, SESSION_ACTIVE, SESSION_RETURNED, SESSION_CLOSED, SESSION_CANCELLED)
# Sessions a device may enrol in, download for and sync into
SESSION_OPEN_STATUSES = (SESSION_PLANNED, SESSION_ACTIVE, SESSION_RETURNED)
# Roles that plan sessions and manage devices
OUTREACH_MANAGER_ROLES = (ROLE_MIC, ROLE_HIS)

# Device
DEVICE_ACTIVE = "Active"
DEVICE_REVOKED = "Revoked"

# Offline record types (envelope "type")
RECORD_PATIENT_REGISTER = "patient.register"
RECORD_PATIENT_UPDATE = "patient.update"
RECORD_VISIT_CAPTURE = "visit.capture"
RECORD_COMMUNITY_ACTIVITY = "community.activity"
RECORD_TYPES = (
	RECORD_PATIENT_REGISTER,
	RECORD_PATIENT_UPDATE,
	RECORD_VISIT_CAPTURE,
	RECORD_COMMUNITY_ACTIVITY,
)

# Per-record result (wire, lower case) and Outreach Sync Record.status (stored, title case)
RESULT_ACCEPTED = "accepted"
RESULT_DUPLICATE = "duplicate"  # already accepted earlier; the earlier result is returned
RESULT_FAILED = "failed"  # validation error; the device keeps it and shows the error
RESULT_HELD = "held"  # waits on a record that is in conflict
RESULT_CONFLICT = "conflict"  # a Sync Conflict was raised for this record
RECORD_STATUS_ACCEPTED = "Accepted"
RECORD_STATUS_FAILED = "Failed"
RECORD_STATUS_HELD = "Held"
RECORD_STATUS_CONFLICT = "Conflict"
RECORD_STATUS_DISCARDED = "Discarded"  # a conflict decision kept the facility version (payload kept)
RECORD_STATUSES = (
	RECORD_STATUS_ACCEPTED,
	RECORD_STATUS_FAILED,
	RECORD_STATUS_HELD,
	RECORD_STATUS_CONFLICT,
	RECORD_STATUS_DISCARDED,
)

# Sync Conflict (FR-26)
CONFLICT_DUPLICATE_REGISTRATION = "Duplicate registration"
CONFLICT_DEMOGRAPHIC_CHANGE = "Demographic change"
CONFLICT_OVERLAPPING_VISIT = "Overlapping visit"
CONFLICT_TYPES = (CONFLICT_DUPLICATE_REGISTRATION, CONFLICT_DEMOGRAPHIC_CHANGE, CONFLICT_OVERLAPPING_VISIT)
CONFLICT_OPEN = "Open"
CONFLICT_RESOLVED = "Resolved"
DECISION_KEEP_FACILITY = "Keep facility version"
DECISION_KEEP_OUTREACH = "Keep outreach version"
DECISION_MERGE = "Merge field by field"
DECISION_LINK = "Link as the same patient"
DECISION_ACKNOWLEDGE = "Reviewed - keep both"  # review-only overlap (closed facility visit, same day)
CONFLICT_DECISIONS = (
	DECISION_KEEP_FACILITY,
	DECISION_KEEP_OUTREACH,
	DECISION_MERGE,
	DECISION_LINK,
	DECISION_ACKNOWLEDGE,
)
# OI-11 (user decision D58): the HIS Officer resolves, the Medical In-Charge as backup.
CONFLICT_RESOLVER_ROLES = (ROLE_HIS, ROLE_MIC)

# Custom fields (W0 creates them in outreach/setup.py)
PATIENT_OPEN_CONFLICTS_FIELD = "krcs_open_sync_conflicts"  # Patient, Int, read-only
POINT_OF_CARE_FIELD = "krcs_point_of_care"  # Observation Template, Check
# Patient Visit link to its session (existing field; Link to Outreach Session from Phase 4)
VISIT_OUTREACH_FLAG_FIELD = "is_outreach"
VISIT_OUTREACH_SESSION_FIELD = "outreach_session"

# EMR Outreach Settings defaults (D58: user answers to OI-10)
OUTREACH_DEFAULTS = {
	"offline_session_days": 7,  # PIN-only use after the last online sign-in
	"purge_after_days": 7,  # device deletes a record this long after the server acknowledged it
	"summary_visits": 3,  # visits in each patient's package summary
	"pin_min_length": 6,
	"max_pin_attempts": 5,  # then the PIN key is discarded; an online sign-in restores it (escrow)
	"max_batch": 50,  # records per sync call
	"package_max_patients": 5000,
	"conflict_score": 8,  # similarity score from which an offline registration is a conflict
}

# Departments never offered offline (sensitive; OI-7). Mental-health content never enters a package.
OUTREACH_EXCLUDED_DEPARTMENTS = ("Mental Health",)

# Access Log action for a patient sent to a device in a package (privacy.access_log)
ACCESS_ACTION_OUTREACH_PACKAGE = "Outreach package"

# Reason recorded when an outreach consultation completes while a sample went to the facility lab
OUTREACH_LAB_PENDING_REASON = "Outreach: sample taken to the facility laboratory"
OUTREACH_NO_VITALS_REASON = "Outreach visit: no vital signs taken"

# Auth scheme of the device sync grant (outreach.auth, via hooks.auth_hooks)
OUTREACH_AUTH_SCHEME = "krcs-outreach"


# ---------------------------------------------------------------------------
# Maternity contract (W0): Phase 5M labour ward at Kalobeyei (SDD §5.12.3, INP-12..18, ROL-24, D60). Builders
# import these and never edit them. Field names and endpoints are in docs/MATERNITY_CONTRACT.md.
# ---------------------------------------------------------------------------
# DocTypes (module "EMR Maternity")
MATERNITY_ADMISSION_DOCTYPE = "Maternity Admission"  # {prefix}-MAT-.YYYY.- (the IP number)
MATERNITY_ADMISSION_LOG_DOCTYPE = "Maternity Admission Log"  # child: append-only status log
OBSTETRIC_HISTORY_DOCTYPE = "Obstetric History"  # child: one row per previous pregnancy
PARTOGRAPH_DOCTYPE = "Partograph"  # {prefix}-PTG-.YYYY.- (one per labour)
PARTOGRAPH_OBSERVATION_DOCTYPE = "Partograph Observation"  # child: hourly rows, append-only
DELIVERY_RECORD_DOCTYPE = "Delivery Record"  # {prefix}-DEL-.YYYY.-, submittable (Summary of Labour)
DELIVERY_BABY_DOCTYPE = "Delivery Baby"  # child: one row per baby

# Maternity Admission lifecycle (M1 owns maternity.admission.TRANSITIONS)
ADMISSION_ADMITTED = "Admitted"
ADMISSION_IN_LABOUR = "In Labour"
ADMISSION_DELIVERED = "Delivered"
ADMISSION_DISCHARGED = "Discharged"
ADMISSION_STATUSES = (ADMISSION_ADMITTED, ADMISSION_IN_LABOUR, ADMISSION_DELIVERED, ADMISSION_DISCHARGED)
ADMISSION_OPEN_STATUSES = (ADMISSION_ADMITTED, ADMISSION_IN_LABOUR, ADMISSION_DELIVERED)

# Discharge outcome of the mother (Maternity Admission.discharge_outcome)
MATERNITY_DISCHARGED_HOME = "Discharged home"
MATERNITY_REFERRED_OUT = "Referred out"
MATERNITY_DIED = "Died"  # needs a reason and a DIED_ROLES role (maternal death, MOH 717)
MATERNITY_ABSCONDED = "Absconded"
MATERNITY_DISCHARGE_OUTCOMES = (
	MATERNITY_DISCHARGED_HOME,
	MATERNITY_REFERRED_OUT,
	MATERNITY_DIED,
	MATERNITY_ABSCONDED,
)

# Mode of delivery (Delivery Record.delivery_mode); "born before arrival" is the separate Check
# Delivery Record.born_before_arrival (MOH 717 counts BBA on its own line).
DELIVERY_SVD = "Spontaneous vaginal delivery"
DELIVERY_ASSISTED = "Assisted vaginal delivery"
DELIVERY_CAESAREAN = "Caesarean section"
DELIVERY_BREECH = "Breech delivery"
DELIVERY_MODES = (DELIVERY_SVD, DELIVERY_ASSISTED, DELIVERY_CAESAREAN, DELIVERY_BREECH)
DELIVERY_BBA_FIELD = "born_before_arrival"

# Baby outcome (Delivery Baby.outcome). Only a live birth becomes a Patient.
BABY_LIVE_BIRTH = "Live birth"
BABY_FRESH_STILLBIRTH = "Fresh stillbirth"
BABY_MACERATED_STILLBIRTH = "Macerated stillbirth"
BABY_OUTCOMES = (BABY_LIVE_BIRTH, BABY_FRESH_STILLBIRTH, BABY_MACERATED_STILLBIRTH)

# Partograph coded values (WHO modified partograph; the Partograph Observation Select options)
LIQUOR_CODES = ("I", "C", "M", "B", "A")  # intact, clear, meconium, blood-stained, absent
MOULDING_GRADES = ("0", "+", "++", "+++")
CONTRACTION_DURATION_BANDS = ("<20 s", "20-40 s", ">40 s")
URINE_DIPSTICK_VALUES = ("Nil", "Trace", "+", "++", "+++")
PARTOGRAPH_ACTIVE_LABOUR_CM = 4  # the chart starts at the first observation at or above 4 cm

# Who admits, discharges, keeps the partograph and records deliveries
MATERNITY_ROLES = (ROLE_MIDWIFE, ROLE_CLINICIAN, ROLE_MIC)

# Newborn registration (M3): duplicate-check override reason (twins / "Baby of ..." names)
NEWBORN_OVERRIDE_REASON = "Newborn registered at delivery"

# Custom fields (W0 creates them in maternity/setup.py)
PATIENT_MOTHER_FIELD = "krcs_mother"  # Patient -> Patient (the mother), read-only, set at delivery
VITAL_SIGNS_ADMISSION_FIELD = "krcs_maternity_admission"  # Vital Signs -> Maternity Admission (ward obs)

# Healthcare Service Unit group seeded under each facility: "Maternity Ward - <facility code>" (+ " - <abbr>")
MATERNITY_WARD_NAME = "Maternity Ward"
