# Outreach sync protocol (Phase 4 contract)

This is the wire contract between the outreach app (`frontend/outreach`, builder O3) and the server: `outreach/devices.py` and `outreach/package.py` (O1), and `outreach/sync.py` (O2).
- Names and values come from `constants.py` (Phase 4 block).
- Covers SDD §5.6, FR-23..26, NFR-8/11/18 and DR-3. The decisions are D58+.
- **Do not change this file without the integrator.**

All endpoints are `/api/method/onerc_healthcare.outreach.<module>.<fn>`.
- **GET:** arguments go in the query string.
- **POST:** the body is JSON (`use_json_request_body`).
- **Response:** Frappe's `{"message": ...}`.
- **Errors:** Frappe's usual JSON (`exc_type`, `_server_messages`), with HTTP 401, 403, 417 or 429.

## 1. Authentication

| Phase | How | Endpoints |
|---|---|---|
| Online sign-in | Normal Frappe login: `POST /api/method/login` (usr, pwd), then a session cookie plus the CSRF token from `/outreach` boot | all |
| Device sync | Header `Authorization: krcs-outreach <grant>:<secret>` (no cookie needed) | **only** `sync.push`, `sync.status`, `package.get_reference`, `package.get_patients` |

**Device grant rules:**
- **Validity:** valid until `expires_at`, unless revoked. The device itself must also be Active and the session open.
- **Failures** all return **401** (`frappe.AuthenticationError`):
  - an invalid, expired or revoked grant;
  - a device grant used on any other endpoint.
- **After a 401** the app must ask for an online sign-in. Records are kept.
- **The secret:**
  - returned once by `enrol`;
  - the server stores only its hash;
  - a new `enrol` rotates it.
- **The user:** the server acts as the grant's user, the one who enrolled. A device holds one grant per user per session.

## 2. Devices and sessions (O1, `outreach.devices`)

### `GET devices.my_sessions()`
Lists the sessions the signed-in user belongs to, with status Planned, Active or Returned.

```json
[{"name": "KLB-OUT-2026-00001", "title": "Kalobeyei Village 3 outreach", "facility": "...", "facility_name": "...",
  "start_date": "2026-11-20", "end_date": "2026-11-21", "catchment_area": "...", "status": "Planned",
  "departments": ["Outpatient", "Maternal and Child Health"]}]
```

### `POST devices.enrol(session, device_id, device_label, data_key)`
- **Requires:** an online sign-in, and the user must be on the session team.
- **Arguments:**
  - `device_id`: the client UUID. It is generated once per installation and kept in IndexedDB `meta`.
  - `data_key`: base64 of the device's raw 256-bit AES key. It is **escrowed** on the grant (Password field), so a forgotten PIN never loses records.
- **Effects:** creates or refreshes the Outreach Device and Outreach Device Grant, and sets `expires_at = now + offline_session_days`.

```json
{"grant": "<grant name>", "secret": "<random, shown once>", "expires_at": "2026-11-27 09:00:00",
 "user": {"name": "...", "full_name": "..."},
 "settings": {"offline_session_days": 7, "purge_after_days": 7, "pin_min_length": 6, "max_pin_attempts": 5,
              "max_batch": 50, "summary_visits": 3}}
```

### `GET devices.recover_key(grant)`
Online sign-in only, and only the grant's own user. Returns `{"data_key": "<base64>"}`. It is used after a PIN lockout, and is refused for a revoked grant.

### `POST devices.revoke_device(device, reason)`
Desk only. Allowed for the HIS Officer, Medical In-Charge or EMR System Administrator. It revokes every grant of the device, as part of the lost-device procedure.

## 3. Package (O1, `outreach.package`)

### `GET package.get_reference(session, version=None)`
Returns `{"version": "<sha1>", "unchanged": true}` when the client's `version` matches. Otherwise:

