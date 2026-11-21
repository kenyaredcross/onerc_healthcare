# MCH contract (W0)

This is the contract between the W0 integrator and the two MCH builders: **MCH-A** (antenatal, postnatal, MOH counts) and **MCH-B** (immunisation, growth monitoring).
- Names and values come from `constants.py` (the "MCH contract (W0)" block). Never hard-code them.
- Covers BRD FR-14, SDD §5.3, tracker CON-14..17 and MOH 717 B07–B09. The decision is **D62** (D61 is the stock session's).
- Content is **provisional** until the MCH In-Charge confirms the fields (OI-8). Sources: Kenya MOH Mother & Child Health Handbook (MOH 216), WHO 2016 ANC (8 contacts), KEPI, WHO Child Growth Standards.
- **Do not change this file, `mch/common.py`, the DocType field names or the signatures below without the integrator.** A builder who believes one must change stops and reports. Property tweaks to your own DocTypes (description, `depends_on`, precision, `reqd`) are allowed: bump `modified`.

## 1. Roles and permissions

`MCH_ROLES = (Clinical Officer, Midwife, Medical In-Charge)` record every MCH record. `CHILD_MCH_ROLES` adds the **Triage Nurse** for Immunisation and Growth Monitoring (measured at the CWC desk, often before a consultation). `MCH_READ_ROLES = (Mental Health Officer, HIS Officer)` read. No new role (an MCH Nurse role may follow OI-8). Head of Operations, Receptionist, Lab, Pharmacist, Outreach: no access.

**DocPerms in the JSON** (no delete, cancel or amend for anyone, NFR-9):

| Role | ANC Pregnancy | ANC Contact, PNC Visit | Immunisation, Growth Monitoring | KEPI Vaccine Schedule |
|---|---|---|---|---|
| Clinical Officer, Midwife, Medical In-Charge | read, write, create | read, write, create, submit | read, write, create, submit | read |
| Triage Nurse | — | — | read, write, create, submit | read |
| Mental Health Officer, HIS Officer | read | read | read | read (HIS: write, create) |
| EMR System Administrator | — | — | — | read, write, create |

The Medical In-Charge also writes KEPI rows. Records are made in the desk form (no create endpoint), so the controllers do all the checks.

**Links a user cannot read** (HANDOVER §11 lesson): the Triage Nurse cannot read Patient Encounter, Maternity Admission or Delivery Record. Those links on our forms are **set by the server on save** and are read-only; no form script or `fetch_from` fills them in the browser.

## 2. DocTypes and naming

Module **EMR MCH**: DocTypes in `emr_mch/doctype/`, logic in `mch/`. The parents (except the KEPI master) are in `CLINICAL_DOCTYPES` (never deleted, `krcs_archived` added by security setup) and `ACCESS_LOGGED_DOCTYPES` (opens, API reads and lists logged; not sensitive).

| DocType | Constant | Kind | Series | Owner |
|---|---|---|---|---|
| ANC Pregnancy | `ANC_PREGNANCY_DOCTYPE` | parent, not submittable, track_changes | `{prefix}-ANC-.YYYY.-` | MCH-A |
| ANC Contact | `ANC_CONTACT_DOCTYPE` | parent, **submittable** | `{prefix}-ANCV-.YYYY.-` | MCH-A |
| PNC Visit | `PNC_VISIT_DOCTYPE` | parent, **submittable** | `{prefix}-PNC-.YYYY.-` | MCH-A |
| Immunisation | `IMMUNISATION_DOCTYPE` | parent, **submittable** | `{prefix}-IMM-.YYYY.-` | MCH-B |
| Immunisation Dose | `IMMUNISATION_DOSE_DOCTYPE` | child (`doses`) | | MCH-B |
| KEPI Vaccine Schedule | `KEPI_SCHEDULE_DOCTYPE` | master, named by `dose_label`, track_changes | | MCH-B |
| Growth Monitoring | `GROWTH_MONITORING_DOCTYPE` | parent, **submittable** | `{prefix}-GRM-.YYYY.-` | MCH-B |

`MCH_RECORD_DOCTYPES` = the four submittable per-visit records. The field lists are the DocType JSON files (read them); the rules for each field are below.

## 3. Shared fields of the per-visit records

| Field | Rule |
|---|---|
| `patient` | Required, set once. The person the record is about (the mother, or the child/baby). |
| `visit` | Required (PNC Visit: or `maternity_admission`). An **open** visit of the same patient when the record is inserted. Set once. |
| `encounter` | Read-only. Set by the server from the visit's consultation (`Patient Visit.encounter`) when there is one. |
| `facility` | Read-only. Set by the server from the visit (PNC on the ward: from the admission). |
| `recorded_by` | Read-only. The user who inserted the record. |
| `addendum` | Read-only, allow on submit. Grows only through `mch.common.add_addendum`. |
| `krcs_archived` | Changes only through `mch.common.archive` (Medical In-Charge). |

## 4. `mch/common.py` (integrator-owned; call it, never edit it)

- `check_visit(doc, required=True) -> dict | None`: locks the visit (`SELECT … FOR UPDATE`, lock order D41: Visit first), checks the patient, refuses a **closed** visit on insert (a draft may be submitted after the visit closes), fills `facility` and `encounter`.
- `guard_one_per_visit(doc, keys=("visit",))`: one record of the DocType per `keys` (drafts and submitted count, archived and cancelled do not). Call **after** `check_visit`.
- `set_recorded_by(doc)`, `age_in_days(dob, on)`, `age_label(days)`.
- `guard_draft(doc) -> bool`: call **first** in `validate`. Refuses an addendum on a draft and any `krcs_archived` change not made by `archive`; returns True for an archive-only save (return straight away) and refuses edits of an archived draft.
- `before_submit(doc)` (refuses archived), `refuse_cancel(doc)`, `guard_after_submit(doc)`: wire into the controller hooks (the W0 stubs already do).
- `add_addendum(doctype, name, text)` (POST): `MCH_ROLES`, or `CHILD_MCH_ROLES` for Immunisation and Growth Monitoring.
- `archive(doctype, name, reason)` (POST): Medical In-Charge only; also accepts ANC Pregnancy.
- `MCHRecordError` is the validation error class.

`validate` pattern every record follows:
```python
def contact_validate(doc):
	if common.guard_draft(doc):
		return
	common.check_visit(doc)
	common.guard_one_per_visit(doc)
	common.set_recorded_by(doc)
	...  # the record's own rules
```
`validate` also runs on submit (`doc._action == "submit"`): keep the rules idempotent.

## 5. ANC (MCH-A): `mch/anc.py`

**ANC Pregnancy** (`pregnancy_validate`, `pregnancy_on_update`):
- Female patient only. **One open, non-archived pregnancy per mother:** check it under a lock on the Patient row.
- EDD: with basis LMP the server sets `edd = maternity.admission.edd_from_lmp(lmp)`; otherwise the EDD is typed. The LMP is not in the future and not more than 45 weeks ago at booking. Validate gravida and para like the admission (`maternity.admission.validate_gravida_para`, `parse_para`). `gestation` is today's gestation (`maternity.admission.gestation` / `gestation_label`).
- `status`, `closed_*`, `delivery_record` and `closure_note` are **server fields** (`privacy.lifecycle.guard_fields`). A Closed pregnancy is read-only (except the archive flag).
- Call `common.guard_draft` first (pregnancies are archived through `common.archive`).
- `factory`: `tests.mch_factories.make_pregnancy` inserts with `flags.from_factory`. The controller must accept it.

**ANC Contact** (`contact_*`):
- `pregnancy` is read-only and set by the server to the mother's open pregnancy. If she has none: "Book the pregnancy first (ANC Pregnancy)". Female patient.
- `contact_number` = this pregnancy's non-archived contacts before this one + 1, set on insert under a lock on the pregnancy row. `gestation` is computed at `contact_date`.
- `contact_date` is not in the future, not before the booking date, and not after a closed pregnancy's `closed_on`.
- **Plausible ranges**, refused outside (the `visit/events.py` VitalRange pattern):

  | Field | Range |
  |---|---|
  | BP systolic | 50–250, and above diastolic |
  | BP diastolic | 30–150 |
  | Foetal heart rate | 60–220 |
  | Weight | 25–200 kg |
  | Fundal height | 5–50 cm |
  | Hb | 2–20 g/dL |

  0 means not recorded.
- `before_submit`: the pregnancy is still Open and not archived.

**Endpoints and hooks:**
- `open_pregnancy(patient, for_update=False) -> str | None`.
- `close_on_delivery(doc, method)`: a Delivery Record `on_submit` doc_event (wired in hooks.py). It closes the mother's open pregnancy: Closed, `PREGNANCY_DELIVERED`, `closed_on` = the delivery date, `delivery_record`, `closed_by`. Idempotent: no open pregnancy means nothing happens. It runs in the submit's transaction and must not fail the delivery.
- `close_pregnancy(pregnancy, reason, note=None)` (POST, `MCH_ROLES`): reason is `PREGNANCY_ENDED` or `PREGNANCY_TRANSFERRED`.
- `admit_prefill(patient) -> dict` (GET): `{lmp, edd_basis, edd, gravida, para, blood_group, rhesus, pregnancy}` of the open pregnancy. Returns `{}` when there is none or the user cannot read it. The integrator calls it from the Maternity admit dialog.

## 6. PNC (MCH-A): `mch/pnc.py`

- **Exactly one of** `visit` or `maternity_admission`.
  - **With an admission:** it is open (`ADMISSION_OPEN_STATUSES`), not archived and locked. The patient is the admitted mother, or a baby whose `krcs_mother` is the admitted mother. `facility` comes from the admission. One record per (`maternity_admission`, `patient`).
  - **With a visit:** `common.check_visit`. One record per (`visit`, `subject`, `patient`).
- **Subject Mother:** a female patient, and `mother` = the patient. `delivery_record` = her latest submitted, non-archived Delivery Record. `days_after_delivery` counts from its `delivered_at`.
- **Subject Baby:**
  - `mother` = `Patient.krcs_mother`; this may be blank for a baby born elsewhere.
  - `delivery_record` = the submitted, non-archived Delivery Record with a `babies` row whose `newborn_patient` is the baby.
  - `days_after_delivery` counts from the baby's date of birth.
  - The baby is under 1 year.
- Mother fields are ignored (cleared) for a Baby check, and baby fields for a Mother check. Ranges follow ANC: temperature 30–43 °C, pulse 30–220, baby weight 0.5–10 kg.

## 7. Immunisation (MCH-B): `mch/immunisation.py`

**KEPI schedule:**
- **Seed:** `mch/data/kepi_schedule.json` → `seed_schedule()` inserts the missing `dose_label`s and never edits a row (D46). The rows:
  - **birth:** BCG, OPV 0;
  - **6, 10 and 14 weeks:** OPV 1–3, Penta 1–3, PCV 1–3, plus Rota 1–2 at 6 and 10 weeks;
  - **14 weeks:** IPV;
  - **9 months:** MR 1 and Vitamin A 1, with Vitamin A every 6 months to 59 months;
  - **18 months:** MR 2.

  Each row has due age in days, latest age, minimum interval, route and sort order.
- **`schedule_row_validate`:** due age ≥ 0; latest age 0 (none) or ≥ due age; unique (`antigen`, `dose_number`).

**Immunisation** (`immunisation_*`):
- The child is under 5 years (`GROWTH_MAX_AGE_DAYS`). `given_on` is not in the future and not before the date of birth.
- At least one dose row; no vaccine twice in a record. A vaccine already **Given** on another submitted, non-archived Immunisation of the child is refused (with its date).
- The vaccine must be active. "Not given" needs a reason. `given_by` is set by the server on Given rows. `age_at_visit` = `common.age_label`.
- **Timing:** a dose given more than 14 days before its due age, or sooner than its minimum interval after the previous dose of the antigen, is **refused**, with the date it becomes due.

**`schedule(patient, on=None) -> list[dict]`** (GET): one dict per active KEPI row:
- **Keys:** `vaccine`, `antigen`, `dose_number`, `due_date`, `latest_date`, `status`, `given_on`, `immunisation`, `dob_estimated`.
- **Due date:** `due_date = max(dob + due_age_days, previous dose of the antigen + min_interval_days)`.
- **Status:**

  | Status | When |
  |---|---|
  | Given | a submitted, non-archived Given dose |
  | Upcoming | `on < due_date` |
  | Due | `due_date ≤ on ≤ due_date + DUE_GRACE_DAYS` (28) |
  | Overdue | after that, until `latest_date` |
  | Missed | after `latest_date` |

- **Access:** the caller must be able to read the Patient.

**Script report "Due Immunisations"** (`REPORT_DUE_IMMUNISATIONS`, in `emr_mch/report/due_immunisations/`):
- **Filters:** facility, from/to date (default today … today+7), status (Due/Overdue/both).
- **Rows:** child, sex, age, mother (`krcs_mother`), phone, dose, due date, status, last immunisation date.
- **Children counted:** under 5, with at least one submitted Immunisation at the facility.
- **Roles:** `CHILD_MCH_ROLES` + HIS Officer.

## 8. Growth Monitoring (MCH-B): `mch/growth.py`

- **Tables:** bundled offline in `mch/data/who_{wfa,lhfa,wfl,wfh}_{boys,girls}.json` (WHO expanded LMS tables; by day for wfa/lhfa, by 0.1 cm for wfl/wfh). Each file has `source` (URL), `retrieved` (date) and rows `[x, L, M, S]`. **Never invent reference values:** if the WHO files cannot be downloaded, stop and report.
- **`zscore(indicator, sex, x, measure)`:** `z = ((measure/M)^L − 1)/(L·S)`. For wfa, wfl and wfh, apply WHO's adjustment beyond ±3 SD (the SD23 rule). Returns None outside the table.
- **Validation:**
  - `age_days` at `measured_on`, from 0 to below `GROWTH_MAX_AGE_DAYS` (otherwise refused); `measured_on` not in the future;
  - plausible ranges: weight 0.5–40 kg, length/height 35–130 cm, MUAC 5–25 cm, head circumference 25–60 cm; 0 means not measured;
  - `dob_estimated` copied from `Patient.krcs_dob_estimated`.
- **Length and height:** before 731 days the child is measured lying, from 731 days standing. If the other position is used, WHO's ±0.7 cm correction applies and is noted in `zscore_note`. wfl applies under 731 days (45–110 cm) and wfh from 731 days (65–120 cm).
- **Calculated values:** `wfa_z`, `lhfa_z` and `wflh_z` are strings with 2 decimals; blank means not calculable (missing measure or out of range), and the reason goes in `zscore_note`.
- **Classifications:**
  - `underweight` and `stunting` (< −2 moderate, < −3 severe);
  - `wasting` (WFLH < −3, or MUAC < 11.5 cm at 6–59 months, or any oedema → SAM; WFLH < −2 or MUAC 11.5–12.4 → MAM; otherwise Normal; blank if nothing to judge).
  - An estimated date of birth adds a note to `zscore_note`.
- **`chart_data(patient)`** (GET; the caller must read the Patient): `{sex, dob, dob_estimated, points: [{age_days, weight_kg, measured_on, name}], curves: {"-3": [[age_days, kg], …], "-2", "0", "+2", "+3"}}`, weight-for-age curves sampled every 30 days up to 1856.
- **`mch/growth_chart.js`:** `onerc_mch.render_growth_chart(wrapper, data)` draws an inline SVG (no CDN), styled like the partograph chart. The integrator places it on the Patient form.

## 9. Reporting (MCH-A): `reporting/mch.py`

- **Source:** type `"MCH Records"` (`workload.SOURCE_MCH`; registered in `registry.SOURCE_TYPES`, `RULE_SCHEMAS`, the element Select). `evaluate(ctx, elements)` and `rule_errors(rule, disaggregation)`.
- **What is counted:** submitted (`docstatus=1`), non-archived records of non-merged patients, at the run's facilities (`workload._facility_conditions` on the record's `facility`), dated in the period by:

  | Record | Date field |
  |---|---|
  | ANC Contact | `contact_date` |
  | PNC Visit | `pnc_date` |
  | Immunisation | `given_on` |
  | Growth Monitoring | `measured_on` |

  Follow `reporting/maternity.py` (`_base`, `workload._aggregate`, `_source_missing`).
