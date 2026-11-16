# Maternity contract (Phase 5M W0)

This is the contract between the W0 integrator and the Phase 5M builders: **M1** Admission, **M2** Partograph and **M3** Delivery and reports.
- Names and values come from `constants.py` (the "Maternity contract (W0)" block). Never hard-code them.
- Covers SDD §5.12.3, tracker INP-12..18 and ROL-24, open items OI-19 (Kalobeyei forms) and OI-23. The decision is D60.
- Content is provisional, from the Dadaab maternity templates, until the Kalobeyei forms arrive (OI-19).
- **Do not change this file, the DocType field names or the signatures below without the integrator.** A builder who believes one must change stops and reports.

## 1. Roles and permissions

`ROLE_MIDWIFE = "Midwife"` is in `ALL_ROLES`, `CLINICAL_ROLES` and `DIED_ROLES` (a Midwife may record a maternal death). `MATERNITY_ROLES = (Midwife, Clinical Officer, Medical In-Charge)` admit, discharge, keep the partograph and record deliveries.

**DocPerms in the maternity DocType JSON** (no delete, cancel or amend for anyone, NFR-9):

| Role | Maternity Admission | Partograph | Delivery Record |
|---|---|---|---|
| Midwife | read, write, create | read, write, create | read, write, create, submit |
| Clinical Officer | read, write, create | read, write, create | read, write, create, submit |
| Medical In-Charge | read, write | read, write | read, write |
| Mental Health Officer | read | read | read |
| HIS Officer | read | read | read |

The Medical In-Charge is in `MATERNITY_ROLES` but has no create right: records are created by the M1/M2 endpoints, which check `MATERNITY_ROLES` themselves and then insert with `ignore_permissions=True` (list every such place in your report). The desk "New" button is for the Midwife and the Clinical Officer only, and the controllers refuse a hand-made admission (see §5).

**Midwife rights on Marley / Frappe DocTypes** (`security/matrix.py`, Custom DocPerm):

| Record | Midwife |
|---|---|
| Patient | read, write, create (fast path registers the mother; newborns are created by the server) |
| Vital Signs | read, write, create, submit (ward observations) |
| Patient Encounter, Clinical Note, Observation, Diagnostic Report, Service Request, Medication Request | read |
| Masters (Medical Department, Healthcare Service Unit, Triage Level, Code System, Code Value, Healthcare Practitioner) | read, through `FACILITY_ROLES` |
| Observation Template, Complaint | read |

**Patient Visit:** the Midwife is in `visit.permissions.VISIT_READ_ROLES` and reads permlevels 0, 1 and 2 (like the Clinical Officer reads them). She never writes a visit; the engine's `admit` action closes it. The fast-path visit insert in M1 therefore needs `ignore_permissions=True` on the insert (the controller's attendance, queue-number and registration rules still run).

**Role settings:** `ROLE_SETTINGS["Midwife"] = {"desk_access": 1, "home_page": "app/maternity-admission"}`. The security fix-up v5 (`krcs_security_fixups_v5`, `security.setup.apply_phase5_fixups`) applies it once on sites set up before Phase 5M. The integrator may point the home page at the Maternity workspace later.

## 2. Department, ward and beds

- `DEPT_MATERNITY = "Maternity"` is in `DEPARTMENTS` (seeded as a Medical Department; Marley ships it too). It is **routable**: Triage routes a woman in labour to Maternity, and a clinician refers internally to Maternity.
- The Maternity queue block is **not** in `visit/workspaces.DEPARTMENT_WORKSPACE` yet. The integrator adds the Maternity workspace, its sidebar and dock area in step 3 and maps the department to it then.
- **Ward:** `maternity.setup.seed_maternity_wards` creates one Healthcare Service Unit group per facility, named by `setup.operations.maternity_ward_name(facility, abbr)`: "Maternity Ward - NTK - KLB", "Maternity Ward - NRG - KLB" (Dadaab: "... - IFO1 - DDB"). `MATERNITY_WARD_NAME = "Maternity Ward"`.
  - Marley clears `service_unit_type` on a group, so a ward is found by name: `maternity.setup.ward_for_facility(facility) -> str | None`.
  - **No beds are seeded.** Beds are leaf units of type "Bed" under the ward (the EMR System Administrator adds them).
- **Bed occupancy** is set with direct writes (`frappe.db.set_value(..., "occupancy_status", "Occupied" / "Vacant")`), never by saving the unit: Marley's `HealthcareServiceUnit.validate` resets the status to Vacant on every save.

