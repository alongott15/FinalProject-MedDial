"""Leak tests for the knowledge policies (KNOW-1..7, defects D-04 and D-05).

The fixture below is synthetic: a hand-written admission that exhibits the
leak pattern found in the real data. No MIMIC-III content appears here (C2).

The central claim these tests defend is that a masked field is *unreachable*
from a participant context, not merely absent from a prompt template.
"""

from __future__ import annotations

import json

import pytest

from meddial.knowledge import (
    Additional,
    Context,
    Core,
    Demographics,
    Diagnosis,
    DoctorContext,
    EvidenceSpan,
    MedicalHistory,
    Medication,
    ParticipantRole,
    PolicyRegistry,
    StructuredClinicalReference,
    Symptom,
    TreatmentOption,
    build_contexts,
    to_legacy_profile,
)
from meddial.knowledge.fieldpath import MISSING, addressable_paths, resolve
from meddial.knowledge.policy import (
    DeprecatedPolicyError,
    PolicyValidationError,
)

DIAGNOSIS = "Congestive Heart Failure"
LEAK_TERMS = ("congestive heart failure", "chf")

ACTIVE_NO_DIAGNOSIS = ("NO_DIAGNOSIS", "NO_DIAGNOSIS_NO_TREATMENT")


def _span(text: str) -> EvidenceSpan:
    return EvidenceSpan(
        note_id="synthetic-note", char_start=0, char_end=len(text), text=text
    )


@pytest.fixture
def reference() -> StructuredClinicalReference:
    """A synthetic admission where the diagnosis leaks through five fields."""
    return StructuredClinicalReference(
        row_id=1,
        subject_id=100,
        hadm_id=200,
        core=Core(
            symptoms=[
                Symptom(
                    description="shortness of breath on exertion",
                    evidence=[_span("dyspnea on exertion")],
                )
            ],
            diagnoses=[
                Diagnosis(
                    primary=DIAGNOSIS,
                    notes="ejection fraction 25 percent",
                    evidence=[_span(DIAGNOSIS)],
                )
            ],
            treatments=[
                TreatmentOption(
                    procedure="Diuresis",
                    details="IV diuresis for congestive heart failure",
                    treatment="fluid and sodium restriction",
                    medications=[
                        Medication(
                            name="Furosemide",
                            purpose="congestive heart failure",
                            evidence=[_span("Lasix 40mg IV")],
                        )
                    ],
                    evidence=[_span("diuresed with IV Lasix")],
                )
            ],
        ),
        context=Context(
            demographics=Demographics(age=68, sex="M"),
            medical_history=MedicalHistory(
                past_medical_history=(
                    "Type 2 diabetes, hypertension, and congestive heart failure "
                    "diagnosed in 2015."
                )
            ),
            allergies=["penicillin"],
            current_medications=[
                Medication(
                    name="Metoprolol",
                    purpose="congestive heart failure",
                    evidence=[_span("metoprolol 25mg BID")],
                )
            ],
            discharge_medications=[
                Medication(
                    name="Lisinopril",
                    purpose="CHF",
                    evidence=[_span("lisinopril 10mg daily")],
                )
            ],
        ),
        additional=Additional(chief_complaint="Worsening CHF symptoms for three days"),
    )


@pytest.fixture
def registry() -> PolicyRegistry:
    return PolicyRegistry()


def _patient_text(context) -> str:
    return json.dumps(context.visible, sort_keys=True).lower()


def test_masked_fields_unreachable_from_patient_context(
    registry: PolicyRegistry, reference: StructuredClinicalReference
) -> None:
    """Every policy, every masked path: nothing resolves. The leak test."""
    for policy in registry.all():
        contexts = build_contexts(reference, policy)
        for path in policy.patient_masked:
            assert (
                resolve(contexts.patient.visible, path) is MISSING
            ), f"{policy.key} leaks {path}"
        # Anything the policy never declared visible is absent too (KNOW-3).
        # A container on the way to a visible field is not itself a leak.
        for path in addressable_paths():
            if any(
                path == v
                or path.startswith((f"{v}.", f"{v}["))
                or v.startswith((f"{path}.", f"{path}["))
                for v in policy.patient_visible
            ):
                continue
            assert (
                resolve(contexts.patient.visible, path) is MISSING
            ), f"{policy.key} exposes undeclared {path}"


