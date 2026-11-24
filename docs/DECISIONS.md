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

**D56. MOH reconciliation (TST-13, SDD §9.3), 2026-11-14.**
- **The dataset.** `tests/test_moh_reconciliation.py` builds a prepared dataset through the real flows: 36 patients, 43 visits and 39 encounters in March 2025, with edge records on 28 February and 1 April. It checks MOH 705A, 705B, 717 and 711 Rehab against counts worked out by hand from D47–D53.
- **Readings of rules that were ambiguous:**
  - Morbidity is dated by the encounter date and attendance by the visit date.
  - An archived encounter keeps its visit's attendance.
  - "Left before completion" is still an attendance; only "Left before being seen" is removed.
  - The missing-DOB exception appears on both 705 runs.
- **Defect fixed: dispensing from an archived encounter.** Dispensing counts now exclude dispensing from archived or unsubmitted encounters (DEFECT-RECON-1).
- **Defect fixed: back-entered visits.**
  - Attendance compares only *earlier* visits: an earlier date, or the same date registered first.
  - When a visit is back-entered, for example by an outreach sync, later visits that year that were "New" by the rule become re-attendances through `engine.annotate`. Overrides are left alone (DEFECT-RECON-2).

**D57. User answers, 2026-11-14.**
- **Voiding a revision restores the original.** When a revised MOH run is voided, the run it superseded becomes Submitted again. The log action is `restore`, so a period never loses its submission.
- **MOH 705, missing DOB:** counted on neither form and listed as an exception (D47 confirmed).
- **National ID** stays a non-primary identifier, so Kenyans with only a National ID register as walk-ins (C-6).
- **Patient portal:** left as is for now.
- **SpO2:** triage records SpO2 (%) on Vital Signs (`krcs_spo2`, Data like Marley's other vitals), checked to be 50–100.
- **Service Delivered:** the Medical In-Charge may record services as well as edit and void them.
- **MOH 717 age bands:** <5, 5–59 and 60+. Still provisional until the HIS Officer confirms (OI-5).
- **Emergency patient IDs:** Marley's provisional patients (`ER-2026-…`) get the site prefix (`KLB-ER-2026-…`) through a Patient `before_insert` hook. The old `ER-` series stays a valid option.
- **Visit Overview cards** stay visible to every role that sees the Healthcare page; they show counts only.
- **"Left before being seen"** stays excluded from MOH 717 attendance.
- **`routed_without_vitals`** stays sticky (D27).
- **Housekeeping deletions** (old Version rows, `smoke.localhost`, the old bench): none for now.
- **The user's visit KLB-PV-2026-00001:** the attendance set by the back-fill was cleared at the user's request (direct write, `modified` unchanged).

**D58. Phase 4 Outreach: user answers (OI-10, OI-11) and the contract, 2026-11-14.**
- **National systems** (Phase 5: FHIR and registries, SHA, the outbox) are skipped for now, at the user's request.
- **Offline session (OI-10):** 7 days of PIN-only use after the last online sign-in. After that the app needs an online sign-in. The setting is `offline_session_days` in EMR Outreach Settings.
- **In the field (OI-10):**
  - The team may record a prescription as dispensed on the spot. It syncs as a Dispensing Record, so the facility pharmacy does not dispense it again.
  - The team may enter point-of-care results for templates flagged `krcs_point_of_care`. The provisional list is MRDT, haemoglobin, random blood sugar, urine pregnancy test and HIV 1/2 rapid test.
  - An approved positive MRDT counts as confirmed malaria.
  - Other lab requests go to the facility lab worklist.
- **Package (OI-10):**
  - Demographics, identifiers, allergies, the last 3 visits' diagnoses and active prescriptions, for patients living in the session's catchment area.
  - Mental Health encounters and sensitive services are never included. Mental Health is never offered offline (OI-7).
  - Each patient sent is written to the Access Log ("Outreach package").
- **Purge:** the device deletes a record 7 days after the server acknowledges it. Failed, held and conflicted records are never purged.
- **Conflicts (OI-11):** resolved by the HIS Officer, with the Medical In-Charge as backup.
- **Who consults:** any Outreach Team Member may consult and prescribe (the user's choice). The member becomes the encounter's practitioner.
- **MOH reports:** outreach visits count for the session's facility, flagged `is_outreach`.
  - MOH 705 counts them.
  - MOH 717 has its outreach lines; facility attendance excludes outreach by default.
- **No DocPerms for the Outreach role.** `desk_access=0` does not block `/api/resource`, so a DocPerm would expose patients to a tablet account. Instead:
  - Records arrive only through `outreach.sync`.
  - The sync checks the device grant and session membership.
  - It then applies each record through the desk pipelines inside `outreach.context.outreach_actor`. While that context is active, the clinical role gates accept that one user.
  - The Outreach Team Member was added to `NO_DESK_ROLES` (security fixups v4).
- **Sync design:**
  - **Records:** each record is one offline unit. A finished visit is a single `visit.capture` bundle (vitals, consultation, labs, POC results, prescriptions, dispensing, outcome), applied in one savepoint.
  - **Idempotency:** on the client UUID.
  - **One result per record:** accepted, duplicate, failed, held or conflict.
  - The wire contract is `docs/OUTREACH_PROTOCOL.md`.
- **Devices:**
  - **Grant:** enrolment issues a per-user, per-device, per-session grant whose secret is stored hashed. Sync uses `Authorization: krcs-outreach <grant>:<secret>` through `auth_hooks`, which accepts it only on the sync and package endpoints.
  - **Data key:** the device's data key is escrowed on the grant, so a forgotten PIN (5 wrong attempts) never loses records; an online sign-in restores the key.
  - **Lost device:** revoking a device ends its sync.
- **Frontend:** React 18 + TypeScript + Vite PWA at `/outreach` (the user's choice, replacing the earlier Vue + frappe-ui note). It is built by `bench build` from the app's root `package.json`.

**D59. Phase 4 review, audit and fix round, 2026-11-14 to 2026-11-16.**
- **Closed sessions still deliver their records (NFR-11).**
  - **What Closed means:** no new device and no new package.
  - **What still works:** a device's grant keeps working for `sync.push` and `sync.status` until it expires, and `enrol` can renew an existing grant for the same user and device.
  - **When a session can be closed:** not while it has Failed, Held or Conflict records.
  - **When it can be cancelled:** only before any record has been received.
- **Failed records:**
  - the device can correct a failed record and resend it under the same client UUID;
  - Accepted, Held, Conflict and Discarded records are never re-applied.
- **Field dispensing:**
  - the app limits the quantity to the prescribed course, worked out as doses per day × days (from the new `dosage_units` and `period_days` in the reference data);
  - a field dispense is final, so the facility pharmacy does not dispense the line again.
- **Demographic conflicts are per field.** A changed field conflicts only when the facility changed it after the package was downloaded (`base_values`) and the value differs from the device's. Accepted updates return the patient's new `modified`, so edits made on the same device chain.
- **Scope.** A session may update, or record visits for, only patients in its catchment area or patients it registered or linked.
- **Robustness:**
  - a deadlock affects only the record involved;
  - held records are replayed until no more can be applied, and swept on each push or status call;
  - losing the Outreach role ends sync;
  - a disabled author stays the practitioner when the HIS Officer decides later (DEFECT-P4-4).
- **Accepted risk (review M5).** Someone with a copy of a lost tablet's storage could try every 6-digit PIN offline. The app's PIN protects the app only. The controls are:
  - the tablet's own storage encryption and screen lock (a deployment requirement);
  - the package being limited to one catchment area;
  - the 7-day offline session;
  - prompt revocation (`docs/outreach-lost-device.md`).
- **The cached `/outreach` page** carries no CSRF token or user.
- **DEFECT-RX-1 (found by the Phase 4 smoke test).** Seeded medications had no dose interval, so Marley's prescribed quantity was one day's dose and a full course could not be dispensed at the desk. Every medication now gets a daily interval: through the seed, and through the patch `medication_default_interval`.
- **Demo data.** `setup/demo.py` adds:
  - an outreach user, Akai Lotukoi;
  - the catchment area "Village 3 (demo)" with three fictional residents;
  - a planned session (KLB-OUT-2026-00001 on kalobeyei).

**D60. Maternity (Kalobeyei labour ward), 2026-11-16.** BRD §3, SDD §5.12.1 (Wave 1) and §5.12.3. Built from the **Dadaab maternity templates**; it will be adjusted when the Kalobeyei forms arrive (OI-19 is still open).
- **User answers:**
  - **Midwife role (ROL-24):** a new Midwife role owns the labour ward records. Clinical Officers and the Medical In-Charge can also act; HIS and MHO read.
  - **Newborn (OI-23):** each live-born baby is registered at delivery as their own Patient, linked to the mother (`krcs_mother` plus a Patient Relation "Mother" row): first name "Baby", exact date of birth, a walk-in facility ID. Twins are allowed through the duplicate override "Newborn registered at delivery". Stillbirths are recorded on the delivery only.
  - **Partograph (OI-23):** hourly entry at the bedside, with a live chart. The alert line runs at 1 cm/h from the first dilatation of 4 cm or more (WHO modified partograph), and the action line is 4 h to its right. Warnings: crossing either line, FHR below 110 or above 160, meconium or blood in the liquor, moulding +++, BP 160/110 or more, temperature 38 or more.
  - **Admission:** normally reception → triage → routed to **Maternity** → the Midwife admits. A **fast path** ("Admit in Labour" on the Patient form) opens the visit behind the scenes. A visit a clinician closed with outcome Admitted is linked; an internal referral to Maternity is received.
- **Marley's Inpatient Record is not used.**
  - Why: every save looks up selling prices, which are disabled (D18), so it crashes; its admit dialog needs a price list; and it carries bed-rent billables and stock settlement (C-5).
  - Instead: our **Maternity Admission** (IP number `KLB-MAT-YYYY-#####`) is the admission record.
  - **Where it sits (user, 2026-11-16):** inside Marley's **Inpatient** area, with the Inpatient Record parts hidden there (record, IP medication order/entry, treatment counselling, discharge summary, billing links, and its Home-page cards and charts). Mothers waiting for admission and newborns are under **Outpatient**. There is no separate Maternity area.
- **Visit and stay:** the visit closes at admission (outcome Admitted; MOH 717-E02). The stay lives on the admission: Admitted → In Labour → Delivered → Discharged, with an append-only log.
  - **Discharge outcomes:** discharged home, referred out, died, absconded. Died needs a cause and a Midwife, Clinical Officer or Medical In-Charge (`DIED_ROLES`).
  - **Corrections:** the Medical In-Charge archives a wrong admission; nothing is deleted.
  - **Ward observations:** Vital Signs linked to the admission.
- **Records stay fixed:**
  - Partograph rows are append-only (a correction is a new row with a note). Dilatation, contractions, oxytocin and urine are stored so that a blank (not recorded) differs from 0.
  - A submitted Delivery Record cannot be cancelled; it takes an addendum.
- **Beds:** a "Maternity Ward" group under each facility (seeded). The EMR System Administrator adds beds. Occupancy is set by locked direct writes and freed on discharge or archive.
- **MOH 717 (provisional, OI-5):** K01–K08 come from Delivery Records, and J01–J04 from admissions.
  - **J05 (average stay) and J06 (bed occupancy)** stay Gap, because the report engine stores whole counts. Total stay days are captured as `stay_days`.
  - **Breech deliveries** fall in no K line; this is a question for the HIS Officers.

**D62. Maternal and child health records (MCH), 2026-11-21.** BRD FR-14, SDD §5.3, tracker CON-14..17. The user chose MCH as the next clinical-services piece and asked to build it now and confirm with KRCS later. **Everything is provisional until the MCH In-Charge confirms the fields (OI-8).** (D61 is reserved for the stock work, Phase 6S.)
- **Sources:**
  - **Fields:** the Kenya MOH Mother & Child Health Handbook (MOH 216) and WHO 2016 ANC (8 contacts).
  - **Immunisation schedule:** KEPI.
  - **Growth:** the WHO Child Growth Standards 0–5 years. The official expanded LMS tables are bundled offline in `mch/data/`, each recording its source URL and retrieval date.
- **Records (module EMR MCH):**

  | Record | What it is | Number |
  |---|---|---|
  | ANC Pregnancy | One open per mother; closed automatically as Delivered when her Delivery Record is submitted, or by hand as "Pregnancy ended" or "Transferred out" | `ANC-` |
  | ANC Contact | Antenatal contact | `ANCV-` |
  | PNC Visit | Postnatal check of the mother or the baby; on the ward it is recorded against the Maternity Admission | `PNC-` |
  | Immunisation | Doses given at a visit | `IMM-` |
  | KEPI Vaccine Schedule | The schedule master: 25 doses, seeded once and never overwritten | |
  | Growth Monitoring | Measurements with WHO z-scores | `GRM-` |
- **Rules for every record** (`mch/common.py`):
  - made during an **open visit** of the same patient;
  - **one per visit**;
  - facility, consultation link and recorder are **set by the server**;
  - **submitted with no cancel**; a correction is a signed addendum, and the Medical In-Charge archives a record made in error.
- **Who:**
  - Clinical Officer, Midwife and Medical In-Charge record every type;
  - the **Triage Nurse** also records Immunisation and Growth Monitoring (measured at the child welfare desk);
  - the Mental Health Officer and HIS Officer read.
  - No MCH Nurse role yet; it can follow OI-8.
- **Immunisation:**
  - a dose already given is refused;
  - a dose more than 14 days early, or sooner than the minimum interval, is refused;
  - each dose's status is Given, Due (up to 28 days past due), Overdue, Upcoming or Missed (past the latest age);
  - the **Due Immunisations** report lists the children due or overdue.
- **Growth:**
  - WFA, LHFA and WFL/WFH z-scores, with WHO's SD23 rule and the ±0.7 cm position correction;
  - classed as underweight, stunting and MAM/SAM (by WFLH, by MUAC from 6 months, or by oedema);
  - an estimated date of birth is noted;
  - records stop at 1,856 days.
- **Desk:**
  - an "MCH" button group on open visits and on draft MCH consultations;
  - an MCH panel on the Patient form (open pregnancy, immunisation status, weight-for-age chart);
  - the Maternity admit dialog prefills LMP, gravida and para from the open pregnancy;
  - an MCH dropdown in the Outpatient sidebar.
- **MOH 717 (provisional, OI-5):**

  | Row | Counts |
  |---|---|
  | B07 CWC | Child visits with an Immunisation or Growth Monitoring, one per visit |
  | B08 ANC | ANC Contacts |
  | B09 PNC | PNC Visits, mothers and babies (ward checks included) |

  They come from the new "MCH Records" source.
- **Review and audit S6 (`tests/test_state_machines_mch.py`), seven defects fixed:**
  - **DEFECT-MCH-1:** facility and consultation were taken from the client. They now always come from the visit or the admission.
  - **DEFECT-MCH-2:** an addendum could be typed into a draft and submitted. It now goes through Add Addendum only.
  - **DEFECT-MCH-3:** the recorder could be rewritten on a draft save. It is now kept.
  - **DEFECT-MCH-4:** the uniqueness checks did not use locking reads. They now do.
  - **DEFECT-MCH-5:** Immunisation locked the visit before the patient. It now locks Patient → Visit, as registration and the maternity admit do.
  - **DEFECT-MCH-6:** there were no desk buttons for Close Pregnancy, Add Addendum or Archive. `mch/record_actions.js` adds them.
  - **DEFECT-MCH-7:** the Patient-panel reads were not access-logged. They now are (D44).
- **Provisional values to confirm (OI-8):**
  - **KEPI:**
    - BCG has no upper age;
    - OPV 0 latest at 14 days;
    - 28-day intervals;
    - Rota latest at 24 months;
    - MR 1 at 9 months, MR 2 at 18 months;
    - Vitamin A from 9 months then 6-monthly to 59 months. Kenya's 6-month Vitamin A dose is not seeded.
  - **ANC/PNC plausible ranges.**
  - **Whether ward PNC checks count in B09.**

**D63. Mental Health consultation note, 2026-11-24.** BRD FR-13, SDD §5.3, tracker CON-13. Built now on a **provisional format**, to be confirmed with the Mental Health Officer (OI-7).
- **Where:** a structured section on the Mental Health consultation itself (Patient Encounter `krcs_mh_*` custom fields, `clinical/setup.py`, logic in `clinical/mh_note.py`), not a separate record.
  - It inherits the encounter's sensitive access logging (D44), its hiding from Midwife-only readers and its exclusion from outreach.
  - Other departments never carry the fields: the server clears them.
- **Format** (from the Dadaab Mental Health Admission Form headings, SDD §5.12.4, plus a standard outpatient psychiatric assessment):

  | Section | Contents |
  |---|---|
  | Assessment | Visit type, referral source, onset, past psychiatric and medical history, substance use, personal and social history (displacement, trauma) |
  | Mental state examination | Appearance and behaviour, speech, mood, affect, thought form and content, perception, cognition, insight, judgement |
  | Risk | Suicide risk and harm to others (None identified / Low / Moderate / High), recent self-harm, notes, safety plan |
  | Screening scores (optional) | PHQ-9 (0–27) and GAD-7 (0–21), stored so that blank differs from 0 |
  | Plan | Formulation, interventions, follow-up date |
- **Completing the consultation** needs the two risk ratings and the mood. A **safety plan** is required when either risk is High or recent self-harm is ticked. A red "High risk" headline shows on the form.
- **Corrections after completion** use the encounter addendum (D29).
- **Review fixes:**
  - **DEFECT-MH-1:** the clinic (`krcs_department`) of an existing consultation could be changed through the API, which cleared the note, skipped its rules and dropped its sensitivity. It can no longer change; refer the patient instead.
  - **DEFECT-MH-2:** an oversized score caused a server error. It is now a validation message.
  - **DEFECT-MH-3:** the note fields printed. They are now `print_hide`.
- **Still to confirm (OI-7):**
  - the required fields, the safety-plan trigger and the option lists;
  - the screening tools (PHQ-9 and GAD-7; others such as AUDIT or PCL-5?);
  - whether the note shows in the history panel and print formats (it does not yet);
  - whether Mental Health consultations stay open to the Clinical Officer (D28).

**D64. Two-factor sign-in off, 2026-11-24 (user).** This revises D16, which required 2FA for the HIS Officer and the EMR System Administrator.
- **Why:** signing in failed with "Please setup default outgoing email account". The OTP-app setup emails a QR code on first sign-in, and no outgoing email account is configured.
- **What changed:**
  - `enable_two_factor_auth` is 0, and no role has `two_factor_auth`.
  - On new sites this comes from `security.matrix.SYSTEM_SETTINGS` and `ROLE_SETTINGS`.
  - On existing sites the one-time fix-up `krcs_security_fixups_v7` (`security.setup.apply_two_factor_off`) applies it.
- **To restore:** configure an outgoing email account (or use SMS), then a System Manager switches it back on in System Settings and on the two roles. Before go-live this needs a decision, because SDD §6.3 and NFR-5 expect it.
