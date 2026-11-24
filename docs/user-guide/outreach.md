# Outreach: user guide

This guide is for outreach team members, the HIS Officer and the Medical In-Charge.

## Before the session (HIS Officer or Medical In-Charge, on the desk)
1. **Catchment Area** (the **Outreach** area in Marley's dock, beside Outpatient, Inpatient and Rehabilitation): name the area, choose the facility, and list the residence locations it covers. Each location includes everything under it.
2. **Outreach Session**:
   - title, facility, catchment area and dates;
   - the **team members**, each holding the Outreach Team Member role (tick the team lead);
   - the **departments offered**, e.g. Outpatient, MCH or Nutrition. Mental Health is never offered offline.
3. **Point-of-care tests:** the tests flagged "Point-of-Care Test (Outreach)" on their Observation Template are the ones the team can enter in the field. Provisional list: MRDT, haemoglobin, random blood sugar, urine pregnancy test, HIV 1/2 rapid test.

## On the tablet, while still connected (team member)
1. Open `https://<site>/outreach` in Chrome (Android) or Safari (iPad), and add it to the home screen.
2. Sign in with **your own** account, choose the session, and wait for the download to finish:
   - the patients of the catchment area (a short summary, never mental-health records);
   - the ICD-11 codes, medicines and tests.
3. Set a PIN of at least 6 digits.
   - The app now works offline for **7 days**.
   - It locks after 15 minutes idle; unlock it with the PIN.
   - After 5 wrong PINs, sign in online again. Your records are kept.
4. **Wrong account, or no session listed?** Use **Sign out** (top right) on the session, PIN or download screen, and tap it again to confirm. It cancels the setup of this tablet so the right person can sign in. Sign out is refused if anything on the tablet is not yet synced, so nothing is lost.

## In the field (offline)
- **Find a patient** by name or identifier, or **register** a new one:
  - an exact date of birth, or the age told;
  - an identifier, or walk-in.

  The app warns you when someone similar already exists.
- **Record a visit:** vital signs → consultation with ICD-11 diagnoses → lab requests (enter the result now for a point-of-care test) → prescriptions (tick "dispensed now" with the quantity when you give the medicine) → outcome → **Finish**.
  - Unfinished visits stay as drafts on the tablet.
  - Lab requests without a result go to the facility laboratory.
  - Prescriptions not dispensed go to the facility pharmacy.
- **Record a health talk or home visit.**

## Syncing
- The app syncs by itself when the tablet has a connection; you can also press **Sync now**.
- Each record shows one of:

  | Status | Meaning |
  |---|---|
  | **Accepted** | It is in the EMR. |
  | **Failed** | Correct it as the message says; it is retried. |
  | **Held / Conflict** | The HIS Officer must decide. It stays on the tablet until then. |
- Records are deleted from the tablet 7 days after the EMR has accepted them.
- When back at the facility, the team lead marks the session **Returned** from the app's home screen.

## Sync conflicts (HIS Officer, or Medical In-Charge as backup)
Desk → **Outreach** → **Open Sync Conflicts** (also a shortcut on the HIS block of the Healthcare page). A patient with an open conflict shows an orange banner on their record. Open a conflict to see both versions side by side, then choose:

| Conflict | Choices |
|---|---|
| Duplicate registration | **Link as the same patient** (the outreach visits go to the existing record), **Keep outreach version** (create a new patient), **Keep facility version** (discard the outreach registration; its visits go to the existing patient), or **Merge field by field** |
| Demographic change | Keep facility, keep outreach, or merge field by field |
| Overlapping visit | The patient had an **open** facility visit: close that visit, then **Retry**, or keep the facility version. If the facility visit on the same day was already closed, review it and choose **Reviewed - keep both**. |

Every decision needs a reason and is recorded with your name and the time. Nothing is ever deleted: a discarded outreach record stays in its Outreach Sync Record.

## Lost tablet
Follow `docs/outreach-lost-device.md` the same day.
