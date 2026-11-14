# Decisions: KRCS EMR (`onerc_healthcare`)

Numbered rulings. Add new ones at the end; never renumber. Requirement IDs refer to BRD KRCS-DRO-BRD-001 v1.0 and SDD v0.1.

**D1. Platform.** Frappe + ERPNext + Frappe Health (Marley) with the custom app `onerc_healthcare` (the SDD's working name was `krcs_emr`).

**D2. Versions.** Everything on version-16: Frappe 16, ERPNext 16, Marley `version-16`. The earlier 17-dev bench, and its bypass of Marley's version guard, are abandoned.

**D3. One site per operation.** `kalobeyei` and `dadaab` each get their own Frappe site, database and single company (`KRCS Kalobeyei` / KLB, `KRCS Dadaab` / DDB). Facilities are Healthcare Service Unit groups inside their operation's site. A site's operation comes from site config `krcs_operation`. (SDD v0.1 §3.2; BRD BR-8, C-7.) The single-site, company-per-operation design and SDD v0.2 are discarded, so Patient needs no Company field.

**D4. No billing.** Nothing is billable (BRD C-5). Healthcare Settings are set billing-free on first install, Observation Templates are non-billable, and billing doctypes are locked away from clinical roles.

**D5. Diagnostics route.** Use Marley's Observation Template, Sample Collection and Diagnostic Report, not the legacy Lab Test Template. The starter lab catalogue is **provisional**: it has no reference ranges and is flagged for Lab Technician review (OI-9). Result approval stays off until OI-9 is decided.

**D6. Patient Portal.** Marley's Vue patient portal includes a Payment screen. We create no Patient-role users, and the portal is to be hidden (pending user confirmation).

**D7. Git.** Local commits on `feat/emr-phase-1`, one per workstream, no push.

**D8. Patient Visit workflow.** A Python state machine (`visit/engine.py::transition`), not a Frappe Workflow fixture. This follows the team convention from `krcs_risk_management`.

**D9. Triage.** Triage is recorded on Marley's Vital Signs, extended with `krcs_` fields, and linked to the Patient Visit.

**D10. Fiscal year.** The KRCS fiscal year is 1 July – 30 June. The setup wizard is run from code with FY 2026-07-01 → 2027-06-30. Confirmed by the user on 2026-11-13.

**D11. ICD-11 source file.**
- The WHO ICD-11 MMS 2025-01 *SimpleTabulation* file is **bundled in the repo**: `onerc_healthcare/coding/data/`, gzip-compressed and otherwise unmodified, with WHO's readme and a `NOTICE.md` that records the source, SHA-256 and CC BY-ND 3.0 IGO licence. A test checks the checksum.
- `coding.setup.install()` imports it automatically on install and migrate, once per site and release (default key `krcs_icd11_release`).
- `coding/icd11.py::import_file(path)` remains available for loading other releases.
- The user first chose to keep the file outside the repo (2026-11-13), then reversed that the same day.

**D12. Facility codes.** NTK, NRG, IFO1 and IFO2 are internal provisional codes, not Master Facility Registry codes (OI-3). The walk-in ID format `<FACILITY CODE>-<YYYY>-<#####>` is configurable, pending OI-13.

**D13. Test isolation.** Automated tests run only on `emr-test.localhost` (operation `kalobeyei`, `allow_tests` on), through `scripts/run_tests.sh`. All DB work is serialised by `scripts/db_lock.sh`.

**D14. Identifier uniqueness.** UNHCR ID, Proof of Life Number and Alien ID each belong to one person and are unique per site. Ration Card Number is a household identifier and is not unique, so family members may share it. National ID and the facility-generated walk-in ID are also unique. The rule is set per type by `Patient Identifier Type.is_unique` and enforced by a DB unique index that applies only to unique types (FR-2). Confirmed by the user on 2026-11-13.

**D15. EMR System Administrator: user management and accepted residual risk.** (FR-39, SDD §6.1)
- **What it may do:** manage users without holding System Manager. It may grant or revoke only the EMR roles, directly or through Role Profiles.
- **What it may not do:**
  - change its own access
  - change the access, email or password of any account holding another role (System Manager, Healthcare Administrator, Physician, accounts roles)
  - be given System Manager, ever
- **Accepted residual risk:** resetting passwords of EMR clinical users is part of its job, so it could reach clinical content through such an account. This is mitigated by:
  - the Medical In-Charge's written request
  - Frappe's role-change permission log
  - the FR-40 access log (Phase 2)

**D16. Security settings ownership.** (NFR-5, SDD §6.3)
- The EMR System Administrator has read-only access to System Settings. The password policy, lockout, 2FA and session expiry are changed only by a System Manager.
- The inactivity timeout is set on the Medical In-Charge's request. It starts at 8 h.
- 2FA is enforced for the EMR System Administrator and HIS Officer roles.

**D17. Delete scope.** (NFR-9)
- Delete is removed from clinical DocTypes (`CLINICAL_DOCTYPES`) for every role, including System Manager. A server-side `on_trash` guard also refuses it.
- Admin and master DocTypes keep their standard delete for System Manager and Healthcare Administrator.
- EMR roles hold delete nowhere.
- ERPNext's bulk "Delete Company Transactions" is closed in three ways:
  - a hook refuses Transaction Deletion Record
  - Company delete is removed
  - clinical DocTypes are listed in `company_data_to_be_ignored`

**D18. Price lists.** (C-5) Only selling price lists are disabled. Buying price lists stay available for future pharmacy procurement.

**D19. Record ID series per operation, set in EMR Naming Settings.**
- Each site is its own database, so Marley's shared prefixes (HLC-PAT-, HLC-ENC-, HLC-VTS-, ...) would give Kalobeyei and Dadaab identical record IDs. Every clinical record type therefore gets a series that starts with the site's **ID Prefix** (KLB / DDB), e.g. `KLB-PAT-2026-00001`, `DDB-ENC-2026-00001`.
- **EMR Naming Settings** (`/app/emr-naming-settings`, System Manager and EMR System Administrator) holds the prefix and one rule per record type. It covers the 16 record types listed in `onerc_healthcare/naming.py::DEFAULT_RULES`: Patient, Patient Visit, Encounter, Vital Signs, Observation, Sample Collection, Diagnostic Report, Lab Test, Medication Request, Service Request, Clinical Procedure, Inpatient Record, Discharge Summary, Medication Administration, Emergency Record and Patient Appointment.
- Saving applies the rules as property setters on each record type's `naming_series` field. Existing records keep their IDs.
- The settings are seeded once per site on install or migrate, after which only the page changes them. Use the page rather than Document Naming Settings, because the page re-applies its own rules.
- Patient Visit moved from `format:PV-{YYYY}-{#####}` to a naming series so it can be configured too.
- Marley's Emergency Record names its patients `ER-.YYYY.-` in code, so that option stays available on Patient.
- Clinical Note is named `{patient}-{reference}-#####`, which already contains the prefixed Patient ID, so it needs no rule.
- Walk-in identifiers are configured separately in EMR Registration Settings.
- Set on 2026-11-13, before any clinical record existed.

**D20. Branch strategy.**
- `main` holds released code only; each production deployment is a tag on `main`.
- `develop` is the integration branch.
- Work happens on `feat/<topic>` or `fix/<topic>` branches that merge into `develop` by pull request after review. `develop` merges into `main` for a release.
- Phase 1 lives on the local branch `feat/emr-phase-1`. It is pushed and opened as a PR into `develop` once `develop` and `main` exist on the remote. Today the remote has only `feat/patient-registration`.
- Commits stay local until the owner decides to push.

**D21. Scheduler.**
- The scheduler is enabled on the operation sites (kalobeyei, dadaab), and background workers run under `bench start` in development and Supervisor in production.
- `emr-test.localhost` keeps automated tests isolated: Frappe's test runner disables the scheduler during runs.

**D22. Strict user permissions.**
- System Settings "Apply strict user permissions" stays **off** for Phase 1. Separation between operations comes from separate sites (D3), and no User Permissions are used inside a site yet.
- Revisit together with SEP-14 if OI-12 decides that users see only their own facility's records.

**D23. Inpatient unit types.**
- Healthcare Service Unit Types **Ward** and **Bed** are seeded on every site: inpatient occupancy, not billable (C-5).
- The wards and beds themselves are created per facility when inpatient care is scoped (Wave 2, OI-18).

**D24. Department queues live on Marley's workspaces.** (FR-8, BR-3)
- The EMR has no workspaces of its own. Each department's queue is a labelled block (header, shortcuts, quick list) on one of Marley's pages:
  - **Healthcare:** Reception, Triage, Pharmacy and Health Information queues, plus the Visit Overview number cards.
  - **Outpatient:** Outpatient, Maternal and Child Health, Mental Health and Nutrition queues.
  - **Diagnostics:** Laboratory queue.
- `visit/workspaces.py` applies this on every migrate. It writes child rows and content directly and never saves Marley's workspaces, so nothing is exported into Marley's files even in developer mode.
- The changes are idempotent and restored after a Marley upgrade.
- Billing items (Sales Invoice shortcuts, "Appointments To Bill" cards) are removed from these pages by rule (C-5).
- If an admin customises one of these pages in the desk, Frappe shows the admin's saved layout instead, and our blocks stop showing.
- Decided by the user on 2026-11-13, replacing the 10 separate EMR workspaces.

**D25. EMR record types open under Marley's sidebars.**
- Frappe 16 shows a record type under the sidebar that claims it (`is_default_module`), and otherwise under its own module's sidebar. Our modules have no pages (D24), so Patient Visit opened under a bare "EMR Visit" sidebar.
- `onerc_healthcare/desk_sidebar.py` adds rows to Marley's sidebars:
  - **Healthcare:** "Patient Visit", right after "Patient".
  - **Healthcare Setup:** a "KRCS EMR" section with EMR Registration Settings, EMR Naming Settings, Patient Identifier Type and Residence Location.
- The rows are written directly, with `key` starting `krcs-`. Marley's sidebars are never saved, so nothing is exported. The rows are re-applied on every migrate and restored after a Marley upgrade.
- Requested by the user on 2026-11-13.

**D26. Marley's workspaces open under their own sidebars.**
- All eight of Marley's sidebars belong to one module, "Healthcare". Frappe 16 lists every workspace of a module on each of that module's sidebars, and the desk opens a workspace under the first sidebar listing it. So the Healthcare app icon (`/desk/healthcare`) opened the Healthcare page under the Diagnostics sidebar.
- Our `boot_session` hook (`onerc_healthcare/boot.py`) narrows each Marley sidebar's workspace list to the workspaces its own rows link: Healthcare → Healthcare, Diagnostics → Diagnostics, Setup → Healthcare Setup, and so on.
- It only adjusts the boot payload; nothing is stored, and other apps' sidebars are untouched.
- **Limitation:** after an admin rearranges a sidebar in the desk, the client reloads sidebars without this hook, and the old behaviour returns until the page is reloaded.
- **Upstream:** this is a Frappe 16 / Marley v16 issue worth reporting.
- Reported by the user on 2026-11-13.

**D27. Routing rules after triage.** (FR-9, SDD §4.4)
- **Routing targets:**
  - `route` and `reroute` refuse **Reception** and **Triage**. These are intake steps, not destinations, and a visit routed "to Triage" showed in no queue.
  - **Re-route** (Triage Nurse) changes the department of a routed visit before consultation starts.
  - **Return to Triage** (Triage Nurse, Clinical Officer or Mental Health Officer, with a reason) sends a routed visit back to In Triage before consultation starts.
- **Vital signs before routing:**
  - `route` needs **submitted triage vital signs** linked to the visit, which carry the triage level and chief complaint.
  - **Emergency override:** a Triage Nurse may route without vitals by giving a reason. The reason is logged as "Routed without vital signs: …", and the visit gets a protected flag `routed_without_vitals` so the Medical In-Charge can review these cases.
  - The flag is sticky: it stays set even if the visit is later returned, triaged and routed normally.
- **Correction on kalobeyei:** the mistaken visit KLB-PV-2026-00009 (Mary Akuru, routed to Triage) was returned to In Triage by Administrator with a logged reason.
- Decided by the user on 2026-11-13.

## Phase 2 (Clinical), 2026-11-13

**D28. Consultation flow.**
- A consultation is a Patient Encounter linked to its visit (`krcs_visit`), with one active encounter per visit.
- It is started, sent and completed only through `clinical.consultation`:
  - Start creates or reopens the encounter.
  - Send Lab Orders releases the lab orders.
  - Complete applies the diagnosis rule, releases prescriptions and completes the visit.
- Desk Submit is refused unless the flow submits. Marley's order-creating endpoints are blocked.
- Clinical Officers and Mental Health Officers may consult in any department.

**D29. No encounter cancel or amend.**
- Amending would duplicate every order, and cancelling would orphan prescriptions.
- Corrections go in an append-only, allow-on-submit addendum.
- Cancel and amend are refused in the permission hook, before Marley's `before_cancel` runs.

**D30. ICD-11 diagnoses.**
- Diagnoses live in the custom child table EMR Encounter Diagnosis: ICD-11 code, New/Follow-up flag, exactly one primary.
- Chapter X (extension codes) is excluded. Leaf codes rank first in the picker; parent codes are allowed with a warning.
- The table rebuilds Marley's `codification_table`.
- A consultation cannot be completed without a coded diagnosis unless a reason is given (FR-11).

**D31. Completing with pending results.**
- A consultation may be completed while lab results are pending, but a reason is required.
- The visit stays Awaiting Laboratory until the results arrive or the visit is closed.

**D32. Close Visit.**
- Any open visit can be closed as Discharged with a reason category and a comment, for example "patient left before being seen".
- A patient may have only one open visit at any date. Registering a new visit offers to close the previous one.

**D33. Referral.**
- Referral is its own record (Draft → Sent → Received/Completed/Lost to Follow-up, or Cancelled while still Draft).
- An internal referral opens a new visit routed straight to the destination department, skipping triage.
- Referral is possible while lab or pharmacy tasks are open; the referred visit keeps those tasks.
- Referral Facility is provisional until the MFR list (OI-3).

**D34. Laboratory.**
- Marley's `make_observation` stops after an encounter's first order. Our `lab.orders.ensure_observations` provisions every order.
- Lab completeness is computed from approved Observations, never from Service Request status, which Marley sets to "completed" at sample collection.
- Approval is single-step by the Lab Technician (provisional, OI-9).
- A rejected result reopens the lab task.

**D35. Pharmacy.**
- Medicines are non-stock items from a provisional catalogue (OI-9). No prices, no ledger.
- Dispensing Record covers each prescription line: partial dispensing, not dispensed with a reason, substitution with a reason.
- The line state is mapped onto Marley's status. The visit's `dispensed` event is raised when every line is resolved.
- Clinicians read prescriptions but cannot write them, so they cannot forge a status.

**D36. Visit events.**
- Lab, pharmacy and referral code raise visit events only through `visit.tasks.raise_event`. It does nothing (and adds a comment) when the visit's state does not allow the event, so a clinical record is never rolled back.
- A scheduled reconciler runs every 10 minutes, and the Medical In-Charge has a "Recheck Tasks" button.

**D37. Consent and data access.**
- Consent statements are versioned, and the provisional EN-1.0 wording is pending OI-14.
- Missing consent produces a warning at visit registration, never a block.
- Data Access Requests follow Received → Identity Verified → Approved/Rejected → Response Ready → Closed.

**D38. Access Log Entry.**
- Append-only. The patient is stored as Data so that merges never rewrite it.
- Desk record opens are written after the response, because GETs roll back.
- Prints, exports and reports are mirrored from core Access Log.
- Only the Medical In-Charge and the EMR System Administrator can read it (metadata only).

**D39. Screening and visitors.**
- Screening parameters are configured by the Medical In-Charge. Temperature defaults to flagged above 37.5 °C (provisional).
- The Nurse Receptionist and the Triage Nurse both screen.
- Visitor Record goes Inside → Exited.
- None of these records is ever deleted. The Head of Operations sees daily counts only.

**D40. Practitioners and Marley endpoints.**
- Marley's Healthcare Practitioner User Permission (applied to all doctypes) is stripped, consistent with D22.
- Marley endpoints that skip permission checks are overridden or blocked.

## Phase 2 review fix round, 2026-11-14

**D41. Lock order and locking reads.**
- Lock order is Visit → Encounter → Observation / Medication Request / Diagnostic Report, and every read after a lock is itself a locking read.
- Frappe locks an Observation before any hook runs. Lab approval hooks therefore take the visit lock with NOWAIT: when another lab update holds the visit, the user is asked to retry immediately rather than waiting.
- Marley's bulk and panel approve/reject endpoints are overridden to lock the visit first.
- Under MariaDB snapshot isolation, a rare race surfaces as a "record changed, retry" error. It never corrupts data.

**D42. Engine-internal visit actions.**
- `refer`, `start_consultation` and `complete_consultation` run only from the consultation and referral code (`api_only`). `apply_action` refuses them for every role.
- `discharge` is a server-only event. Complete Consultation and Close Visit cover the user-facing cases.

**D43. Visit comments and history.**
- Status-log comments are at permlevel 2, readable by Triage Nurse, Clinical Officer, Mental Health Officer and Medical In-Charge. Triage keeps access because "Return to Triage" reasons are written for them.
- Patient Visit has `track_changes` off, because Version diffs leaked comments to every reader. The status log is the audit trail (D8).
- A Data Access Request correction therefore cannot target a Patient Visit. Visit corrections are logged engine actions; Phase 3 adds `annotate`.

**D44. Read-access logging scope (FR-40, NFR-7).**
- **What is logged:** every read of an `ACCESS_LOGGED_DOCTYPES` record, one entry per call:
  - desk opens, the history panel, `frappe.client.get` / `get_value` / `get_list`, list and report views, link fetches, print preview and exports;
  - REST and API v2 single and list GETs, through `after_request`.
- **Not logged:** Administrator.
- **Refused:** bulk Data Import export of clinical records, except for System Manager.
- **Extracts:** Data Access Request extracts are never served as files, only through the logged `download_extract` (HIS, MIC). Other request attachments, such as authorisation scans, follow normal rules; the MIC has write on the request so it can attach them.

## Phase 3: MOH reporting and data-capture gaps, 2026-11-14

**D45. Everything MOH is provisional (OI-5).**
- The 705A/705B category lists, their ICD-11 mappings, MOH 717 and the MOH 711 rehabilitation and social work section are seeded as "Active - Provisional". Exports carry a PROVISIONAL banner.
- MOH 711 RH-MCH, 747A, 643B and the Mental Health Clinic Report exist as **Blocked** definitions with no elements:
  - 711 RH-MCH waits for OI-5.
  - 747A and 643B wait for OI-5; their FCDRR forms also need stock, which conflicts with C-5.
  - The Mental Health Clinic Report waits for OI-24.
- Elements the EMR cannot capture yet are "Gap" elements: shown blank with a note, never computed.

**D46. Seeds never overwrite HIS edits.**
- Report definitions carry a `seed_hash`. Categories and mappings carry a "Seed v1" marker plus a hash.
- A seed updates a row only while the row still matches the last seed. Rows created in the desk are never touched.
- A seed that fails validation is skipped and logged; it never breaks a migrate.

**D47. MOH 705 counting rule** (user decision 2026-11-13, R2):
- **What counts:** New diagnoses only, primary and secondary.
- **Once per episode:** each category counts once per encounter, and an internal-referral chain counts as one episode. This applies at the earliest encounter in the chain that has the category, and a category already counted in an earlier period is not counted again.
- **Group priority:** confirmed malaria needs an approved positive RDT or slide on the same encounter, otherwise the case is clinical malaria. A specific injury beats "Other injuries".
- **Unmapped codes** go to "All other diseases" and appear on the exceptions list.
- **Age** is age at the encounter date: under 60 months goes to 705A, 60 months and over to 705B.
- **Missing date of birth:** the diagnosis is counted on neither form and listed as an exception.
- **Wrong form:** a category not on the patient's form (e.g. Hypertension for a child under 5) goes to "All other diseases".
- **Monthly totals:** no day-of-month columns.

**D48. Mapping precedence:** Exact code > longest Prefix > deepest ICD-11 Block > catch-all.
- Blocks come from the WHO 2025-01 Grouping1–5 columns.
- The primary tabulation is stored for information only.
- Categories with no ICD-11 equivalent are Gap elements and remain OI-5 questions: diarrhoea dehydration grade, severe pneumonia, dog vs other bites, snake bites.

**D49. Base filters for every MOH count** (`reporting/filters.py`):
- **Excluded:**
  - visits closed as "Registered in error" or "Superseded by a new visit";
  - archived visits and encounters;
  - patients merged into another record;
  - draft and cancelled encounters;
  - anything the reserved `krcs_report_exclusions` hook excludes (migrated SmartCare history, FR-43, waits for OI-4).
- **Not excluded:** archived *patients*, because the care was given.
- **Ages:** in completed months, computed identically in SQL and in Python (29 February: 11 months on 28 Feb of the next year).

**D50. Attendance.**
- **Rule:** a visit is "New" when it is the patient's first visit to this facility in the calendar year. Otherwise it is a "Re-attendance".
- **Reception override:** "Seen here before (SmartCare)", ticked at registration, makes a would-be New visit a re-attendance. Settings can switch this off.
- **Corrections:** the receptionist may correct attendance while the visit is Registered, and the HIS Officer at any time, with a reason, through `engine.annotate`. The Medical In-Charge cannot correct attendance.
- **Internal referrals:** a visit created by an internal referral is a re-attendance.
- **Department attribution (MOH 717):** a visit counts for the department it was last routed to. Internal-referral visits count for the receiving clinic but not again in the facility totals. "Left before being seen" is excluded from attendance, as a rule setting the HIS Officer can change.

**D51. Visit outcome.**
- **Values:** Discharged home, Admitted, Referred out, Referred internally, Died, Left without completing, Not applicable.
- **Recorded by the closing action:**
  - Complete Consultation: Discharged home by default, Admitted or Died.
  - Refer: Referred out or Referred internally, from the referral type.
  - Close Visit: implied by the closure reason; with "Other" the user chooses.
- **Close Visit after completion:** a later Close Visit keeps the consultation's outcome unless the reason is Died.
- **Death:** "Died" (outcome or closure reason) needs Clinical Officer, Mental Health Officer or Medical In-Charge.
- **Closed visits before Phase 3:** outcomes are back-filled from their history by a patch.

**D52. Estimated date of birth.**
- New patients need an exact DOB or an estimated age.
- With an estimated age, DOB = estimate date minus the age (same day of month, clamped at month end), and the record is flagged "DOB estimated".
- An exact DOB entered later clears the flag. Existing patients without a DOB are asked for one only when the DOB fields are changed.
- The duplicate check widens to ±2 years when either DOB is estimated.

**D53. Service Delivered and Community Activity Record.**
- **Service Delivered:**
  - It is a separate DocType, not a child table of Patient Visit, which would show social-work details to every visit reader. It is access-logged.
  - The Clinical Officer or Mental Health Officer records it. The Medical In-Charge edits and voids. It is voided with a reason, never deleted.
- **Community Activity Record:**
  - It is submittable. Cancelling needs a reason.
  - Sessions and participants feed MOH 717.

**D54. MOH Report Run.**
- **Lifecycle:** a status lifecycle (D8 pattern): Draft → Generated → Submitted → Superseded, plus Void from Draft or Generated with a reason. Revise needs a reason and creates a new Draft that supersedes the submitted run.
- **One active run** per report, facility and period.
- **Snapshot:** a generated run stores a snapshot (aggregates and fingerprints of the definition and mapping). Exports and the API read only the snapshot. A Submitted run is immutable.
- **Generation:** synchronous, or on the long queue for ranges over 92 days.
- **Periods:** calendar months and quarters (Q1 = January to March, as in DHIS2), ending no later than today.
- **Who sees what:**
  - Exceptions hold record IDs only and sit at permlevel 1 (HIS Officer, Medical In-Charge).
  - The Head of Operations reads runs (aggregates) only.
  - `track_changes` is off, for the same reason as D43.

**D55. Aggregate API and the MOH Data Reader role.**
- `reporting.api` has three methods, all GET only: `list_reports`, `list_runs` and `get_report_values`.
- Allowed roles: HIS Officer, Medical In-Charge and the API-only role "MOH Data Reader". The role has no desk access and no users until the DHIS2 link exists.
- Calls are rate-limited and access-logged. The Head of Operations is refused.
