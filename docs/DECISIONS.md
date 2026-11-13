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

**D19. Patient ID series per operation.**
- Patient records are named `KLB-PAT-.YYYY.-#####` on Kalobeyei and `DDB-PAT-.YYYY.-#####` on Dadaab, so the two sites never issue the same Patient ID. Marley's `HLC-PAT-` series is replaced through a property setter.
- Marley's Emergency Record still names its patients `ER-.YYYY.-#####`. That series is hard-coded in Marley, so it stays an option.
- The series is set only while no naming-series property setter exists, so an admin's later choice in Document Naming Settings survives migrate.
- Set on 2026-11-13, before any patient existed.
