# Maternal and Child Health (MCH): user guide

Antenatal care, postnatal checks, immunisation and child growth monitoring.

The fields are **provisional**: they follow the Kenya MOH Mother & Child Health Handbook, the KEPI schedule and the WHO growth standards until the MCH In-Charge confirms them (D62, OI-8).

## Who does what

| Role | Can record |
|---|---|
| Clinical Officer, Midwife, Medical In-Charge | Every MCH record |
| Triage Nurse | Immunisation and Growth Monitoring |
| HIS Officer, Mental Health Officer | Read only |

## Where to find it
- **Outpatient sidebar → MCH:**
  - MCH Queue;
  - Open Pregnancies, Antenatal Contacts, Postnatal Checks;
  - Immunisations, **Due Immunisations**, Growth Monitoring;
  - KEPI Schedule.
- **On an open visit**, or a draft consultation in the MCH clinic, use the **MCH** button group. It only offers what fits the patient:
  - **women:** Book Pregnancy / New Antenatal Contact, and Postnatal Check;
  - **babies under 1 year:** Postnatal Check;
  - **children under 5:** Immunisation and Growth Monitoring.

  If the record already exists for the visit, the button opens it.
- **Patient form, Maternal and Child Health panel:**
  - the open pregnancy and its EDD;
  - each vaccine dose as a coloured pill: Given (green), Due (blue), Overdue (orange), Missed (red), Upcoming (grey);
  - the weight-for-age chart.

## Antenatal care
1. At the first contact, **Book Pregnancy**: the LMP (or the EDD from ultrasound), gravida, para and the booking tests. A mother has one open pregnancy at a time.
2. At each contact, **New Antenatal Contact** records:
   - the examination: weight, BP, fundal height, presentation, foetal heart;
   - urine;
   - Td and IPTp doses, iron/folate, deworming, the mosquito net;
   - danger signs;
   - the next visit date.

   The contact number and the gestation are worked out for you. **Submit** when done.
3. **Admitted in labour:** the admit dialog fills the LMP, gravida and para from the pregnancy. Submitting the **Delivery Record** closes the pregnancy as Delivered.
4. **Miscarriage, abortion or transfer:** open the pregnancy and use **Close Pregnancy**.

## Postnatal checks
- **Check of:** choose Mother or Baby.
- **On the ward** (the mother is still admitted), choose her **Maternity Admission** instead of a visit.
- The delivery and the days since delivery are filled in for you.

## Immunisation
- Add one row per dose: the vaccine dose (e.g. "Penta 2"), batch and site. For a dose not given, choose "Not given" and give the reason.
- The system refuses:
  - a dose already given;
  - a dose more than 14 days early;
  - a dose sooner than the minimum interval after the previous one.

  It tells you the date the dose can be given.
- **Due Immunisations** lists the children due or overdue at your facility, with the mother's phone number for follow-up.

## Growth monitoring
- Enter the weight, length/height (and whether the child was measured lying or standing), MUAC, head circumference and oedema.
- **Z-scores** (weight-for-age, length/height-for-age, weight-for-length/height) and the classification (underweight, stunting, moderate or severe acute malnutrition) are calculated on save.
- When the child's date of birth is estimated, the age-based results are approximate, and the note says so.

## Corrections
- **After submit:** a record cannot be edited or cancelled. Use **Add Addendum**, a dated, signed correction.
- **Records made in error** (wrong patient or visit): the **Medical In-Charge** uses **Archive**. Nothing is deleted.

## MOH 717 (provisional)
- **B07 Child welfare clinic attendances:** child visits with an immunisation or growth record.
- **B08 Antenatal attendances:** ANC contacts.
- **B09 Postnatal attendances:** postnatal checks.