## 3. DocTypes and naming

Module **EMR Maternity**: DocTypes in `emr_maternity/doctype/`, logic in `maternity/`. The parents are in `CLINICAL_DOCTYPES` (never deleted, `krcs_archived` field added by `security.setup.add_archived_fields`, ERPNext company deletion skips them) and in `ACCESS_LOGGED_DOCTYPES` (desk opens, API reads and lists go to the Access Log).

| DocType | Constant | Kind | Series (EMR Naming Settings) | track_changes |
|---|---|---|---|---|
| Maternity Admission | `MATERNITY_ADMISSION_DOCTYPE` | parent | `{prefix}-MAT-.YYYY.-` (the IP number) | 0 (status log) |
| Maternity Admission Log | `MATERNITY_ADMISSION_LOG_DOCTYPE` | child of the admission (`status_log`) | | |
| Obstetric History | `OBSTETRIC_HISTORY_DOCTYPE` | child of the admission (`obstetric_history`) | | |
| Partograph | `PARTOGRAPH_DOCTYPE` | parent | `{prefix}-PTG-.YYYY.-` | 0 (append-only rows) |
| Partograph Observation | `PARTOGRAPH_OBSERVATION_DOCTYPE` | child of the partograph (`observations`) | | |
| Delivery Record | `DELIVERY_RECORD_DOCTYPE` | parent, **submittable** | `{prefix}-DEL-.YYYY.-` | 0 |
| Delivery Baby | `DELIVERY_BABY_DOCTYPE` | child of the delivery (`babies`) | | |

Diagnoses on the admission reuse `EMR Encounter Diagnosis` (`ENCOUNTER_DIAGNOSIS_DOCTYPE`) in the `diagnoses` table: ICD-11 `code` (Link Code Value), `code_value`, `title`, `case_type`, `is_primary`, `note`. Every existing query on that child filters on `parenttype = "Patient Encounter"`, so admission rows never enter the MOH 705 counts.

### 3.1 Coded values (constants)

| Value set | Constant | Values |
|---|---|---|
| Admission status | `ADMISSION_STATUSES` | Admitted, In Labour, Delivered, Discharged (`ADMISSION_OPEN_STATUSES` = the first three) |
| Discharge outcome | `MATERNITY_DISCHARGE_OUTCOMES` | Discharged home, Referred out, Died, Absconded |
| Mode of delivery | `DELIVERY_MODES` | Spontaneous vaginal delivery, Assisted vaginal delivery, Caesarean section, Breech delivery |
| Born before arrival | `DELIVERY_BBA_FIELD` | the Check `Delivery Record.born_before_arrival`, independent of the mode |
| Baby outcome | `BABY_OUTCOMES` | Live birth, Fresh stillbirth, Macerated stillbirth |
| Liquor | `LIQUOR_CODES` | I, C, M, B, A |
| Moulding | `MOULDING_GRADES` | 0, +, ++, +++ |
| Contraction duration | `CONTRACTION_DURATION_BANDS` | <20 s, 20-40 s, >40 s |
| Urine protein / acetone | `URINE_DIPSTICK_VALUES` | Nil, Trace, +, ++, +++ |
| Active labour | `PARTOGRAPH_ACTIVE_LABOUR_CM` | 4 (cm) |
| Newborn duplicate override | `NEWBORN_OVERRIDE_REASON` | "Newborn registered at delivery" |

### 3.2 Fields

The JSON is the source of truth; this list is generated from it. Section and column breaks are left out.

#### Maternity Admission

