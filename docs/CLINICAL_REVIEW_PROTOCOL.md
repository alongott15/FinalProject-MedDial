# Cohort clinical-review protocol

The lexical filter produces candidates, not a clinical severity determination.

## Inclusion

- Adult patient (18 or older), one admission per patient.
- Active, identifiable index complaint suitable for simulated consultation.
- Adequate documentation for a valid Structured Clinical Reference.
- Presenting episode judged lower acuity by the review protocol.

## Exclusion

- Neonatal encounter, ICU stay, in-hospital death, mechanical ventilation, shock, major trauma,
  or major surgery.
- ACS/STEMI/NSTEMI, stroke, sepsis, organ failure, active malignancy, or another clearly
  high-acuity presentation.
- A mild term that is negated, historical, incidental, or unrelated to the indexed encounter.

## Review procedure

1. Generate a private review template with `meddial-cohort review-template`.
2. Two clinicians independently record eligibility, index complaint, acuity label, reasons, and
   reviewer ID while blinded to generated dialogues and study condition.
3. Disagreement requires a separately identified adjudication record.
4. Any missing review or unresolved disagreement fails closed.
5. Revise rules only on the development cohort; freeze the guideline/filter version before the
   final cohort.
6. Report reviewer agreement and the exclusion flow in the manuscript.