```json
{"version": "<sha1>", "unchanged": false, "data": {
  "icd11": [["<Code Value name>", "<code>", "<display>", 1]],
  "medications": [{"name": "...", "generic_name": "...", "strength": "...", "dosage_form": "...",
                    "default_dosage": "TID", "default_period": "3 Day"}],
  "dosages": ["OD", "BD", "TID"], "periods": ["3 Day", "5 Day"], "dosage_forms": ["Tablet"],
  "lab_templates": [{"name": "Malaria Rapid Diagnostic Test", "abbr": "MRDT", "point_of_care": 1,
                      "data_type": "Select", "options": ["Negative", "..."], "unit": null}],
  "departments": ["Outpatient"],
  "identifier_types": [{"name": "UNHCR ID", "is_primary": 1, "pattern": "^...$", "facility_generated": 0}],
  "residences": [{"name": "...", "location_name": "...", "parent": "...", "is_group": 1, "level": "Village"}],
  "triage_levels": ["Emergency", "Priority", "Non-urgent"],
  "community_activity_types": ["Health talk", "Home visit"],
  "outcomes": ["Discharged home", "Died"],
  "genders": ["Female", "Male"]}}
```

- **`icd11` rows** are `[name, code_value, display, is_leaf]`. Chapter X is excluded.
- **`departments`** are the session's departments, never Mental Health (`OUTREACH_EXCLUDED_DEPARTMENTS`).
- **`residences`** are the whole operation tree, for the registration picker.

### `GET package.get_patients(session, page=0, page_size=500)`
Covers active, unmerged patients whose `krcs_residence` is inside the catchment subtrees, capped at `package_max_patients`. Each patient sent is written once to the Access Log ("Outreach package"). The first download sets the session to Active.

```json
{"page": 0, "has_more": false, "total": 132, "patients": [{
  "name": "KLB-PAT-2026-00012", "modified": "2026-11-10 08:01:02.123456",
  "first_name": "...", "middle_name": null, "last_name": "...", "sex": "Female",
  "dob": "1990-05-01", "dob_estimated": 0, "mobile": null, "residence": "...", "allergies": null,
  "identifiers": [{"identifier_type": "UNHCR ID", "identifier_value": "..."}],
  "recent_visits": [{"date": "2026-10-02", "facility": "...", "department": "Outpatient",
                      "diagnoses": [{"code": "1F40", "title": "...", "case_type": "New"}]}],
  "active_prescriptions": [{"medication": "...", "dosage": "TID", "period": "5 Day", "date": "2026-10-02"}]}]}
```

- **`recent_visits`** holds the last `summary_visits` visits. It **never** includes a Mental Health encounter, or a sensitive service.
- **`modified`** is the base version the device sends back in `patient.update`.

## 4. Offline records and sync (O2, `outreach.sync`)

### Envelope (every record)

```json
{"client_uuid": "6f1c...", "type": "visit.capture", "session": "KLB-OUT-2026-00001",
 "device_id": "a3b2...", "captured_at": "2026-11-20 10:42:00", "depends_on": ["<client_uuid>"],
 "payload": {}}
```

- **`client_uuid`:** a UUID v4, created when the record is finished. It is the idempotency key.
- **`captured_at`:** the device's local time, `YYYY-MM-DD HH:mm:ss`, in the site timezone (Africa/Nairobi).
  - The server refuses a value more than 10 minutes in the future, or before the session's `start_date`.
- **`depends_on`:** the client UUIDs this record needs.
  - The device sends records in dependency order.
  - The server answers `held` while a dependency is in conflict, and `failed` ("dependency not received") while the dependency is unknown.

### `patient.register` payload

```json
{"first_name": "...", "middle_name": null, "last_name": "...", "sex": "Female",
 "dob": "1990-05-01", "estimated_age_years": null, "estimated_age_months": null,
 "mobile": null, "residence": "<Residence Location>", "allergies": null,
 "is_walk_in": 0, "identifiers": [{"identifier_type": "UNHCR ID", "identifier_value": "..."}]}
```

The payload carries **either** `dob` **or** `estimated_age_years` (with optional months). The age is estimated as of the `captured_at` date.

The registration facility is the session's facility. The usual registration rules apply:
- identifier patterns;
- at least one primary identifier, or walk-in;
- a DOB, or an age.

**Conflict check:**
- **Conflict:** the identifier already exists, the patient is a strong duplicate, or similarity ≥ `conflict_score`. The result is `conflict`, and the conflict name comes back in `conflict`.
- **Accepted:** any other case. `name` is the new Patient.

### `patient.update` payload

```json
{"patient": "KLB-PAT-2026-00012", "base_modified": "2026-11-10 08:01:02.123456",
 "changes": {"mobile": "0712...", "residence": "..."},
 "add_identifiers": [{"identifier_type": "Alien ID", "identifier_value": "..."}]}
```