| Field | Type | Notes |
|---|---|---|
| `naming_series` | Select | required; set once |
| `patient` | Link → Patient | required; set once |
| `patient_name` | Data | read-only; fetched from `patient.patient_name` |
| `status` | Select | options: Admitted, In Labour, Delivered, Discharged; read-only; default Admitted |
| `visit` | Link → Patient Visit | read-only |
| `facility` | Link → Healthcare Service Unit | required |
| `ward` | Link → Healthcare Service Unit |  |
| `bed` | Link → Healthcare Service Unit |  |
| `admitted_at` | Datetime | read-only |
| `admitted_by` | Link → User | read-only |
| `husband_name` | Data |  |
| `employer` | Data |  |
| `safe_motherhood_no` | Data |  |
| `gravida` | Int |  |
| `para` | Data |  |
| `anc_clinic` | Data |  |
| `lmp` | Date |  |
| `edd` | Date |  |
| `gestation_weeks` | Int | read-only |
| `medical_history` | Small Text |  |
| `family_history` | Small Text |  |
| `transfusion_history` | Small Text |  |
| `obstetric_history` | Table → Obstetric History |  |
| `blood_group` | Select | options: A, B, AB, O |
| `rhesus` | Select | options: Positive, Negative |
| `syphilis_rpr` | Select | options: Reactive, Non-reactive, Not done |
| `blood_group_result` | Link → Observation |  |
| `rhesus_result` | Link → Observation |  |
| `syphilis_result` | Link → Observation |  |
| `fundal_height_cm` | Float |  |
| `presentation` | Select | options: Cephalic, Breech, Transverse, Oblique, Other |
| `engagement` | Select | options: 5/5, 4/5, 3/5, 2/5, 1/5, 0/5 |
| `position` | Data |  |
| `foetal_heart_rate` | Int |  |
| `urine` | Data |  |
| `haemoglobin` | Float |  |
| `bp_systolic` | Int |  |
| `bp_diastolic` | Int |  |
| `temperature` | Float |  |
| `pulse` | Int |  |
| `respiratory_rate` | Int |  |
| `weight_kg` | Float |  |
| `vaginal_discharge` | Data |  |
| `genitals` | Data |  |
| `spleen` | Data |  |
| `abnormalities` | Small Text |  |
| `diagnoses` | Table → EMR Encounter Diagnosis |  |
| `remarks` | Small Text |  |
| `discharge_outcome` | Select | options: Discharged home, Referred out, Died, Absconded; read-only |
| `discharge_condition` | Small Text | read-only |
| `death_reason` | Small Text | read-only |
| `discharged_at` | Datetime | read-only |
| `discharged_by` | Link → User | read-only |
| `discharge_notes` | Small Text | read-only |
| `status_log` | Table → Maternity Admission Log | read-only |

#### Maternity Admission Log

| Field | Type | Notes |
|---|---|---|
| `from_status` | Data | read-only |
| `to_status` | Data | required; read-only |
| `action` | Data | required; read-only |
| `user` | Link → User | required; read-only |
| `timestamp` | Datetime | required; read-only |
| `comment` | Small Text | read-only |

#### Obstetric History

| Field | Type | Notes |
|---|---|---|
| `year` | Int |  |
| `place` | Data |  |
| `labour_duration` | Data |  |
| `delivery_type` | Data |  |
| `birth_weight_kg` | Float |  |
| `sex` | Link → Gender |  |
| `alive_or_dead` | Select | options: Alive, Dead |
| `feeding` | Data |  |
| `puerperium` | Data |  |
| `pelvic_assessment` | Data |  |

#### Partograph

| Field | Type | Notes |
|---|---|---|
| `naming_series` | Select | required; set once |
| `admission` | Link → Maternity Admission | required; set once |
| `patient` | Link → Patient | read-only; fetched from `admission.patient` |
| `patient_name` | Data | read-only; fetched from `admission.patient_name` |
| `facility` | Link → Healthcare Service Unit | read-only; fetched from `admission.facility` |
| `started_at` | Datetime | read-only |
| `alert_line_crossed` | Check | read-only |
| `action_line_crossed` | Check | read-only |
| `observations` | Table → Partograph Observation | read-only |

#### Partograph Observation

| Field | Type | Notes |
|---|---|---|
| `observed_at` | Datetime | required |
| `recorded_by` | Link → User | read-only |
| `foetal_heart_rate` | Int |  |
| `liquor` | Select | options: I, C, M, B, A |
| `moulding` | Select | options: 0, +, ++, +++ |
| `cervical_dilatation_cm` | Float |  |
| `descent_fifths` | Int |  |
| `contractions_per_10_min` | Int |  |
| `contraction_duration` | Select | options: <20 s, 20-40 s, >40 s |
| `oxytocin_u_per_l` | Float |  |
| `oxytocin_drops_per_min` | Int |  |
| `drugs_iv_fluids` | Small Text |  |
| `pulse` | Int |  |
| `bp_systolic` | Int |  |
| `bp_diastolic` | Int |  |
| `temperature` | Float |  |
| `respiratory_rate` | Int |  |
| `urine_protein` | Select | options: Nil, Trace, +, ++, +++ |
| `urine_acetone` | Select | options: Nil, Trace, +, ++, +++ |
| `urine_volume_ml` | Int |  |
| `note` | Small Text |  |

#### Delivery Record

