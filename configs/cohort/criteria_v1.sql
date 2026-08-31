-- MedDial structured cohort candidate query
-- criteria: mimiciii_structured_lower_acuity@1.1
--
-- This query deliberately returns excluded as well as eligible admissions.
-- meddial.cohort.criteria applies E1-E9 and records every criterion that fired;
-- meddial.cohort.select then applies E10 and produces sequential flow counts.
-- No clinical eligibility decision is made from note vocabulary.  NOTEEVENTS
-- supplies only the deterministic E9 length/category adequacy input.

WITH discharge_notes AS (
    SELECT
        n.subject_id,
        n.hadm_id,
        n.row_id,
        n.category AS note_category,
        n.text AS note_text,
        ROW_NUMBER() OVER (
            PARTITION BY n.subject_id, n.hadm_id
            ORDER BY n.chartdate DESC NULLS LAST, n.row_id DESC
        ) AS note_rank
    FROM noteevents AS n
    WHERE LOWER(n.category) = 'discharge summary'                 -- E9
),
icu_flags AS (
    SELECT i.hadm_id, TRUE AS has_icu_stay
    FROM icustays AS i                                             -- E1
    GROUP BY i.hadm_id
),
procedure_codes AS (
    SELECT
        p.hadm_id,
        ARRAY_AGG(DISTINCT REPLACE(UPPER(p.icd9_code), '.', ''))
            FILTER (WHERE p.icd9_code IS NOT NULL) AS procedure_icd9_codes
    FROM procedures_icd AS p                                      -- E3
    GROUP BY p.hadm_id
),
diagnosis_codes AS (
    SELECT
        d.hadm_id,
        ARRAY_AGG(DISTINCT REPLACE(UPPER(d.icd9_code), '.', ''))
            FILTER (WHERE d.icd9_code IS NOT NULL) AS diagnosis_icd9_codes
    FROM diagnoses_icd AS d                                       -- E6, E8
    GROUP BY d.hadm_id
)
SELECT
    a.subject_id,
    a.hadm_id,
    dn.row_id,
    a.admittime,
    a.dischtime,
    DATE_PART('year', AGE(a.admittime, p.dob)) AS age_years,       -- E4, E5
    -- The band is [minimum_age_years, maximum_age_years_exclusive),
    -- 12 and 90 at criteria 1.1. The thresholds live in the Python
    -- criteria body, not here, so this query is version-agnostic.
    a.admission_type,                                              -- E4
    COALESCE(i.has_icu_stay, FALSE) AS has_icu_stay,               -- E1
    COALESCE(a.hospital_expire_flag, 0) AS hospital_expire_flag,   -- E2
    a.deathtime,                                                   -- E2
    COALESCE(pc.procedure_icd9_codes, ARRAY[]::TEXT[])
        AS procedure_icd9_codes,                                   -- E3
    COALESCE(dc.diagnosis_icd9_codes, ARRAY[]::TEXT[])
        AS diagnosis_icd9_codes,                                   -- E6, E8
    dn.note_category,
    dn.note_text,                                                  -- E9
    a.dischtime - a.admittime AS length_of_stay                    -- E7
FROM admissions AS a
JOIN patients AS p
    ON p.subject_id = a.subject_id
LEFT JOIN discharge_notes AS dn
    ON dn.subject_id = a.subject_id
   AND dn.hadm_id = a.hadm_id
   AND dn.note_rank = 1
LEFT JOIN icu_flags AS i
    ON i.hadm_id = a.hadm_id
LEFT JOIN procedure_codes AS pc
    ON pc.hadm_id = a.hadm_id
LEFT JOIN diagnosis_codes AS dc
    ON dc.hadm_id = a.hadm_id
ORDER BY a.subject_id, a.admittime, a.hadm_id;

-- E8: Charlson categories and weights are evaluated from diagnosis_icd9_codes
--     by calculate_charlson(), using the versioned Python criteria body.
-- E10: among admissions surviving E1-E9, select.py keeps the earliest
--      (admittime, hadm_id) for each subject before seeded sampling.
