# Underscore aliases: Frappe treats every public module attribute here as a hook.
from onerc_healthcare.constants import ACCESS_LOGGED_DOCTYPES as _ACCESS_LOGGED_DOCTYPES
from onerc_healthcare.constants import CLINICAL_DOCTYPES as _CLINICAL_DOCTYPES

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
	# --- Phase 2 (W0 contract): stubs owned by B1 clinical / B2 lab / B4 privacy ---
	"Patient Encounter": {
		"before_validate": "onerc_healthcare.clinical.events.encounter_before_validate",
		"validate": "onerc_healthcare.clinical.events.encounter_validate",
		"before_submit": "onerc_healthcare.clinical.events.encounter_before_submit",
		"before_cancel": "onerc_healthcare.clinical.events.encounter_before_cancel",
	},
	"Healthcare Practitioner": {
		"on_update": "onerc_healthcare.clinical.events.practitioner_on_update",
	},
	"Observation": {
		# M6: lock the visit before Frappe writes the Observation (lock order Visit -> Encounter -> Observation)
		"before_submit": "onerc_healthcare.lab.events.observation_before_submit",
		"before_cancel": "onerc_healthcare.lab.events.observation_before_cancel",
		"on_submit": "onerc_healthcare.lab.events.observation_on_submit",
		"on_cancel": "onerc_healthcare.lab.events.observation_on_cancel",
	},
	"Service Request": {
		"on_cancel": "onerc_healthcare.lab.events.service_request_on_cancel",
	},
	"Sample Collection": {
		"before_cancel": "onerc_healthcare.lab.events.sample_collection_before_cancel",
	},
	"Access Log": {
		"after_insert": "onerc_healthcare.privacy.access_log.mirror_core_access_log",
	},
}

# Access logging of desk record opens (FR-40, NFR-7): `onload` on every logged DocType (B4).
for _doctype in _ACCESS_LOGGED_DOCTYPES:
	doc_events.setdefault(_doctype, {})["onload"] = "onerc_healthcare.privacy.access_log.log_open"

# B1: internal referral -> Received when the target visit starts consultation.
doc_events.setdefault("Patient Visit", {})["on_update"] = "onerc_healthcare.clinical.referral.visit_on_update"
# B4: warn (never block) when a visit is registered for a patient without current consent (FR-37, OI-14).
doc_events["Patient Visit"]["after_insert"] = "onerc_healthcare.privacy.consent.visit_after_insert"
# B1: the encounter addendum is append-only after submit.
doc_events["Patient Encounter"]["before_update_after_submit"] = (
	"onerc_healthcare.clinical.events.encounter_before_update_after_submit"
)

# ERPNext "Delete Company Transactions" bulk-deletes company-linked DocTypes with frappe.db.delete,
# bypassing on_trash. Clinical records must never be deleted (NFR-9, SDD §4.6).
company_data_to_be_ignored = list(_CLINICAL_DOCTYPES)

# Desk navigation fix: Marley's workspaces open under their own sidebars (D26).
boot_session = "onerc_healthcare.boot.boot_session"

doctype_js = {
	"Patient": ["registration/patient.js", "privacy/patient.js"],  # A1 registration + B4 consent
	"Vital Signs": "visit/vital_signs.js",
	"Patient Encounter": "clinical/patient_encounter.js",
	"Service Request": "lab/service_request.js",
	"Medication Request": "pharmacy/medication_request.js",
}