| Field | Type | Notes |
|---|---|---|
| `naming_series` | Select | required; set once |
| `admission` | Link → Maternity Admission | required |
| `patient` | Link → Patient | read-only; fetched from `admission.patient` |
| `patient_name` | Data | read-only; fetched from `admission.patient_name` |
| `facility` | Link → Healthcare Service Unit | read-only; fetched from `admission.facility` |
| `delivered_at` | Datetime | required |
| `delivered_by` | Link → User |  |
| `delivery_mode` | Select | options: Spontaneous vaginal delivery, Assisted vaginal delivery, Caesarean section, Breech delivery; required |
| `born_before_arrival` | Check |  |
| `labour_induced` | Check |  |
| `induction_method` | Data |  |
| `first_stage_duration_min` | Int |  |
| `vaginal_examinations` | Int |  |
| `second_stage_duration_min` | Int |  |
| `third_stage_management` | Select | options: Active (AMTSL), Expectant |
| `third_stage_duration_min` | Int |  |
| `placenta` | Select | options: Complete, Incomplete, Retained |
| `blood_loss_ml` | Int |  |
| `perineum` | Select | options: Intact, First degree tear, Second degree tear, Third degree tear, Fourth degree tear, Episiotomy |
| `perineal_repair` | Small Text |  |
| `maternal_bp_systolic` | Int |  |
| `maternal_bp_diastolic` | Int |  |
| `maternal_pulse` | Int |  |
| `maternal_temperature` | Float |  |
| `maternal_notes` | Small Text |  |
| `babies` | Table → Delivery Baby | required |
| `remarks` | Small Text |  |

#### Delivery Baby

| Field | Type | Notes |
|---|---|---|
| `outcome` | Select | options: Live birth, Fresh stillbirth, Macerated stillbirth; required |
| `sex` | Link → Gender |  |
| `weight_kg` | Float |  |
| `apgar_1` | Int |  |
| `apgar_5` | Int |  |
| `head_circumference_cm` | Float |  |
| `length_cm` | Float |  |
| `resuscitation` | Select | options: Not needed, Stimulation, Suction, Bag and mask ventilation |
| `vitamin_k` | Check |  |
| `other_drugs` | Small Text |  |
| `newborn_patient` | Link → Patient | read-only |

## 4. Custom fields (W0, `maternity/setup.py`)

| DocType | Field | Constant | Type | Notes |
|---|---|---|---|---|
| Patient | `krcs_mother` | `PATIENT_MOTHER_FIELD` | Link → Patient | read-only, in standard filter, indexed. Set by M3 on a newborn. |
| Vital Signs | `krcs_maternity_admission` | `VITAL_SIGNS_ADMISSION_FIELD` | Link → Maternity Admission | indexed. A ward observation: `krcs_visit` stays empty, so the triage hooks (`visit/events.py`) leave it alone. |

## 5. Visit integration (W0, `visit/engine.py`)

New action **`admit`** (`engine.ACTION_ADMIT`) in `engine.TRANSITIONS`:

| | |
|---|---|
| From | any open status (`VISIT_OPEN_STATUSES`) |
| To | Discharged; `closed_at` set |
| Roles | `MATERNITY_ROLES`, checked as the current user (never a system event), plus read access to the visit |
| Outcome | Admitted (`engine.ADMIT_OUTCOMES`); `outcome_at` / `outcome_by` set |
| Department | `current_department` becomes Maternity |
| Guard | no open laboratory or pharmacy task, and no draft (unfinished) consultation |
| api_only | yes: `apply_action` refuses it, and it is never a visit-form button |

Call it only through **`engine.admit_visit(visit: str, comment: str | None = None) -> Patient Visit`**. It locks the visit (`for_update`), checks the role, writes the status-log row and saves.

Consequences for M1:
- **Lock order** is Visit → Maternity Admission → Bed. Call `admit_visit` first, then create the admission, then occupy the bed, all in one transaction.
- **A routed visit** (Routed to Department, department Maternity) is the normal case. Registered / In Triage are allowed too (the fast path, an emergency).
- **During a consultation** the guard refuses while the encounter is a draft. The clinician either completes the consultation with outcome Admitted (the visit closes, and M1 may then admit with `visit` pointing at that closed visit **without** calling `admit_visit`), or refers internally to Maternity (existing flow; the Midwife admits from the new visit).
- **Internal referral receipt:** `clinical.referral.receive_for_visit` marks a referral Received only when its visit reaches In Consultation. A Maternity visit admitted straight from Routed skips that, so M1 calls `clinical.referral.transition(referral, ACTION_RECEIVE, system=True)` for a Sent internal referral in `visit.referred_from` when it admits. Report it if you would rather the integrator change `receive_for_visit`.
- **One open admission per patient:** M1 refuses a second admission while one is in `ADMISSION_OPEN_STATUSES`.
- **No hand-made admissions:** the Maternity Admission controller refuses an insert that does not come from `maternity.admission.admit` (a flag M1 sets), except `flags.from_factory` (the W0 test factory).

