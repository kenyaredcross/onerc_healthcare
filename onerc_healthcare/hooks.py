from onerc_healthcare.constants import CLINICAL_DOCTYPES

app_name = "onerc_healthcare"
app_title = "Onerc Healthcare"
app_publisher = "Lennox Mwita"
app_description = "OneRC Healthcare "
app_email = "mwitalnx@gmail.com"
app_license = "mit"

# Send non-GET requests for this app's endpoints as native `application/json`
# bodies instead of form-encoded, per-key JSON-stringified values.
use_json_request_body = True

# Every whitelisted method must have type-annotated parameters.
require_type_annotated_api_methods = True
export_python_type_annotations = True

required_apps = ["erpnext", "healthcare"]

# ---------------------------------------------------------------------------
# Install / migrate. Seeding is idempotent: create what is missing, never
# overwrite what an admin has edited. See onerc_healthcare/setup/install.py.
# The operation (kalobeyei | dadaab) comes from site config `krcs_operation`.
# ---------------------------------------------------------------------------
after_install = "onerc_healthcare.setup.install.after_install"
after_migrate = "onerc_healthcare.setup.install.after_migrate"

# ---------------------------------------------------------------------------
# Workstream wiring. Each handler lives in the owning workstream's package
# (registration / visit / security). Handlers start as no-op stubs and are
# filled in by that workstream; this file is owned by integration only.
# ---------------------------------------------------------------------------
extend_doctype_class = {
	"Patient": "onerc_healthcare.registration.patient.PatientMixin",
}

doc_events = {
	"Patient": {
		"validate": "onerc_healthcare.registration.events.patient_validate",
		"after_insert": "onerc_healthcare.registration.events.patient_after_insert",
	},
	"Vital Signs": {
		"validate": "onerc_healthcare.visit.events.vital_signs_validate",
		"on_submit": "onerc_healthcare.visit.events.vital_signs_on_submit",
	},
	"User": {
		"validate": "onerc_healthcare.security.user_guard.user_validate",
	},
	"Role Profile": {
		"validate": "onerc_healthcare.security.user_guard.role_profile_validate",
	},
	"Transaction Deletion Record": {
		"before_insert": "onerc_healthcare.security.delete_guard.block_transaction_deletion",
		"before_submit": "onerc_healthcare.security.delete_guard.block_transaction_deletion",
	},
	"*": {
		"on_trash": "onerc_healthcare.security.delete_guard.on_trash",
	},
}

# ERPNext "Delete Company Transactions" bulk-deletes company-linked DocTypes with frappe.db.delete,
# bypassing on_trash. Clinical records must never be deleted (NFR-9, SDD §4.6).
company_data_to_be_ignored = list(CLINICAL_DOCTYPES)

doctype_js = {
	"Patient": "registration/patient.js",
	"Vital Signs": "visit/vital_signs.js",
}

permission_query_conditions = {
	"Patient Visit": "onerc_healthcare.visit.permissions.patient_visit_query",
}

has_permission = {
	"Patient Visit": "onerc_healthcare.visit.permissions.patient_visit_has_permission",
}