# Marley endpoints that insert/submit with ignore_permissions and no permission check (Phase 2 audit A3).
_PE = "healthcare.healthcare.doctype.patient_encounter.patient_encounter"
_SR = "healthcare.healthcare.doctype.service_request.service_request"
_OBS = "healthcare.healthcare.doctype.observation.observation"
override_whitelisted_methods = {
	f"{_PE}.create_service_request": "onerc_healthcare.clinical.guards.create_service_request",
	f"{_PE}.create_service_request_from_widget": (
		"onerc_healthcare.clinical.guards.create_service_request_from_widget"
	),
	f"{_PE}.create_patient_referral": "onerc_healthcare.clinical.guards.create_patient_referral",
	f"{_PE}.cancel_request": "onerc_healthcare.clinical.guards.cancel_request",
	# Would release prescriptions from a draft encounter (B1).
	f"{_PE}.create_medication_request": "onerc_healthcare.clinical.guards.create_medication_request",
	"healthcare.healthcare.doctype.service_request.service_request.make_observation": (
		"onerc_healthcare.lab.guards.make_observation"
	),
	# M8: Marley endpoints that leaked order/patient details or took a client ignore_permissions
	f"{_SR}.make_lab_test": "onerc_healthcare.lab.guards.make_lab_test",
	f"{_SR}.make_clinical_procedure": "onerc_healthcare.lab.guards.make_clinical_procedure",
	f"{_SR}.make_appointment": "onerc_healthcare.lab.guards.make_appointment",
	# M6: Marley's bulk/panel approve and reject take the visit lock first (no Observation -> Visit cycle; B2)
	f"{_OBS}.set_observation_status": "onerc_healthcare.lab.guards.set_observation_status",
	f"{_OBS}.approve_all_observations": "onerc_healthcare.lab.guards.approve_all_observations",
	f"{_OBS}.reject_all_observations": "onerc_healthcare.lab.guards.reject_all_observations",
	# M4: read access logging for logged doctypes/reports (wrappers keep upstream signatures; B4)
	"frappe.client.get": "onerc_healthcare.privacy.read_log.get",
	"frappe.client.get_value": "onerc_healthcare.privacy.read_log.get_value",
	"frappe.desk.reportview.get": "onerc_healthcare.privacy.read_log.reportview_get",
	"frappe.desk.reportview.export_query": "onerc_healthcare.privacy.read_log.reportview_export_query",
	"frappe.desk.query_report.export_query": "onerc_healthcare.privacy.read_log.query_report_export_query",
	# M4 round 2: list APIs, link fetch, print preview, Data Import record export (B4)
	"frappe.client.get_list": "onerc_healthcare.privacy.read_log.get_list",
	"frappe.desk.reportview.get_list": "onerc_healthcare.privacy.read_log.reportview_get_list",
	"frappe.client.validate_link_and_fetch": "onerc_healthcare.privacy.read_log.validate_link_and_fetch",
	"frappe.www.printview.get_html_and_style": "onerc_healthcare.privacy.read_log.get_html_and_style",
	"frappe.core.doctype.data_import.data_import.download_template": (
		"onerc_healthcare.privacy.read_log.download_template"
	),
	# L6: no forged access-log entries over HTTP (B4)
	"frappe.core.doctype.access_log.access_log.make_access_log": (
		"onerc_healthcare.privacy.access_log.make_access_log"
	),
}

# Visit task reconciler (lab/pharmacy completion missed by a race or a skipped event).
scheduler_events = {
	"cron": {
		"*/10 * * * *": ["onerc_healthcare.visit.tasks.reconcile_open_tasks"],
	},
}

permission_query_conditions = {
	"Patient Visit": "onerc_healthcare.visit.permissions.patient_visit_query",
	"Patient Encounter": "onerc_healthcare.clinical.permissions.encounter_query",
	"Access Log Entry": "onerc_healthcare.privacy.permissions.access_log_query",
}

has_permission = {
	"Patient Visit": "onerc_healthcare.visit.permissions.patient_visit_has_permission",
	# H1: Data Access Request extracts are never downloadable as files (only via download_extract; B4)
	"File": "onerc_healthcare.privacy.permissions.file_has_permission",
	"Patient Encounter": "onerc_healthcare.clinical.permissions.encounter_has_permission",
	"Access Log Entry": "onerc_healthcare.privacy.permissions.access_log_has_permission",
}

# M4: log successful REST/API reads of logged doctypes (after_request: the API user is known by then; B4)
after_request = ["onerc_healthcare.privacy.read_log.after_request"]