## 6. Lifecycles

### 6.1 Maternity Admission (M1, `maternity/admission.py`)

A module-level `TRANSITIONS` dict and the append-only `status_log` (`Maternity Admission Log`: `action`, `from_status`, `to_status`, `user`, `timestamp`, `comment`). Status changes only through these actions. A Discharged admission is read-only (except `krcs_archived`).

| Action | From → To | Who |
|---|---|---|
| `admit` | (new) → Admitted | `MATERNITY_ROLES` |
| `start_labour` | Admitted → In Labour | `MATERNITY_ROLES` |
| `record_delivery` | Admitted / In Labour → Delivered | **system**, from the Delivery Record submit (M3) |
| `discharge` | Admitted / In Labour / Delivered → Discharged | `MATERNITY_ROLES`; outcome Died needs `DIED_ROLES` and a `death_reason` |

**Discharge** sets `discharge_outcome`, `discharge_condition`, `discharge_notes`, `death_reason` (Died only), `discharged_at` and `discharged_by`, and frees the bed. **Correction** of a wrong admission is archiving (`krcs_archived`) by the Medical In-Charge; nothing is deleted.

### 6.2 Partograph (M2, `maternity/partograph.py`)

One per admission. Rows are only appended, through `add_observation`, and never edited; a correction is a new row with a `note`. `started_at` is the `observed_at` of the first row with `cervical_dilatation_cm >= PARTOGRAPH_ACTIVE_LABOUR_CM`. `alert_line_crossed` / `action_line_crossed` are kept by M2 (alert line 1 cm/h from the first ≥ 4 cm point, action line 4 h to its right). Rows are added only while the admission is Admitted or In Labour.

### 6.3 Delivery Record (M3, `maternity/delivery.py`)

Submittable, no cancel. One submitted Delivery Record per admission. `on_submit`, in this order and in one transaction:
1. `maternity.admission.record_delivery(doc.admission, doc.name)` (M1; locks the admission, moves it to Delivered);
2. `maternity.delivery.register_newborns(doc)` (M3): each **live** baby becomes a Patient (first name "Baby", the mother's last name, `dob` = date of `delivered_at`, sex, a walk-in facility ID at the mother's registration facility, population group and residence copied, `krcs_mother` set, a Patient Relation row "Mother", duplicate override reason `NEWBORN_OVERRIDE_REASON`), and its name is written to `Delivery Baby.newborn_patient`. Stillbirths get no Patient. Rows that already have `newborn_patient` are skipped (idempotent).

## 7. Endpoints and server functions

All whitelisted methods are type-annotated; POST for state changes. Paths are under `onerc_healthcare.maternity.`.