- **Allowed `changes` keys:** `first_name`, `middle_name`, `last_name`, `sex`, `dob`, `estimated_age_years`, `estimated_age_months`, `mobile`, `residence` and `allergies`.
- **Conflict:** the server Patient's `modified` is later than `base_modified` **and** one of the changed fields now differs from the server's value. The result is `conflict`.
- **Accepted:** any other case. The changes are applied.

### `visit.capture` payload (one finished outreach visit)

```json
{"patient": "KLB-PAT-2026-00012", "patient_ref": null,
 "department": "Outpatient",
 "vitals": {"triage_level": "Non-urgent", "chief_complaint": "...", "temperature": 37.2, "pulse": 88,
            "respiratory_rate": 18, "bp_systolic": 120, "bp_diastolic": 80, "weight": 60.5, "height": 1.62,
            "spo2": 98},
 "consultation": {"presenting_complaint": "...", "history": "...", "examination_findings": "...",
                  "management_plan": "...", "no_diagnosis_reason": null,
                  "diagnoses": [{"code": "<Code Value name>", "case_type": "New", "is_primary": 1}]},
 "lab_requests": [{"line_id": "l1", "observation_template": "Malaria Rapid Diagnostic Test",
                   "poc_result": {"result": "Positive - P. falciparum", "resulted_at": "2026-11-20 10:55:00"}}],
 "prescriptions": [{"line_id": "p1", "medication": "...", "dosage": "BD", "period": "3 Day",
                    "dosage_form": "Tablet", "comment": null,
                    "dispensed": {"quantity": 6, "dispensed_at": "2026-11-20 11:05:00"}}],
 "outcome": "Discharged home"}
```

**Fields:**
- **Patient:** exactly one of `patient` (a server name) and `patient_ref` (the client UUID of a `patient.register`, which must also be in `depends_on`).
- **`vitals`:** optional. When present, `triage_level` and `chief_complaint` are required, plus at least one value.
- **`poc_result`:** only for `point_of_care` templates.
  - With a result, the server records the sample, the result and the approval at `resulted_at`.
  - Without one, the order goes to the facility lab worklist. The consultation completes with `OUTREACH_LAB_PENDING_REASON`.
- **`dispensed`:** optional. Without it, the prescription goes to the facility pharmacy worklist.
- **`outcome`:** `Discharged home` (default) or `Died`.

**Server steps** (one savepoint for the whole record):
1. Insert the visit with `is_outreach=1`, back-dated to `captured_at`.
2. Submit the vitals, or start triage.
3. Route the visit.
4. Start the consultation and fill in the encounter.
5. Send the lab orders.
6. Record the POC results.
7. Complete the consultation.
8. Dispense.

**Conflicts and results:**
- **Open facility visit** for the patient (the one-open-visit rule): `conflict` (Overlapping visit). The record is held.
- **Closed facility visit on the same date:** `accepted`, plus a review-only Overlapping visit conflict. Its name comes back in `conflict`.
- **Patient record in conflict** (the record depends on it): `held`.

### `community.activity` payload

```json
{"activity_type": "Health talk", "activity_date": "2026-11-20", "location": "Village 3 market",
 "topic": "...", "participants_male": 12, "participants_female": 25, "participants_total": 37, "notes": null}
```

The server creates and submits a Community Activity Record with `facility` = the session facility and `lead_staff` = the user.

### `POST sync.push(session, device_id, batch_id, records)`
- At most `max_batch` records per call.
- `batch_id` is a client UUID for the call; a re-sent call reuses it.

```json
{"batch": "<Outreach Sync Batch name>", "results": [
  {"client_uuid": "...", "result": "accepted", "doctype": "Patient Visit", "name": "KLB-PV-2026-00031",
   "conflict": null, "error": null}]}
```

**`result` values:**

| Result | Meaning |
|---|---|
| `accepted` | Applied. |
| `duplicate` | Already received; the earlier outcome is returned. |
| `failed` | Not applied. `error` is a readable message, and the device keeps the record and shows the error. |
| `held` | Waiting on a conflict. |
| `conflict` | A Sync Conflict was raised. |

**How the server handles a batch:**
- Every record gets exactly one result, in the order it was sent.
- Each record runs in its own savepoint, so one failure never blocks the others.
- A record already stored with status Accepted, Held or Conflict returns its stored result as `duplicate` (or `held` / `conflict`).
- A record stored as Failed is retried.

