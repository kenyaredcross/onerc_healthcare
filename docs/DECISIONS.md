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

**D11. ICD-11 source file.** The WHO ICD-11 MMS file (CC BY-ND 3.0 IGO) is **not** stored in the repo. The importer `coding/icd11.py::import_file(path)` loads it per site at deploy time; only a small test fixture is committed. Confirmed by the user on 2026-11-13.

**D12. Facility codes.** NTK, NRG, IFO1 and IFO2 are internal provisional codes, not Master Facility Registry codes (OI-3). The walk-in ID format `<FACILITY CODE>-<YYYY>-<#####>` is configurable, pending OI-13.

**D13. Test isolation.** Automated tests run only on `emr-test.localhost` (operation `kalobeyei`, `allow_tests` on), through `scripts/run_tests.sh`. All DB work is serialised by `scripts/db_lock.sh`.
