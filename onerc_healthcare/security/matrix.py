# Copyright (c) 2026, Lennox Mwita and contributors
# For license information, please see license.txt
"""Declarative access matrix for the EMR roles (SDD v0.1 §6.1, FR-39, C-5, NFR-9).

Permission codes: r = read (+ report, print), w = write, c = create, s = submit, e = export.
Submit is dropped automatically on non-submittable DocTypes. Delete is never granted (NFR-9).

DocPerms on our own DocTypes (Patient Visit, Patient Identifier…) live in their DocType JSON and are
owned by the builder of that DocType. This file only covers Marley / ERPNext / Frappe DocTypes, which
we customise through Custom DocPerm.
"""

from onerc_healthcare.constants import (
	ROLE_CLINICIAN,
	ROLE_HEAD_OPS,
	ROLE_HIS,
	ROLE_LAB,
	ROLE_MHO,
	ROLE_MIC,
	ROLE_OUTREACH,
	ROLE_PHARMACIST,
	ROLE_RECEPTIONIST,
	ROLE_SYSADMIN,
	ROLE_TRIAGE,
)

# Roles that work at the desk on facility data. The Outreach Team Member is left out on purpose: it
# has no desk access (see ROLE_SETTINGS) and gets its access through the Phase 4 outreach app.
FACILITY_ROLES = (
	ROLE_RECEPTIONIST,
	ROLE_TRIAGE,
	ROLE_CLINICIAN,
	ROLE_MHO,
	ROLE_LAB,
	ROLE_PHARMACIST,
	ROLE_HIS,
	ROLE_MIC,
)
_ALL_READ = dict.fromkeys(FACILITY_ROLES, "r")

# Clinical and patient-level DocTypes (Phase 1 baseline). The EMR System Administrator and the Head
# of Operations are absent from every entry: no clinical content (SDD §6.1).
CLINICAL_MATRIX: dict[str, dict[str, str]] = {
	"Patient": {
		ROLE_RECEPTIONIST: "rwc",
		ROLE_TRIAGE: "r",  # demographics read-only
		ROLE_CLINICIAN: "r",
		ROLE_MHO: "r",
		ROLE_LAB: "r",
		ROLE_PHARMACIST: "r",
		ROLE_HIS: "re",
		ROLE_MIC: "re",
	},
	# No cancel/amend in Phase 1: Marley deletes the Patient Medical Record on cancel and that needs a
	# delete right we never grant. Corrections are a Phase 2 decision.
	"Vital Signs": {
		ROLE_TRIAGE: "rwcs",
		ROLE_CLINICIAN: "r",
		ROLE_MHO: "r",
		ROLE_MIC: "r",
	},
	"Patient Encounter": {
		ROLE_CLINICIAN: "rwcs",
		ROLE_MHO: "rwcs",
		ROLE_HIS: "r",  # reports and diagnosis mapping; no editing (SDD §6.1)
		ROLE_MIC: "r",
	},
	"Service Request": {
		ROLE_CLINICIAN: "rwcs",
		ROLE_MHO: "rwcs",
		ROLE_LAB: "r",
		ROLE_HIS: "r",
		ROLE_MIC: "r",
	},
	"Medication Request": {
		ROLE_CLINICIAN: "rwcs",
		ROLE_MHO: "rwcs",
		ROLE_PHARMACIST: "r",
		ROLE_HIS: "r",
		ROLE_MIC: "r",
	},
	"Observation": {
		ROLE_LAB: "rwcs",
		ROLE_CLINICIAN: "r",
		ROLE_MHO: "r",
		ROLE_HIS: "r",
		ROLE_MIC: "r",
	},
	"Sample Collection": {
		ROLE_LAB: "rwcs",
		ROLE_CLINICIAN: "r",
		ROLE_MHO: "r",
		ROLE_MIC: "r",
	},
	"Diagnostic Report": {
		ROLE_LAB: "rwc",
		ROLE_CLINICIAN: "r",
		ROLE_MHO: "r",
		ROLE_HIS: "r",
		ROLE_MIC: "r",
	},
}

# Reference data used by EMR forms and link searches. The EMR System Administrator maintains it
# (configuration); the Head of Operations reads the operational masters for the queue overview.
MASTER_MATRIX: dict[str, dict[str, str]] = {
	"Medical Department": {**_ALL_READ, ROLE_HEAD_OPS: "r", ROLE_SYSADMIN: "rwc"},
	"Healthcare Service Unit": {**_ALL_READ, ROLE_HEAD_OPS: "r", ROLE_SYSADMIN: "rwc"},
	"Triage Level": {**_ALL_READ, ROLE_HEAD_OPS: "r", ROLE_SYSADMIN: "rwc"},
	"Code System": {**_ALL_READ, ROLE_SYSADMIN: "rwc"},
	"Code Value": {**_ALL_READ, ROLE_SYSADMIN: "rwc"},
	"Healthcare Practitioner": {**_ALL_READ, ROLE_SYSADMIN: "rwc"},
	"Observation Template": {
		ROLE_CLINICIAN: "r",
		ROLE_MHO: "r",
		ROLE_LAB: "r",
		ROLE_MIC: "r",
		ROLE_SYSADMIN: "r",
	},
}