| Owner | Function | Kind | Signature and result |
|---|---|---|---|
| M1 | `admission.admit` | POST | `admit(patient: str, visit: str \| None = None, ward: str \| None = None, bed: str \| None = None, values: dict \| str \| None = None) -> dict` → `{"name", "status", "visit", "bed"}`. With `visit`: it must be the patient's, routed to Maternity (or closed with outcome Admitted, §5). Without: the fast path opens a visit at the user's facility and closes it with `admit_visit`. `values`: admission fields the form collected (personal, pregnancy, history, examination). `ward` defaults to `ward_for_facility(facility)`. |
| M1 | `admission.apply_action` | POST | `apply_action(admission: str, action: str, values: dict \| str \| None = None) -> dict` → `{"name", "status"}`. Human actions only (`start_labour`, `discharge`); `values` carries the discharge fields and an optional `comment`. |
| M1 | `admission.record_delivery` | server only | `record_delivery(admission: str, delivery: str) -> Maternity Admission`. System transition to Delivered; refuses a non-open admission and a second delivery. |
| M1 | `admission.free_bed` | server only | `free_bed(bed: str \| None) -> None`. Sets the bed Vacant by direct write unless another open admission holds it. M1 also owns `occupy_bed(bed: str, admission: str) -> None` (refuses an Occupied bed or one outside the facility's ward). |
| M2 | `partograph.get_or_create` | POST | `get_or_create(admission: str) -> str` → the admission's Partograph name (created on first call). |
| M2 | `partograph.add_observation` | POST | `add_observation(partograph: str, values: dict \| str) -> dict` → `{"name", "row", "started_at", "alert_line_crossed", "action_line_crossed", "warnings": [str]}`. Range checks (pattern of `visit/events.py` `VitalRange`): FHR 60–220, dilatation 0–10 and never below the previous row, descent 0–5, Select values from §3.1. Sets `recorded_by`. |
| M2 | `partograph.get_chart` | GET | `get_chart(partograph: str) -> dict` → `{"started_at", "points": [{"hours", "observed_at", "dilatation", "descent", "fhr"}], "alert_line": [[hours, cm], ...], "action_line": [[hours, cm], ...], "warnings": [str]}`. Warnings: alert line crossed, action line crossed, FHR < 110 or > 160. |
| M3 | `delivery.register_newborns` | server only | `register_newborns(doc) -> list[str]` → the newborn Patient names, in baby-row order (§6.3). |

## 8. Ownership

| Builder | Exclusive files |
|---|---|
| **M1 Admission** | `maternity/admission.py`; `emr_maternity/doctype/{maternity_admission, maternity_admission_log, obstetric_history}/` (controllers and `maternity_admission.js`; field names fixed by §3.2); `maternity/patient.js` ("Admit in Labour"), `maternity/patient_visit.js` ("Admit to Maternity"), `maternity/vital_signs.py` (ward observations: `krcs_maternity_admission` must be an open admission of the same patient); `tests/test_maternity_admission.py` |
| **M2 Partograph** | `maternity/partograph.py`; `emr_maternity/doctype/{partograph, partograph_observation}/` (controllers and `partograph.js`: the bedside dialog and the inline-SVG chart, no CDN); `tests/test_partograph.py` |
| **M3 Delivery and reports** | `maternity/delivery.py`; `emr_maternity/doctype/{delivery_record, delivery_baby}/` (controllers and `delivery_record.js`); the "Delivery" and "Maternity Admission" sources in `reporting/registry.py` and `reporting/workload.py` (`RULE_SCHEMAS`) and their evaluators; the MOH 717 K01–K08 / J01–J06 elements in `reporting/data/moh_717.json`; `tests/test_delivery.py` and the reconciliation additions |
| **Integrator** | `constants.py`, `hooks.py`, `patches.txt`, `maternity/setup.py`, `visit/engine.py`, `visit/permissions.py`, `security/*`, `naming.py`, `desk_sidebar.py`, `visit/workspaces.py`, the dock, the Maternity sidebar and workspace, `tests/maternity_factories.py` (builders add helpers only at the end, and say so) |

Report the hook lines you need (doc_events, `doctype_js`, `has_permission`) with their exact text; the integrator wires them.

## 9. Test factories (`tests/maternity_factories.py`)

- `maternity_users() -> dict` → users `t-mat-<key>@test.krcs` for `midwife`, `clinician`, `mic`, `his`, `reception`, `mho`, each holding exactly that role.
- `make_maternity_patient(**kw)` → a T- woman of 25 with a UNHCR ID.
- `make_admission(patient=None, facility=None, ward=None, bed=None, status="Admitted", **kw)` → a Maternity Admission inserted directly (`ignore_permissions`, `flags.from_factory`), with one `admit` log row. It closes no visit and occupies no bed. For M2 and M3 until M1's API exists; M1's own tests use the API.
- `make_bed(ward, name=None) -> str` → a T- Bed (type Bed, Vacant) under `ward`.

## 10. Desk (Marley's Inpatient area)

Marley's Inpatient Record is not used (D60): every save runs `set_item_rate` and crashes without an enabled selling price list (D18), and it carries bed-rent billing and stock (C-5). `desk_sidebar.HIDDEN_DOCK_ROWS` sets `hidden = 1` on the Inpatient row of Marley's dock on every migrate (direct write; Marley's Dock is never saved). The area stays reachable by route, and a Workspace Manager can bring it back in Manage Dock.

## 11. Deployment note

A new module is read from `modules.txt` through Frappe's cached module map (`app_modules` in Redis). On a site whose cache predates the module, the first `bench migrate` creates the Module Def but does not sync the new DocTypes. Run `bench --site <site> clear-cache` before the first migrate (or migrate twice).
