"""Role-aware claim classification and clinical faithfulness scoring."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Protocol

from meddial.evaluation.models import EvaluationStatus, MetricResult
from meddial.knowledge import EvaluatorContext
from meddial.llm import ChatMessage, LLMProvider


class ClaimType(str, Enum):
    PATIENT_FACT = "patient_fact"
    DOCTOR_FACT = "doctor_fact"
    QUESTION = "question"
    DIAGNOSTIC_HYPOTHESIS = "diagnostic_hypothesis"
    RECOMMENDATION = "recommendation"
    ADVICE = "advice"
    NON_MEDICAL = "non_medical"


@dataclass(frozen=True)
class Claim:
    role: str
    text: str
    claim_type: ClaimType
    turn_index: int

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["claim_type"] = self.claim_type.value
        return data


@dataclass(frozen=True)
class ClaimExtractionResult:
    status: EvaluationStatus
    claims: tuple[Claim, ...]
    reason: str


class ClaimExtractor(Protocol):
    def extract(self, dialogue: Sequence[Mapping[str, str]]) -> ClaimExtractionResult: ...


_QUESTION_STARTS = (
    "what ", "when ", "where ", "why ", "how ", "do ", "does ", "did ",
    "is ", "are ", "can ", "could ", "have ", "has ", "would ", "will ",
)
_HYPOTHESIS_MARKERS = (
    "may be", "might be", "could be", "possibly", "likely", "sounds like",
    "working diagnosis", "differential", "i suspect", "i think this is",
)
_RECOMMENDATION_MARKERS = (
    "i recommend", "we recommend", "i suggest", "you should", "you could try",
    "the plan is", "start taking", "stop taking", "prescribe",
)
_ADVICE_MARKERS = (
    "seek care", "call", "return if", "watch for", "rest", "drink fluids",
    "follow up", "avoid", "monitor",
)
_NON_MEDICAL_PHRASES = (
    "hello",
    "hi",
    "good morning",
    "good afternoon",
    "thank you",
    "thanks",
    "you are welcome",
    "you're welcome",
    "i understand",
    "that makes sense",
    "okay",
    "ok",
    "yes",
    "no",
    "goodbye",
)


def classify_claim(role: str, text: str) -> ClaimType:
    normalized = text.strip().lower()
    if not normalized:
        return ClaimType.NON_MEDICAL
    punctuation_stripped = normalized.strip(" .,!;:")
    if punctuation_stripped in _NON_MEDICAL_PHRASES:
        return ClaimType.NON_MEDICAL
    if normalized.endswith("?") or normalized.startswith(_QUESTION_STARTS):
        return ClaimType.QUESTION
    if role.lower() == "doctor":
        if any(marker in normalized for marker in _HYPOTHESIS_MARKERS):
            return ClaimType.DIAGNOSTIC_HYPOTHESIS
        if any(marker in normalized for marker in _RECOMMENDATION_MARKERS):
            return ClaimType.RECOMMENDATION
        if any(marker in normalized for marker in _ADVICE_MARKERS):
            return ClaimType.ADVICE
        return ClaimType.DOCTOR_FACT
    if role.lower() == "patient":
        return ClaimType.PATIENT_FACT
    return ClaimType.NON_MEDICAL


class RuleBasedClaimExtractor:
    """Deterministic baseline used in tests and as a no-call fallback."""

    def extract(self, dialogue: Sequence[Mapping[str, str]]) -> ClaimExtractionResult:
        if not dialogue:
            return ClaimExtractionResult(
                EvaluationStatus.UNSCORABLE, (), "Dialogue contains no turns"
            )
        claims: list[Claim] = []
        for index, turn in enumerate(dialogue):
            role = str(turn.get("role", "Unknown"))
            content = str(turn.get("content", "")).strip()
            if not content:
                continue
            sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+|\n+", content)]
            for sentence in sentences:
                if sentence:
                    claims.append(
                        Claim(
                            role=role,
                            text=sentence,
                            claim_type=classify_claim(role, sentence),
                            turn_index=index,
                        )
                    )
        if not claims:
            return ClaimExtractionResult(
                EvaluationStatus.UNSCORABLE, (), "No claims could be extracted from non-empty dialogue"
            )
        return ClaimExtractionResult(EvaluationStatus.PASS, tuple(claims), "Claims extracted")


class LLMClaimExtractor:
    """Strict JSON claim extractor. Parse or provider errors never become empty success."""

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    def extract(self, dialogue: Sequence[Mapping[str, str]]) -> ClaimExtractionResult:
        if not dialogue:
            return ClaimExtractionResult(EvaluationStatus.UNSCORABLE, (), "Dialogue contains no turns")
        prompt = json.dumps(list(dialogue), ensure_ascii=False)
        messages = [
            ChatMessage(
                role="system",
                content=(
                    "Extract every atomic claim from both patient and doctor turns. Return only "
                    "a JSON array with role, text, turn_index and claim_type. claim_type must be "
                    "one of patient_fact, doctor_fact, question, diagnostic_hypothesis, "
                    "recommendation, advice, non_medical."
                ),
            ),
            ChatMessage(role="user", content=prompt),
        ]
        try:
            response = self.provider.generate(messages).content
        except Exception as exc:
            return ClaimExtractionResult(EvaluationStatus.ERROR, (), f"Claim extraction failed: {exc}")
        try:
            match = re.search(r"\[.*\]", response, re.DOTALL)
            if not match:
                raise ValueError("response did not contain a JSON array")
            data = json.loads(match.group(0))
            claims = tuple(
                Claim(
                    role=str(item["role"]),
                    text=str(item["text"]),
                    claim_type=ClaimType(item["claim_type"]),
                    turn_index=int(item["turn_index"]),
                )
                for item in data
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return ClaimExtractionResult(EvaluationStatus.ERROR, (), f"Invalid claim extraction: {exc}")
        if not claims:
            return ClaimExtractionResult(
                EvaluationStatus.UNSCORABLE,
                (),
                "Extractor returned no claims for a non-empty dialogue",
            )
        return ClaimExtractionResult(EvaluationStatus.PASS, claims, "Claims extracted")


def _entity_terms(reference: Mapping[str, Any]) -> set[str]:
    terms: set[str] = set()
    core = reference.get("Core_Fields", {})
    context = reference.get("Context_Fields", {})
    extra = reference.get("Additional_Context", {})
    for symptom in core.get("Symptoms", []):
        if isinstance(symptom, Mapping):
            for key in ("description", "onset", "duration", "severity"):
                value = symptom.get(key, "")
                if value:
                    terms.add(str(value).strip().lower())
        elif symptom:
            terms.add(str(symptom).strip().lower())
    for diagnosis in core.get("Diagnoses", []):
        value = diagnosis.get("primary", "") if isinstance(diagnosis, Mapping) else diagnosis
        if value:
            terms.add(str(value).strip().lower())
    for treatment in core.get("Treatment_Options", []):
        if isinstance(treatment, Mapping):
            for key in ("procedure", "treatment"):
                value = treatment.get(key, "")
                if value and value != "not provided":
                    terms.add(str(value).strip().lower())
            for med in treatment.get("medications", []):
                value = med.get("name", "") if isinstance(med, Mapping) else med
                if value:
                    terms.add(str(value).strip().lower())
    for key in ("Current_Medications", "Discharge_Medications"):
        for med in context.get(key, []):
            value = med.get("name", "") if isinstance(med, Mapping) else med
            if value:
                terms.add(str(value).strip().lower())
    history = context.get("Medical_History", {})
    if isinstance(history, Mapping):
        value = history.get("Past_Medical_History", "")
        if value:
            terms.add(str(value).strip().lower())
    for allergy in context.get("Allergies", []):
        if allergy:
            terms.add(str(allergy).strip().lower())
    chief = extra.get("Chief_Complaint", "")
    if chief and chief != "not provided":
        terms.add(str(chief).strip().lower())
    return {
        term
        for term in terms
        if term and term.lower() not in {"not provided", "unknown", "none"}
    }


class RoleAwareClinicalFaithfulness:
    """Checks factual claims from both roles against the clinical reference."""

    def __init__(
        self,
        extractor: ClaimExtractor | None = None,
        checker: Callable[[Claim, Mapping[str, Any]], bool | None] | None = None,
        threshold: float = 0.70,
    ) -> None:
        self.extractor = extractor or RuleBasedClaimExtractor()
        self.checker = checker or self._default_checker
        self.threshold = threshold

    def _default_checker(self, claim: Claim, reference: Mapping[str, Any]) -> bool | None:
        if claim.claim_type in {
            ClaimType.QUESTION,
            ClaimType.DIAGNOSTIC_HYPOTHESIS,
            ClaimType.NON_MEDICAL,
        }:
            return None
        normalized = claim.text.lower()
        terms = _entity_terms(reference)
        if any(term in normalized or normalized in term for term in terms):
            return True
        # Recommendations/advice are evaluated only when they name a reference entity.
        if claim.claim_type in {ClaimType.RECOMMENDATION, ClaimType.ADVICE}:
            return None
        return False

    def evaluate(
        self, dialogue: Sequence[Mapping[str, str]], context: EvaluatorContext
    ) -> MetricResult:
        extraction = self.extractor.extract(dialogue)
        if extraction.status in {EvaluationStatus.ERROR, EvaluationStatus.UNSCORABLE}:
            return MetricResult(
                name="role_aware_clinical_faithfulness",
                status=extraction.status,
                score=None,
                reason=extraction.reason,
                details={"claim_count": 0},
            )

        reference = context.reference_dict()
        checked: list[tuple[Claim, bool]] = []
        try:
            for claim in extraction.claims:
                verdict = self.checker(claim, reference)
                if verdict is not None:
                    checked.append((claim, verdict))
        except Exception as exc:
            return MetricResult(
                name="role_aware_clinical_faithfulness",
                status=EvaluationStatus.ERROR,
                score=None,
                reason=f"Claim checking failed: {exc}",
                details={"claim_count": len(extraction.claims)},
            )
        if not checked:
            return MetricResult(
                name="role_aware_clinical_faithfulness",
                status=EvaluationStatus.UNSCORABLE,
                score=None,
                reason="No verifiable patient or doctor factual claims were found",
                details={"claim_count": len(extraction.claims)},
            )
        supported = sum(1 for _, verdict in checked if verdict)
        score = supported / len(checked)
        return MetricResult(
            name="role_aware_clinical_faithfulness",
            status=EvaluationStatus.PASS if score >= self.threshold else EvaluationStatus.FAIL,
            score=score,
            reason=f"{supported}/{len(checked)} verifiable claims were supported",
            details={
                "claim_count": len(extraction.claims),
                "verifiable_claim_count": len(checked),
                "patient_claim_count": sum(c.role.lower() == "patient" for c in extraction.claims),
                "doctor_claim_count": sum(c.role.lower() == "doctor" for c in extraction.claims),
                "unsupported_claims": [c.to_dict() for c, ok in checked if not ok],
                "claims": [c.to_dict() for c in extraction.claims],
            },
        )