@pytest.mark.parametrize("policy_id", ACTIVE_NO_DIAGNOSIS)
def test_no_diagnosis_policy_leaks_no_diagnosis_terms(
    registry: PolicyRegistry,
    reference: StructuredClinicalReference,
    policy_id: str,
) -> None:
    """D-04: the term must not survive anywhere in the patient payload."""
    policy = registry.for_confirmatory_run(policy_id)
    contexts = build_contexts(reference, policy)
    payload = _patient_text(contexts.patient)

    for term in LEAK_TERMS:
        assert term not in payload, f"{policy.key} leaks '{term}'"
    assert contexts.patient.redactions.total > 0


@pytest.mark.parametrize("policy_id", ACTIVE_NO_DIAGNOSIS)
def test_medications_dropped_under_both_no_diagnosis_policies(
    registry: PolicyRegistry,
    reference: StructuredClinicalReference,
    policy_id: str,
) -> None:
    """A medication names the condition it treats, so the list cannot stay."""
    policy = registry.for_confirmatory_run(policy_id)
    visible = build_contexts(reference, policy).patient.visible

    assert resolve(visible, "context.current_medications") is MISSING
    assert resolve(visible, "context.discharge_medications") is MISSING
    assert resolve(visible, "core.treatments[].medications") is MISSING


def test_ndnt_strictly_more_restrictive_than_no_diagnosis(
    registry: PolicyRegistry, reference: StructuredClinicalReference
) -> None:
    """The arms must form a ladder, not three unrelated conditions."""
    nd = build_contexts(
        reference, registry.for_confirmatory_run("NO_DIAGNOSIS")
    ).patient.visible
    ndnt = build_contexts(
        reference, registry.for_confirmatory_run("NO_DIAGNOSIS_NO_TREATMENT")
    ).patient.visible

    narrowed = False
    for path in addressable_paths():
        in_ndnt = resolve(ndnt, path) is not MISSING
        in_nd = resolve(nd, path) is not MISSING
        if in_ndnt:
            assert in_nd, f"{path} is visible under NDNT but not under NO_DIAGNOSIS"
        elif in_nd:
            narrowed = True
    assert narrowed, "NDNT hides nothing that NO_DIAGNOSIS shows"


def test_past_history_retained_but_redacted(
    registry: PolicyRegistry, reference: StructuredClinicalReference
) -> None:
    """KNOW-5: the patient keeps their history minus the index diagnosis."""
    policy = registry.for_confirmatory_run("NO_DIAGNOSIS")
    contexts = build_contexts(reference, policy)
    history = resolve(
        contexts.patient.visible, "context.medical_history.past_medical_history"
    )

    assert history is not MISSING
    assert "[REDACTED]" in history
    assert "congestive heart failure" not in history.lower()
    # Comorbidities are not the index diagnosis and must survive.
    assert "diabetes" in history.lower()
    assert "hypertension" in history.lower()


def test_deprecated_policy_refused_by_confirmatory_runner(
    registry: PolicyRegistry,
) -> None:
    """The thesis arms stay replayable, but cannot produce a reported result."""
    with pytest.raises(DeprecatedPolicyError):
        registry.for_confirmatory_run("NO_DIAGNOSIS", "1.0")

    replayed = registry.load("NO_DIAGNOSIS", "1.0")
    assert replayed.deprecated

    # Without a version, the active policy is selected.
    assert registry.load("NO_DIAGNOSIS").version == "2.0"


def test_thesis_policy_leaks_what_the_current_one_does_not(
    registry: PolicyRegistry, reference: StructuredClinicalReference
) -> None:
    """Records D-04 as a measurable quantity for the E0 comparison arm."""
    thesis = build_contexts(reference, registry.load("NO_DIAGNOSIS", "1.0"))

    assert "congestive heart failure" in _patient_text(thesis.patient)