- **`unit`:**
  - `records` (default);
  - `visits`: distinct `visit` across all listed record types, so a visit with both an Immunisation and a Growth Monitoring counts once;
  - `patients`.
- **`pnc_subjects`** filters PNC Visit.
- **MOH 717 rows (W0 set them):**

  | Row | Counts | Unit |
  |---|---|---|
  | B07 CWC | Immunisation + Growth Monitoring | visits |
  | B08 ANC | ANC Contact | records |
  | B09 PNC | PNC Visit | records |

  Age and sex disaggregation must work when an HIS Officer sets it.

## 10. Integrator-owned (step 3, not the builders)

- **Entry points:** an MCH button group on Patient Encounter (`krcs_department` = MCH) and on Patient Visit (open MCH visits). New records are prefilled with the patient and visit only, never the encounter.
- **Patient form panel:** the open pregnancy; the immunisation schedule from `schedule`; the growth chart from `chart_data` and `render_growth_chart`.
- **Maternity admit dialog:** prefilled from `admit_prefill`.
- **Desk and data:** the Outpatient sidebar "MCH" dropdown and the MCH queue block shortcut; demo data.
- **Records:** D62, the user guide, the tracker.

## 11. Ownership and working rules

| Builder | Owns (exclusive) | Tests |
|---|---|---|
| MCH-A | `mch/anc.py`, `mch/pnc.py`, `reporting/mch.py`, `emr_mch/doctype/{anc_pregnancy,anc_contact,pnc_visit}/` | `tests/test_mch_anc.py`, `tests/test_mch_pnc.py`, `tests/test_moh_mch.py` |
| MCH-B | `mch/immunisation.py`, `mch/growth.py`, `mch/data/`, `mch/growth_chart.js`, `emr_mch/doctype/{kepi_vaccine_schedule,immunisation,immunisation_dose,growth_monitoring}/`, `emr_mch/report/` | `tests/test_mch_immunisation.py`, `tests/test_mch_growth.py` |

- **Tests:** subclass `tests.base.EMRTestCase` (a savepoint per test; never commit). Use `tests/mch_factories.py`. Test permissions as real users (`mch_users()`, `frappe.set_user`), not as Administrator.
- **Database commands:** run tests with `apps/onerc_healthcare/scripts/run_tests.sh --no-migrate <module>`, never inside `db_lock.sh`. Migrate emr-test with `scripts/db_lock.sh "bench --site emr-test.localhost migrate"`, and only emr-test.
- **Lint:** `uvx ruff@0.14.10 check .` and `format .` (tabs, line length 110, copyright header).
- **Never** edit Marley, `constants.py`, `hooks.py`, `mch/common.py`, the other builder's files, `pharmacy/` or stock settings; a separate session owns stock. Don't commit: the integrator commits. Leave no background processes running.