# EMR System Administrator: user and role management and configuration, no clinical content.
# User permlevel 1 holds the roles / role profile / module fields. Escalation to System Manager is
# blocked by security.user_guard (hook on User validate).
ADMIN_MATRIX: dict[str, dict[str, str] | dict[int, dict[str, str]]] = {
	"User": {0: {ROLE_SYSADMIN: "rwc"}, 1: {ROLE_SYSADMIN: "rw"}},
	"Role": {0: {ROLE_SYSADMIN: "r"}},
	"Role Profile": {0: {ROLE_SYSADMIN: "rwc"}, 1: {ROLE_SYSADMIN: "rw"}},
	"Module Profile": {0: {ROLE_SYSADMIN: "rwc"}},
	"User Permission": {0: {ROLE_SYSADMIN: "rwc"}},
	# Read-only: password policy, lockout, 2FA and session expiry are System Manager decisions (NFR-5).
	"System Settings": {0: {ROLE_SYSADMIN: "r"}},
	# Read-only so the billing-free settings (C-5) stay under System Manager control.
	"Healthcare Settings": {0: {ROLE_SYSADMIN: "r"}},
}

# Bulk-deletion routes closed for every role (NFR-9). Enforced on every migrate.
BULK_DELETION_PERMS: dict[str, dict[str, int]] = {
	"Transaction Deletion Record": {"create": 0, "submit": 0, "cancel": 0, "amend": 0, "delete": 0},
	"Company": {"delete": 0},
}

# Billing DocTypes no EMR role may touch (C-5). Enforced on every migrate.
BILLING_DOCTYPES = (
	"Sales Invoice",
	"POS Invoice",
	"Payment Entry",
	"Journal Entry",
	"Price List",
	"Item Price",
	"Fee Validity",
	"Payment Request",
	"Sales Order",
	"Quotation",
)

# Navigation lock-down (C-5): these Workspaces get a roles table limited to the native ERPNext / Marley
# roles of the module, so EMR-only users never see them. Applied only while the workspace has no roles
# of its own, so an admin's choice survives; re-applied after an app update resets the JSON.
RESTRICTED_WORKSPACES: dict[str, tuple[str, ...]] = {
	"Accounting": ("Accounts User", "Accounts Manager", "Auditor", "System Manager"),
	"Invoicing": ("Accounts User", "Accounts Manager", "Auditor", "System Manager"),
	"Payments": ("Accounts User", "Accounts Manager", "Auditor", "System Manager"),
	"Financial Reports": ("Accounts User", "Accounts Manager", "Auditor", "System Manager"),
	"Selling": ("Sales User", "Sales Manager", "Sales Master Manager", "System Manager"),
	"Buying": ("Purchase User", "Purchase Manager", "Purchase Master Manager", "System Manager"),
	"Stock": ("Stock User", "Stock Manager", "Item Manager", "System Manager"),
	"Insurance": ("Healthcare Administrator", "System Manager"),
}

# Workspaces the EMR System Administrator needs for user management (added if missing).
ADMIN_WORKSPACES: dict[str, tuple[str, ...]] = {"Users": (ROLE_SYSADMIN,)}

# Role settings applied on first run only (admin edits survive). home_page is the "/" redirect for the
# role; department workspaces from the visit workstream may replace these list routes later.
ROLE_SETTINGS: dict[str, dict] = {
	ROLE_RECEPTIONIST: {"desk_access": 1, "home_page": "app/patient"},
	ROLE_TRIAGE: {"desk_access": 1, "home_page": "app/vital-signs"},
	ROLE_CLINICIAN: {"desk_access": 1, "home_page": "app/patient-encounter"},
	ROLE_MHO: {"desk_access": 1, "home_page": "app/patient-encounter"},
	ROLE_LAB: {"desk_access": 1, "home_page": "app/sample-collection"},
	ROLE_PHARMACIST: {"desk_access": 1, "home_page": "app/medication-request"},
	ROLE_HIS: {"desk_access": 1, "home_page": "app/patient", "two_factor_auth": 1},
	ROLE_MIC: {"desk_access": 1, "home_page": "app/patient"},
	ROLE_HEAD_OPS: {"desk_access": 1, "home_page": "app/patient-visit"},
	ROLE_SYSADMIN: {"desk_access": 1, "home_page": "app/user", "two_factor_auth": 1},
	# No desk access (SDD §6.1): the outreach app (Phase 4) is the only entry point.
	ROLE_OUTREACH: {"desk_access": 0},
}

# NFR-5 / NFR-8 / SDD §6.3. Applied on first run only.
SYSTEM_SETTINGS = {
	"enable_password_policy": 1,
	"minimum_password_score": "3",
	"allow_consecutive_login_attempts": 5,
	"allow_login_after_fail": 900,  # seconds locked out after 5 failures
	"session_expiry": "08:00",  # idle expiry (hh:mm); the Medical In-Charge may change it
	"enable_two_factor_auth": 1,
	"two_factor_method": "OTP App",
	"allow_guests_to_upload_files": 0,
}
WEBSITE_SETTINGS = {"disable_signup": 1}