### `GET sync.status(session, uuids)`
`uuids` is a JSON list of up to 200. Returns `{"results": [...]}` in the same shape, so the device can see when a held or conflicted record has been resolved.

**Final states** (the device may purge these `purge_after_days` after it was acknowledged):
- `accepted`;
- `duplicate`;
- a record whose conflict was resolved: its status becomes Accepted, or **Discarded** when the decision kept the facility version. Discarded comes back as `result: "accepted"` with `"discarded": true`, and the payload stays on the server.

## 5. Device storage rules (O3)
- **Encryption.** IndexedDB values are AES-GCM encrypted under the data key. Index keys for search are HMAC-SHA-256 under a key derived from the data key, never plain names.
- **The data key** is stored only:
  - wrapped by a PIN key (PBKDF2-SHA-256, at least 310,000 iterations, random salt);
  - escrowed on the server.
- **Wrong PINs.** After `max_pin_attempts` wrong PINs, the wrapped key is deleted. Recovery is an online sign-in followed by `recover_key`.
- **Expiry.** After `expires_at`, unlock is refused until an online sign-in, which runs `enrol` again (a new secret and new expiry, same data key).
- **Retention.** A record stays in the outbox until it has a final result. It is then purged `purge_after_days` later. Records that are failed, held or in conflict are never purged.

## 6. Fix round 1 (review findings M1–M4, L1–L3, L5; 2026-11-14). These rules override earlier sections.

**F1. Closed sessions still sync their records (M1, NFR-11).**
- **Closed** means "no new work":
  - `enrol` is refused for a new device;
  - the package endpoints are refused.
- **What still works on a Closed session:**
  - The grant keeps working for `sync.push` and `sync.status` until `expires_at`. Records captured during the session are still accepted, so nothing on a tablet is lost.
  - `enrol` is still allowed to **renew** an existing grant for the same user and device. It gives a new secret and a new expiry, so a device whose grant expired after the session closed can still deliver its records.
- **Cancelled** sessions refuse everything.
- **Closing** is also refused while the session has **Failed** records, as well as Held or Conflict ones.

**F2. Correcting a failed record (M2).**
- The device may edit a record whose result is `failed` and send it again **with the same `client_uuid`**. The server stores the new payload, re-applies it, and records the attempt.
- Accepted, Held, Conflict and Discarded records are never re-applied, whatever payload is sent.

**F3. Dispensing in the field (M2, D58).**
- `get_reference` adds:
  - `dosage_units`: `{"<Prescription Dosage>": <doses per day>}`;
  - `period_days`: `{"<Prescription Duration>": <days>}`.
- The prescribed quantity of a line is `dosage_units[dosage] × period_days[period]`, at least 1. That matches the server (daily interval, DEFECT-RX-1). The app shows it and does not allow more to be dispensed.
- A field dispense closes the line (`final`), so the facility pharmacy does not dispense it again. A partial dispense is recorded as given.

**F4. `patient.update` without false conflicts (M3).**
- **New payload key `base_values`:** `{field: value as downloaded}` for each key in `changes`.
- **When the server raises a conflict:** only for a changed field whose server value now differs from its `base_values` value (the facility changed it) **and** differs from the device's value.
- **Without `base_values`,** the old `base_modified` rule applies.
- **Refused as `failed`:** a `base_modified` later than the patient's current `modified`.
- **The `accepted` result** of a `patient.update` carries `"modified"`, the patient's new `modified`. The device updates its local base, so a second edit on the same device chains correctly.

**F5. Patients a session may touch (M4).**
- `patient.update` and `visit.capture` (with `patient`) are accepted only for a patient who either:
  - lives in the session's catchment area; or
  - was registered or linked through this session's sync records.
- Any other patient gets `failed`: "This patient is not in the session's catchment area."

**F6. Robustness (L1–L3).**
- **Deadlocks:** a database deadlock or lock timeout while storing a record gives that record `failed` ("busy, try again"), not a failed call.
- **Held records:** records still Held on a **Resolved** conflict are replayed on the next `sync.push` or `sync.status` of the session.
- **Role check:** the grant is refused (401), and `push` is refused, once the user no longer holds the Outreach Team Member role.

**F7. App shell (L5).** The cached `/outreach` page carries **no** CSRF token or user. The app gets them from `www.outreach.get_boot` when it is online.
