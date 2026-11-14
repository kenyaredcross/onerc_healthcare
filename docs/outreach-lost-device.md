# Lost or stolen outreach tablet: procedure (OUT-25, DR-3)

**Who acts:** the outreach team lead reports. The HIS Officer, Medical In-Charge or EMR System Administrator revokes.
**When:** as soon as a tablet is lost, stolen, or out of the team's control for any length of time.

## Why this matters
An outreach tablet holds patient data offline:
- the catchment patients' summaries;
- any records not yet synced.

The tablet can't be wiped remotely while it is offline. The protections are:
- the tablet's own storage encryption (a deployment requirement);
- the app's encryption (AES-GCM; the key is unlocked by the user's PIN);
- the package being limited to one catchment area;
- the offline session ending after 7 days;
- this procedure.

**Weak point:** someone who copies the tablet's storage can try every possible PIN offline. A 6-digit PIN slows them down but does not stop them, so **report quickly**.

## Steps
1. **Report (team lead, same day).** Call or message the HIS Officer, giving the tablet label and the outreach session.
2. **Revoke the device (HIS Officer, Medical In-Charge or EMR System Administrator).**
   - Desk → Healthcare → Outreach → **Outreach Device**.
   - Open the device by its label and choose **Revoke**, with the reason, e.g. "Lost on 2026-11-20, Village 3".
   - Every grant on that device stops working at once. The tablet can no longer sync or download a package, even with the PIN.
3. **Change the password (HIS Officer / System Administrator).** Reset the password of every user who signed in on that tablet. Any session cookie still on the tablet then stops working.
4. **Find what was not synced (HIS Officer).**
   - Open **Outreach Sync Batch**, filtered by the device, to see the last successful sync.
   - Records captured after that and never synced are lost with the tablet. Ask the team to re-enter them on paper or on another tablet, from memory and any notes.
5. **Record the incident (Medical In-Charge).**
   - Note it in the facility's data-protection incident log: the date, the catchment area, the number of patients in the package (from the session) and the time between the loss and the revocation.
   - Follow KRCS's personal-data breach process. Under the Data Protection Act 2019, the ODPC may need to be notified within 72 hours. That is the Data Protection Officer's decision.
6. **If the tablet is found:** do not reuse it as it is. Reinstall the app (clear the browser data for the site) so it enrols as a new device. A revoked device stays revoked.

## Prevention
- Turn on device encryption and a screen lock on every tablet before issuing it.
- Keep tablets with the team lead between sessions.
- Sync whenever there is a connection; the app syncs automatically when the tablet comes online.
- Never share a sign-in or a PIN. Each team member has their own account (NFR-8).