def test_policy_version_bump_required_on_change(
    tmp_path, registry: PolicyRegistry
) -> None:
    """KNOW-4: editing a policy body in place must fail, not pass silently."""
    policy = registry.load("FULL", "2.0")
    body = {
        "policy_id": policy.policy_id,
        "version": policy.version,
        "patient_visible": sorted(policy.patient_visible),
        "patient_masked": sorted(policy.patient_masked),
        "redact_index_diagnosis_terms_in": sorted(
            policy.redact_index_diagnosis_terms_in
        ),
        "doctor_visible": sorted(policy.doctor_visible),
        "rationale": policy.rationale,
    }
    (tmp_path / "full.json").write_text(json.dumps(body))
    (tmp_path / "POLICY_HASHES.json").write_text(
        json.dumps({policy.key: policy.content_hash})
    )
    assert (
        PolicyRegistry(tmp_path).load("FULL", "2.0").content_hash == policy.content_hash
    )

    body["patient_visible"] = [
        p for p in body["patient_visible"] if p != "core.diagnoses"
    ]
    body["patient_masked"] = ["core.diagnoses"]
    (tmp_path / "full.json").write_text(json.dumps(body))

    with pytest.raises(PolicyValidationError, match="Bump the version"):
        PolicyRegistry(tmp_path).all()


def test_policy_refuses_to_leave_a_field_unclassified() -> None:
    """KNOW-3: fail closed, so a new reference field cannot slip through."""
    from meddial.knowledge import KnowledgePolicy

    with pytest.raises(PolicyValidationError, match="does not classify"):
        KnowledgePolicy.from_mapping(
            {
                "policy_id": "PARTIAL",
                "version": "1.0",
                "patient_visible": ["core.symptoms"],
                "patient_masked": ["core.diagnoses"],
                "redact_index_diagnosis_terms_in": [],
                "doctor_visible": [],
            }
        )


def test_doctor_guidance_independent_of_patient_policy(
    registry: PolicyRegistry, reference: StructuredClinicalReference
) -> None:
    """D-05: the doctor's instructions are a separate factor from disclosure."""
    policy = registry.for_confirmatory_run("NO_DIAGNOSIS_NO_TREATMENT")

    default = build_contexts(reference, policy)
    assert default.doctor.guidance_id == policy.policy_id

    crossed = build_contexts(reference, policy, guidance_id="FULL")
    assert isinstance(crossed.doctor, DoctorContext)
    assert crossed.doctor.guidance_id == "FULL"
    assert crossed.patient.policy_id == "NO_DIAGNOSIS_NO_TREATMENT"
    # Crossing the factors must not change what the patient knows.
    assert crossed.patient.visible == default.patient.visible


@pytest.mark.parametrize("policy_id", ACTIVE_NO_DIAGNOSIS)
def test_legacy_profile_handed_to_the_agents_is_masked(
    registry: PolicyRegistry,
    reference: StructuredClinicalReference,
    policy_id: str,
) -> None:
    """The shape the pipeline actually passes to PatientAgent must be clean."""
    policy = registry.for_confirmatory_run(policy_id)
    contexts = build_contexts(reference, policy)
    profile = to_legacy_profile(contexts.patient)

    assert profile["Core_Fields"]["Diagnoses"] == []
    assert profile["Context_Fields"]["Current_Medications"] == []
    assert profile["Context_Fields"]["Discharge_Medications"] == []
    assert profile["profile_type"] == policy_id
    assert profile["policy_version"] == policy.version

    serialised = json.dumps(profile).lower()
    for term in LEAK_TERMS:
        assert term not in serialised, f"{policy.key} leaks '{term}' into the profile"


def test_evidence_survives_roundtrip(reference: StructuredClinicalReference) -> None:
    """KNOW-1: provenance is preserved on disk, and never leaves the evaluator."""
    restored = StructuredClinicalReference.model_validate(
        json.loads(reference.model_dump_json(by_alias=True))
    )

    assert restored.core.diagnoses[0].evidence == reference.core.diagnoses[0].evidence
    assert restored.case_id == "100_200"
    assert restored.unevidenced_entities() == []

    registry = PolicyRegistry()
    contexts = build_contexts(restored, registry.for_confirmatory_run("FULL"))
    assert "evidence" not in json.dumps(contexts.patient.visible)
    assert "evidence" not in json.dumps(contexts.doctor.visible)
    # The evaluator is the only role that keeps it (KNOW-7).
    evaluator_view = contexts.evaluator.policy.mask(
        contexts.evaluator.reference, ParticipantRole.EVALUATOR
    )
    assert evaluator_view["core"]["diagnoses"][0]["evidence"]


def test_unevidenced_entity_is_reported_not_raised(
    reference: StructuredClinicalReference,
) -> None:
    """Extraction recall is a measured quantity, so this is a finding."""
    reference.core.diagnoses[0].evidence = []

    assert "core.diagnoses[0]" in reference.unevidenced_entities()
