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
	"*": {
		"on_trash": "onerc_healthcare.security.delete_guard.on_trash",
	},
}

permission_query_conditions = {
	"Patient Visit": "onerc_healthcare.visit.permissions.patient_visit_query",
}

has_permission = {
	"Patient Visit": "onerc_healthcare.visit.permissions.patient_visit_has_permission",
}
