# Overrides and customisations of Frappe, ERPNext and Marley

`onerc_healthcare` never edits the files of Frappe, ERPNext or Frappe Health (Marley). Every change to
their behaviour goes through one of the mechanisms below:
- Frappe hooks in `onerc_healthcare/hooks.py`;
- records our install/migrate code writes: custom fields, property setters, Custom DocPerms, settings,
  and direct row writes.

This file lists each one, what it does and why it exists, so the effect of a Frappe or Marley upgrade
can be checked quickly. Decision numbers (D…) refer to `DECISIONS.md`. Finding IDs (H1, M4, …) are from
the Phase 2 independent review.

> **When you upgrade Frappe or Marley,** work through the [upgrade checklist](#9-upgrade-checklist) at
> the end.

---

## 1. Overridden whitelisted methods (`override_whitelisted_methods`)

Frappe calls our function instead of the original when the method is called over HTTP (desk and REST).
Server-side Python that imports the original directly is not affected.

### Marley endpoints that skipped permission checks

| Original (Marley) | Ours | What ours does | Why |
|---|---|---|---|
| `patient_encounter.create_service_request` | `clinical.guards.create_service_request` | Refuses (PermissionError). | Marley inserts and submits orders with `ignore_permissions` and no check, and can even force `docstatus=1` on "invoiced" rows. Orders are released only by our consultation flow (D28). |
| `patient_encounter.create_service_request_from_widget` | `clinical.guards.create_service_request_from_widget` | Refuses. | Same as above. |
| `patient_encounter.create_patient_referral` | `clinical.guards.create_patient_referral` | Refuses. | Marley's "referral" is only a Service Request. Ours is the Referral record (D33, BR-14). |
| `patient_encounter.cancel_request` | `clinical.guards.cancel_request` | Refuses. | It cancels any document with no permission check. |
| `patient_encounter.create_medication_request` | `clinical.guards.create_medication_request` | Refuses. | It would release prescriptions from a draft encounter. They are released on Complete Consultation (D28). |
| `service_request.make_observation` | `lab.guards.make_observation` | Requires the Lab Technician, Clinical Officer or Mental Health Officer role plus read on the Service Request, then provisions the order with `lab.orders.ensure_observations`. | Marley's version has no permission check, and **stops after an encounter's first lab order**: later orders got no sample row or Observation (D34). |
| `service_request.make_lab_test` | `lab.guards.make_lab_test` | Administrator only. | It leaked patient name, mobile and email with no permission check (M8). Legacy Lab Test is unused (D5). |
| `service_request.make_clinical_procedure` | `lab.guards.make_clinical_procedure` | Administrator only. | Same leak (M8). Clinical procedures are not in scope. |
| `service_request.make_appointment` | `lab.guards.make_appointment` | Requires read on the Service Request and always calls Marley with `ignore_permissions=False`. | Marley took `ignore_permissions` from the client (M8). |
| `observation.set_observation_status` | `lab.guards.set_observation_status` | Accepts only Approved or Rejected. Requires submit or cancel permission on the Observation, locks the visit first, then delegates. | Panel approval submits several Observations. Without the visit lock first, it deadlocked with a single approval (M6, D41). |
| `observation.approve_all_observations` | `lab.guards.approve_all_observations` | Requires write on the Diagnostic Report plus submit on Observation, locks the visit first, then delegates. | Same deadlock (M6). |
| `observation.reject_all_observations` | `lab.guards.reject_all_observations` | Same checks with cancel, locks the visit first, then delegates. | Same deadlock (M6). |

Paths are `healthcare.healthcare.doctype.<doctype>.<doctype>.<function>`.

### Frappe core endpoints: read-access logging (FR-40, NFR-7, finding M4)

| Original (Frappe) | Ours (`privacy.read_log`) | What ours does | Why |
|---|---|---|---|
| `frappe.client.get` | `get` | Calls the original, then writes one Access Log Entry "Read (API)" when the DocType is in `ACCESS_LOGGED_DOCTYPES`. | Desk and API reads of patient records were not audited. |
| `frappe.client.get_value` | `get_value` | Same: log after a successful call. | Same. |
| `frappe.desk.reportview.get` | `reportview_get` | Logs "List" with the filter names and row count, never values. | List and Report View could show clinical columns unlogged. |
| `frappe.desk.reportview.export_query` | `reportview_export_query` | Logs "Export". | Exports were logged only by client JavaScript. |
| `frappe.desk.query_report.export_query` | `query_report_export_query` | Logs "Export" for our reports and for reports on logged DocTypes. | A direct call skipped the log. |
| `frappe.client.get_list` | `get_list` | Logs "List" (filter names and row count). | List reads of logged records (D44). REST `/api/resource/<doctype>` lists call the core function directly and are logged by `after_request` instead. |
| `frappe.desk.reportview.get_list` | `reportview_get_list` | Logs "List". | Used by `frappe.db.get_list` in desk JS. |
| `frappe.client.validate_link_and_fetch` | `validate_link_and_fetch` | Logs "Read (API)" when fields are fetched from a logged record. | It could pull clinical fields, for example from an encounter, without a log. |
| `frappe.www.printview.get_html_and_style` | `get_html_and_style` | Logs "Print". | Print preview rendered records without a log. |
| `frappe.core.doctype.data_import.data_import.download_template` | `download_template` | Refuses exporting **records** of a logged DocType unless the user is System Manager, whose export is logged. Blank templates are still allowed. | Bulk export of clinical records is not an EMR workflow (D44). |

The wrappers keep the **exact upstream signatures, type annotations and decorators**, and log only
after the original succeeds, so a refused call logs nothing.

### Frappe core endpoint: forged access-log entries (finding L6)

| Original (Frappe) | Ours | What ours does | Why |
|---|---|---|---|
| `frappe.core.doctype.access_log.access_log.make_access_log` | `privacy.access_log.make_access_log` | Over HTTP, requires read on the document (or DocType, or permission on the report) and is rate-limited to 60 calls a minute. It then calls the original. | Anyone could fabricate audit entries that our mirror copied into the Access Log Entry. |

---

## 2. Controller extension (`extend_doctype_class`)

| DocType | Ours | What | Why |
|---|---|---|---|
| Patient (Marley) | `registration.patient.PatientMixin` | Mixed into Marley's controller (it always calls `super()`). It turns the identifier unique-index database error into the readable duplicate message (FR-2). | Duplicate prevention must hold even when two receptionists save at the same moment. |

We use `extend_doctype_class`, never `override_doctype_class`, so Marley's own logic still runs.

---

## 3. Document events on Frappe, ERPNext and Marley DocTypes (`doc_events`)

Events on our own DocTypes live in their controllers and are not listed here.

| DocType (owner) | Event | Handler | What and why |
|---|---|---|---|
| **All DocTypes** `"*"` | `on_trash` | `security.delete_guard.on_trash` | Refuses deleting any record in `CLINICAL_DOCTYPES`, for every role including Administrator, except during install, migrate or patches (NFR-9, D17). |
| Transaction Deletion Record (ERPNext) | `before_insert`, `before_submit` | `security.delete_guard.block_transaction_deletion` | ERPNext's "Delete Company Transactions" bulk-deletes with `frappe.db.delete`, which bypasses `on_trash` (D17). |
| User (Frappe) | `validate` | `security.user_guard.user_validate` | The EMR System Administrator may grant EMR roles only, and may not take over accounts that hold other roles (D15). |
| Role Profile (Frappe) | `validate` | `security.user_guard.role_profile_validate` | Stops privilege escalation through a role profile (D15). |
| Access Log (Frappe) | `after_insert` | `privacy.access_log.mirror_core_access_log` | Copies prints, exports and downloads into our append-only, merge-safe Access Log Entry (D38). |
| Patient (Marley) | `validate`, `after_insert` | `registration.events.*` | Identifier rules, walk-in IDs, duplicate check and similarity (FR-1 to FR-3). |
| Vital Signs (Marley) | `validate`, `on_submit` | `visit.events.*` | Plausibility checks, triage fields and the visit link. Submitting starts triage (FR-9, NFR-10). |
| Patient Encounter (Marley) | `before_validate` | `clinical.events.encounter_before_validate` | Defaults the practitioner and appointment type, and forces `submit_orders_on_save=0`. |
| Patient Encounter (Marley) | `validate` | `clinical.events.encounter_validate` | Requires an open visit for the same patient and one active encounter per visit. Validates the ICD-11 diagnoses (D28, D30). |
| Patient Encounter (Marley) | `before_submit` | `clinical.events.encounter_before_submit` | Refuses a desk Submit unless our consultation flow does the submitting. |
| Patient Encounter (Marley) | `before_cancel` | `clinical.events.encounter_before_cancel` | Second line of the no-cancel rule (D29). |
| Patient Encounter (Marley) | `before_update_after_submit` | `clinical.events.encounter_before_update_after_submit` | The addendum is append-only. |
| Healthcare Practitioner (Marley) | `on_update` | `clinical.events.practitioner_on_update` | Removes the User Permission Marley adds with `apply_to_all_doctypes`. It would hide colleagues' encounters and orders (D40, D22). |
| Observation (Marley) | `before_submit`, `before_cancel` | `lab.events.observation_before_*` | Locks the visit before Frappe writes the Observation, so concurrent approvals can't deadlock (M6; lock order Visit → Encounter → Observation). |
| Observation (Marley) | `on_submit`, `on_cancel` | `lab.events.observation_on_*` | Notifies the clinician and raises `lab_results_ready` when all orders are approved, or `request_lab` when a result is rejected (FR-17, D34). |
| Service Request (Marley) | `on_cancel` | `lab.events.service_request_on_cancel` | Re-checks lab completeness when an order is cancelled. |
| Sample Collection (Marley) | `before_cancel` | `lab.events.sample_collection_before_cancel` | Unlinks the Diagnostic Report, because Marley would otherwise call `delete_doc`, which our guard refuses. Re-points Observations so results can still be entered. |
| Logged DocTypes (`ACCESS_LOGGED_DOCTYPES`: Patient, Patient Encounter, Vital Signs, Observation, Diagnostic Report, Medication Request, Service Request, Patient Visit, Referral, Dispensing Record, Patient Consent, Data Access Request, Screening Record) | `onload` | `privacy.access_log.log_open` | Writes one "Open" Access Log Entry per desk open. Mental Health encounters are marked sensitive (FR-40, DPR-16). |

---

## 4. Permission hooks

| Hook | DocType | Handler | What and why |
|---|---|---|---|
| `has_permission` | **File** (Frappe) | `privacy.permissions.file_has_permission` | Denies every user except Administrator access to the private record **extract** of a Data Access Request. Other request attachments, such as an authorisation scan, follow Frappe's normal rules. Frappe would otherwise let anyone who can read the request download the full clinical-record extract (H1). The extract is served only by `privacy.extract.download_extract` (HIS Officer, Medical In-Charge; logged). |
| `has_permission`, `permission_query_conditions` | **Patient Encounter** (Marley) | `clinical.permissions.encounter_*` | Refuses `cancel` and `amend` before Marley's `before_cancel` runs (D29). Mental Health visibility is a pass-through until OI-7. |
| `has_permission`, `permission_query_conditions` | Patient Visit, Access Log Entry (ours) | `visit.permissions.*`, `privacy.permissions.*` | Listed for completeness. These are our own DocTypes. |
| `has_permission`, `permission_query_conditions` | Outreach Session (ours, Phase 4) | `outreach.permissions.*` | An Outreach Team Member reads only the sessions they are on; managers see all (D58). |

---

## 5. Request, session and scheduler hooks

| Hook | Handler | What and why |
|---|---|---|
| `after_request` | `privacy.read_log.after_request` | Logs successful REST and API v2 reads of logged records: single records as "Open (API)", lists as "List", one entry per request (M4, D44). It runs **after** the request because API-key and token users are only identified once `validate_auth` has run, which happens after `before_request`. |
| `boot_session` | `boot.boot_session` | Narrows each Marley sidebar's `workspaces` list to the workspaces it links to. All eight Marley sidebars share one module, so the desk opened the Healthcare page under the Diagnostics sidebar (D26). Only the boot payload changes; nothing is stored. |
| `scheduler_events` (every 10 minutes) | `visit.tasks.reconcile_open_tasks` | Catches lab or pharmacy completion events that a race or a skipped event missed (D36). |
| `company_data_to_be_ignored` | `CLINICAL_DOCTYPES` | ERPNext's "Delete Company Transactions" skips these DocTypes (NFR-9, D17). |
| `auth_hooks` (Phase 4) | `outreach.auth.authenticate_device` | Runs inside Frappe's `validate_auth` on every request. It does nothing unless the `Authorization` scheme is `krcs-outreach`. With that scheme, it accepts the device grant only on `sync.push`, `sync.status`, `package.get_reference` and `package.get_patients`, and sets the grant's user. Any other path with that scheme gets 401. The outreach tablet then syncs without a desk session or CSRF cookie (D58, `docs/OUTREACH_PROTOCOL.md`). |
| `website_route_rules` (Phase 4) | `/outreach/<path:app_path>` → `www/outreach.py` | Serves the React offline app, and its service worker at `/outreach/sw.js` with `Service-Worker-Allowed: /outreach/`. |

---

## 6. Form scripts on Marley DocTypes (`doctype_js`)

Our JavaScript is loaded **in addition to** Marley's.

| DocType | Script | What it changes in the form |
|---|---|---|
| Patient | `registration/patient.js`, `privacy/patient.js`, `outreach/patient.js` | Search-first dialog, possible-duplicates dialog, Link Identifier and Merge menu, consent status and Record Consent. Phase 4: a banner when outreach records for the patient wait on a Sync Conflict (FR-26). |
| Vital Signs | `visit/vital_signs.js` | Links the patient's open visit automatically. Adds the Route to Department dialog. |
| Patient Encounter | `clinical/patient_encounter.js` | Consultation buttons (Send Lab Orders, Complete, Refer, Addendum), the lab results and history panels, and the drug query on our non-stock catalogue. Hides Marley's Refer, create-order and delete-note buttons and the Submit, Cancel and Amend buttons. |
| Service Request | `lab/service_request.js` | Lab order view. |
| Medication Request | `pharmacy/medication_request.js` | Dispense status, Dispense button, Hold / Stop / Resume, and stock and billing UI hidden. Marley's Status buttons are removed because they would bypass a hold. |

---

## 7. Data-level customisations (written by install/migrate)

These are records, not code. Unless noted, each is created only when missing, so an admin's later
change survives.

### Custom fields on Marley and Frappe DocTypes (`krcs_` prefix)

| DocType | Fields | Set by |
|---|---|---|
| Patient | identifiers table, population group, residence, walk-in flag, registration facility, merge fields, name keys, override reason | `registration/setup.py` |
| Patient Encounter | visit link, department, presenting complaint, history, findings, management plan, diagnoses table, no-diagnosis reason, addendum, results and history panels | `clinical/setup.py` |
| Lab Prescription | `krcs_withdrawn` (lab rows withdrawn on Close Visit are flagged, never deleted; L4) | `clinical/setup.py` |
| Vital Signs | visit link, triage level, chief complaint, initial assessment | `visit/setup.py` |
| Medication Request | dispense status, quantity dispensed, hold status and reason | `pharmacy/setup.py` |
| Code Value | ICD-11 parent code, chapter, leaf flag | `coding/setup.py` |
| Healthcare Service Unit | facility code, county, sub-county | `setup/install.py` |
| All `CLINICAL_DOCTYPES` | `krcs_archived` in an "Archive" section at the end of the form | `security/setup.py`, re-anchored on every migrate |

### Property setters on Marley DocTypes

| DocType | Properties | Why |
|---|---|---|
| Patient Encounter | `appointment_type` default "KRCS Consultation" and hidden; `practitioner` read-only; hidden: `submit_orders_on_save`, Marley's diagnosis MultiSelect, the procedure, therapy and rehabilitation sections, the insurance fields, `order_history_html`, treatment plans; `codification_table` read-only | Marley requires an appointment type and practitioner we don't use. Orders go through our flow. Diagnoses are our ICD-11 table (D28, D30). |
| Patient | `quick_entry` off; `search_fields` plus the identifier search key | The full form runs the duplicate check. Patients are searchable by identifier. |
| Residence Location level options, naming series of 22 clinical DocTypes | (see `registration/residence.py` and `naming.py`) | Per-operation residence levels; per-site ID prefixes such as KLB- / DDB- (D19). |
| Billing fields on many Marley DocTypes (`security/matrix.py` `BILLING_FIELDS`) | `hidden=1`, `print_hide=1`; re-applied on every migrate | No charging (C-5). Mandatory fields are never hidden. |

### Direct row writes to Marley's standard documents

These are never saved through the Marley document, because in developer mode a save would export
into Marley's JSON files. They are re-applied on every migrate, so a Marley upgrade that reloads its
JSON gets them back.

| Target | Code | What |
|---|---|---|
| Workspaces Healthcare, Outpatient, Diagnostics | `visit/workspaces.py` | Department queue blocks, worklists and Visit Overview cards. Billing shortcuts and cards are removed by rule (D24). |
| Sidebars Healthcare, Diagnostics, Healthcare Setup | `desk_sidebar.py` | Rows claiming our DocTypes and reports (`key` starts with `krcs-`), so they open under Marley's sidebars (D25). |
| Marley's Dock (`healthcare`) | `desk_sidebar.apply_dock` | Rows for our own areas, each with its own sidebar and workspace shipped as module files, so they behave like Outpatient or Inpatient: **Pharmacy** after Diagnostics, **Outreach** after Rehabilitation, **MOH Reporting** after Outreach. |
| Companion dock | `dock/onerc_healthcare` (`mount_on: healthcare`) | Our app mounts on Marley's rail. Without it, opening one of our areas switched the desk to "our app", which has no rail, and Marley's icon rail disappeared. |
| Billing Workspaces (Accounting, Invoicing, Payments, Financial Reports, Selling, Buying, Stock, Insurance) | `security/setup.py` | A roles table limited to native ERPNext/Marley roles, so EMR users don't see them (C-5). |

### Permissions, settings and records

| What | Where | Why |
|---|---|---|
| Custom DocPerms on Marley/ERPNext DocTypes (role matrix, no delete on clinical DocTypes, no EMR role on billing DocTypes); marker-guarded fix-ups v2/v3 | `security/matrix.py`, `security/setup.py` | SDD §6.1 role matrix, NFR-9, C-5. Clinicians are read-only on Service and Medication Requests (no forged statuses). |
| Healthcare Settings billing-free values (first install) | `setup/install.py` | C-5. |
| System Settings (password policy, lockout, session expiry, 2FA) and Website Settings (sign-up off) | `security/setup.py`, first run | NFR-5, NFR-8, D16. |
| Log Settings: Activity Log and Access Log kept 3650 days | `security/setup.py`, first run | NFR-7. |
| Selling Price Lists disabled (buying kept) | `security/setup.py`, every migrate | C-5, D18. |
| "Lab Test Print" Print Format disabled; print removed on Service Request / Lab Test | `lab/setup.py`, `security/setup.py` | No paper lab request (LAB-07). |
| `consume_stock` off on catalogue Observation Templates | `lab/setup.py` | No stock movement (C-5). |
| Healthcare Settings `default_code_system` = ICD-11 (only while empty) | `coding/setup.py` | FR-11. |

---

### Phase 4 role-gate exception (not a Marley override)

`clinical.utils.has_any_role` accepts the user of an active `outreach.context.outreach_actor`. Only `outreach.sync` and the conflict replay enter that context, after checking the device grant and session membership. It lets a synced record pass the same consultation role gates as the desk, without the Outreach Team Member holding any clinical DocPerm (D58). `outreach/apply.py` lists every DocPerm skip it makes (`# DocPerm:` comments). Validations and controller hooks all still run.

### Medication dose interval (DEFECT-RX-1)

Marley's `DrugPrescription.get_quantity()` multiplies the daily dose by the course length only when the prescription has an interval. Our seeded Medications now have `default_interval = 1 Day` (`pharmacy/catalogue.py`, patch `medication_default_interval`). Without it, the prescribed quantity was one day's dose, and dispensing a full course was refused.

---

## 8. Marley internals we call directly

These Marley functions are imported by our code instead of going through their HTTP endpoint. A Marley
upgrade that changes their signature or behaviour breaks us.

| Our code | Marley functions |
|---|---|
| `lab/orders.py::ensure_observations` | `service_request.create_sample_collection`, `service_request.insert_diagnostic_report`, `observation.new_observation`, `observation_template.get_observation_template_details`, `sample_collection.create_component_observations`, `sample_collection.set_component_observation_data`, `healthcare.utils.create_non_sample_observations`, `healthcare.utils.has_sample_required_component` |
| `lab/api.py::get_encounter_results` | `observation.ensure_result_flag`, `observation.get_child_observations` |
| `clinical/consultation.py` | `PatientEncounter.make_service_request()` and its submit path (`on_submit` creates the Medication Requests) |
| `clinical/guards.py`, `lab/guards.py` | the original endpoint functions they wrap or delegate to |
| `outreach/apply.py` (Phase 4) | `observation.new_observation`, `sample_collection.update_collection_status`, `PatientEncounter.make_service_request()`, and `DrugPrescription.get_quantity()` (via the encounter submit). A point-of-care result is recorded the way Marley's `collect_sample` / `create_observation` do it, without their Sample Collection DocPerm check (D58). |

---

## 9. Upgrade checklist

After upgrading Frappe or Marley, on the test site:

1. **Run the full test suite** (`scripts/run_tests.sh`). These tests guard the overrides above:
   - `test_state_machines`
   - `test_lab` (including the Marley first-order regression marker)
   - `test_privacy` (read-log wrappers, File hook, extract)
   - `test_security` (role matrix, delete guard, billing)
   - `test_sidebar`, `test_boot` (navigation)
   - `test_http_methods` (JavaScript ↔ server HTTP verbs)
2. **Compare the signatures** of every overridden Frappe function in section 1 (`frappe.client.get`,
   `get_value`, `get_list`, `validate_link_and_fetch`, `reportview.get`, `reportview.get_list`,
   `reportview.export_query`, `query_report.export_query`, `printview.get_html_and_style`,
   `data_import.download_template`, `make_access_log`) and Marley's `observation.set_observation_status`,
   `approve_all_observations` and `reject_all_observations` with our wrappers in `privacy/read_log.py` and `privacy/access_log.py`. Copy any
   new parameter or decorator.
3. **List Marley's new `@frappe.whitelist` functions** on Patient Encounter, Service Request, Medication
   Request, Sample Collection and Observation. Any new function that inserts or submits with
   `ignore_permissions` needs a guard in section 1.
4. **Check that Marley still**:
   - stops after the first lab order in `make_observation`; if fixed upstream, `ensure_observations`
     can be simplified;
   - adds the practitioner User Permission;
   - calls `delete_doc` on cancel paths.
5. **Run `bench migrate`** and check that the workspace blocks, sidebar rows and billing-field
   property setters are back, by opening Healthcare, Outpatient and Diagnostics as a Clinical Officer.
6. **Check that `frappe.desk.form.load.getdoc` still runs `onload`** through `run_method`; our open
   logging depends on it.
