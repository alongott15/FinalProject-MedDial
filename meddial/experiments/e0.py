"""E0 — is the disclosure→faithfulness trend a property of generation or of measurement?

The thesis reports patient faithfulness rising as disclosure is restricted
(0.739 → 0.787 → 0.835) and reads it as a generation finding. Two of the four
confounds in plan §6 can be tested by re-scoring dialogues that already
exist, with no regeneration:

* **Test 1 · reference scope.** The thesis scored claims against the *policy
  context*, which shrinks as disclosure is restricted. A smaller reference is
  a smaller surface to contradict, so the score can rise while the dialogues
  get no better. Re-scoring the same dialogues against the full reference
  separates the two.
* **Test 2 · turn scope.** Only patient turns were scored, so a doctor who
  invented a lab value was never penalised. Scoring both roles says whether
  the trend is a property of the dialogue or of the half of it measured.

Tests 3 and 4 need regeneration and are deliberately absent — plan §6 step 4
makes them conditional on tests 1-2 leaving the trend standing.

**Nothing here decides what the trend means.** This module produces the
decomposition; the framing decision is plan §6 step 5 and belongs to a human.

Cost shape: claim extraction runs **once per dialogue** and is reused across
both reference modes and both roles. Extraction does not depend on the
reference, so re-extracting per mode would double the extraction calls and,
worse, let the two modes disagree because they were shown different claim
sets. Per dialogue: 1 extraction + 4 verifications, not 2 + 4.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meddial.evaluation import (
    DOCTOR_ROLE,
    PATIENT_ROLE,
    ClaimExtractionError,
    ClaimSet,
    EvaluationStatus,
    ReferenceMode,
    Score,
    ScoreProvenance,
    Turn,
    TurnScope,
    build_turns,
    extract_claims,
    load_prompt,
    score_faithfulness,
)
from meddial.evaluation.acceptance import DOCTOR_FACTUALITY, PATIENT_FACTUALITY
from meddial.knowledge import (
    KnowledgePolicy,
    PolicyRegistry,
    StructuredClinicalReference,
    build_contexts,
)
from meddial.llm import LLMProvider
from meddial.stats import (
    Interval,
    PairedResult,
    StatsError,
    case_clustered_bootstrap,
    paired_difference,
)

POLICY_ORDER: tuple[str, ...] = ("FULL", "NO_DIAGNOSIS", "NO_DIAGNOSIS_NO_TREATMENT")
"""Increasing restriction. The thesis trend runs left to right along this axis."""

REFERENCE_MODES: tuple[ReferenceMode, ...] = (
    ReferenceMode.POLICY_CONTEXT,
    ReferenceMode.FULL_REFERENCE,
)

DIMENSION_FOR_ROLE = {PATIENT_ROLE: PATIENT_FACTUALITY, DOCTOR_ROLE: DOCTOR_FACTUALITY}
TURN_SCOPE_FOR_ROLE = {PATIENT_ROLE: TurnScope.PATIENT, DOCTOR_ROLE: TurnScope.DOCTOR}

SCORER_ID = "meddial.experiments.e0"
DEFAULT_RESAMPLES = 2000


class CorpusError(Exception):
    """The corpus on disk is not the shape E0 needs."""


# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DialogueRecord:
    """One already-generated dialogue and the case it came from."""

    case_id: str
    dialogue_id: str
    policy_key: str
    turns: tuple[Turn, ...]
    reference: StructuredClinicalReference


def load_corpus(dialogues_path: Path, references_path: Path) -> list[DialogueRecord]:
    """Read the existing corpus from two JSONL files.

    ``references_path`` is keyed by ``case_id``, so a case's reference is
    stored once rather than once per policy arm. The three arms of a case must
    score against an identical reference, or test 1 measures reference drift
    instead of reference scope.
    """
    references: dict[str, StructuredClinicalReference] = {}
    for line_no, item in _read_jsonl(references_path):
        case_id = str(_require(item, "case_id", references_path, line_no))
        payload = _require(item, "reference", references_path, line_no)
        references[case_id] = StructuredClinicalReference.model_validate(payload)

    records: list[DialogueRecord] = []
    seen: set[str] = set()
    for line_no, item in _read_jsonl(dialogues_path):
        case_id = str(_require(item, "case_id", dialogues_path, line_no))
        dialogue_id = str(_require(item, "dialogue_id", dialogues_path, line_no))
        policy_key = str(_require(item, "policy", dialogues_path, line_no))

        if dialogue_id in seen:
            raise CorpusError(f"{dialogues_path}:{line_no}: duplicate dialogue_id {dialogue_id!r}")
        seen.add(dialogue_id)
        if case_id not in references:
            raise CorpusError(
                f"{dialogues_path}:{line_no}: no reference for case {case_id!r}. "
                "A dialogue with no reference cannot be scored against one."
            )
        if policy_key not in POLICY_ORDER:
            raise CorpusError(
                f"{dialogues_path}:{line_no}: policy {policy_key!r} is not one of {POLICY_ORDER}"
            )

        records.append(
            DialogueRecord(
                case_id=case_id,
                dialogue_id=dialogue_id,
                policy_key=policy_key,
                turns=tuple(build_turns(_require(item, "dialogue", dialogues_path, line_no))),
                reference=references[case_id],
            )
        )

    if not records:
        raise CorpusError(f"{dialogues_path} holds no dialogues")
    return records


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoredDialogue:
    """One dialogue scored under one reference mode, for both roles."""

    dialogue_id: str
    case_id: str
    policy_key: str
    reference_mode: ReferenceMode
    scores: Mapping[str, Score]

    def as_record(self) -> dict[str, Any]:
        return {
            "dialogue_id": self.dialogue_id,
            "case_id": self.case_id,
            "policy": self.policy_key,
            "reference_mode": self.reference_mode.value,
            "scores": {name: score.as_record() for name, score in self.scores.items()},
        }


def score_corpus(
    records: Sequence[DialogueRecord],
    *,
    provider: LLMProvider,
    registry: PolicyRegistry | None = None,
    reference_modes: Sequence[ReferenceMode] = REFERENCE_MODES,
    results_path: Path | None = None,
    seed: int | None = None,
    temperature: float = 0.0,
) -> list[ScoredDialogue]:
    """Re-score every dialogue under every reference mode, for both roles.

    Results are appended to ``results_path`` as they are produced, and any
    already present are skipped, so an interrupted run resumes rather than
    paying for completed work twice. 450 dialogues is hours of local
    inference; it will be interrupted.
    """
    policies = registry or PolicyRegistry()
    done = _completed_keys(results_path) if results_path else set()
    scored: list[ScoredDialogue] = []

    for record in records:
        pending = [
            mode for mode in reference_modes if (record.dialogue_id, mode.value) not in done
        ]
        if not pending:
            continue

        policy = policies.load(record.policy_key)
        claim_set = _extract_once(record, provider=provider, temperature=temperature, seed=seed)
        for mode in pending:
            result = _score_one(
                record,
                policy,
                claim_set,
                mode,
                provider=provider,
                temperature=temperature,
                seed=seed,
            )
            scored.append(result)
            if results_path:
                _append_jsonl(results_path, result.as_record())

    return scored


def _extract_once(
    record: DialogueRecord, *, provider: LLMProvider, temperature: float, seed: int | None
) -> ClaimSet | None:
    """Extract claims once per dialogue. ``None`` when extraction failed."""
    try:
        return extract_claims(record.turns, provider=provider, temperature=temperature, seed=seed)
    except ClaimExtractionError:
        return None


def _score_one(
    record: DialogueRecord,
    policy: KnowledgePolicy,
    claim_set: ClaimSet | None,
    mode: ReferenceMode,
    *,
    provider: LLMProvider,
    temperature: float,
    seed: int | None,
) -> ScoredDialogue:
    if claim_set is None:
        template = load_prompt("claim_verification")
        scores: dict[str, Score] = {
            dimension: Score.incomplete(
                ScoreProvenance.unmeasured(
                    scorer_id=SCORER_ID,
                    reference_mode=mode,
                    turn_scope=TURN_SCOPE_FOR_ROLE[role],
                    prompt_version=template.version,
                    reason="claim_extraction_failed",
                )
            )
            for role, dimension in DIMENSION_FOR_ROLE.items()
        }
    else:
        context = build_contexts(record.reference, policy).evaluator
        scores = {
            dimension: score_faithfulness(
                claim_set,
                context,
                role=role,
                reference_mode=mode,
                provider=provider,
                temperature=temperature,
                seed=seed,
            )
            for role, dimension in DIMENSION_FOR_ROLE.items()
        }

    return ScoredDialogue(
        dialogue_id=record.dialogue_id,
        case_id=record.case_id,
        policy_key=record.policy_key,
        reference_mode=mode,
        scores=scores,
    )


def read_results(path: Path) -> list[ScoredDialogue]:
    """Rebuild scored dialogues from a results file — the inverse of ``as_record``.

    A resumed run only returns the dialogues it scored this time, so the file
    is the sole complete record. Reconstruction goes through :class:`Score`,
    whose validation rejects a truncated or hand-edited line rather than
    letting a malformed score reach the analysis.
    """
    results: list[ScoredDialogue] = []
    for line_no, item in _read_jsonl(path):
        scores: dict[str, Score] = {}
        for dimension, payload in _require(item, "scores", path, line_no).items():
            try:
                scores[dimension] = _score_from_record(payload)
            except (KeyError, TypeError, ValueError) as exc:
                raise CorpusError(f"{path}:{line_no}: bad score {dimension!r}: {exc}") from exc
        results.append(
            ScoredDialogue(
                dialogue_id=str(_require(item, "dialogue_id", path, line_no)),
                case_id=str(_require(item, "case_id", path, line_no)),
                policy_key=str(_require(item, "policy", path, line_no)),
                reference_mode=ReferenceMode(_require(item, "reference_mode", path, line_no)),
                scores=scores,
            )
        )
    return results


def _score_from_record(payload: Mapping[str, Any]) -> Score:
    provenance = payload["provenance"]
    mode = provenance["reference_mode"]
    return Score(
        value=payload["value"],
        status=EvaluationStatus(payload["status"]),
        provenance=ScoreProvenance(
            scorer_id=provenance["scorer_id"],
            model_family=provenance["model_family"],
            model_id=provenance["model_id"],
            model_digest=provenance["model_digest"],
            quantisation=provenance["quantisation"],
            reference_mode=None if mode is None else ReferenceMode(mode),
            turn_scope=TurnScope(provenance["turn_scope"]),
            prompt_version=provenance["prompt_version"],
            sampling=provenance.get("sampling", {}),
            fallback_used=provenance.get("fallback_used", False),
            incomplete_reason=provenance.get("incomplete_reason"),
        ),
        detail=payload.get("detail", {}),
    )


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class E0Report:
    """The decomposition. Not a conclusion — plan §6 step 5 is a human decision."""

    curves: Mapping[str, Mapping[str, Interval]]
    reference_scope: Mapping[str, PairedResult]
    turn_scope: Mapping[str, PairedResult]
    gradients: Mapping[str, PairedResult]
    incomplete: Mapping[str, int]
    n_cases: int
    n_dialogues: int

    def as_record(self) -> dict[str, Any]:
        return {
            "curves": {
                label: {policy: interval.as_record() for policy, interval in curve.items()}
                for label, curve in self.curves.items()
            },
            "test_1_reference_scope": {
                key: result.as_record() for key, result in self.reference_scope.items()
            },
            "test_2_turn_scope": {
                key: result.as_record() for key, result in self.turn_scope.items()
            },
            "gradients": {key: result.as_record() for key, result in self.gradients.items()},
            "incomplete": dict(self.incomplete),
            "n_cases": self.n_cases,
            "n_dialogues": self.n_dialogues,
            "note": (
                "Tests 3 and 4 require regeneration and are not included. "
                "No manuscript framing follows from this report alone."
            ),
        }


def analyse(
    scored: Iterable[ScoredDialogue], *, resamples: int = DEFAULT_RESAMPLES, seed: int = 0
) -> E0Report:
    """Build the E0 decomposition from re-scored dialogues."""
    results = list(scored)
    if not results:
        raise StatsError("no scored dialogues to analyse")

    by_condition = _index_by_condition(results)

    curves: dict[str, dict[str, Interval]] = {}
    for (dimension, mode_value, policy), values in by_condition.items():
        try:
            interval = case_clustered_bootstrap(_cluster(values), resamples=resamples, seed=seed)
        except StatsError:
            continue  # every dialogue in this cell was INCOMPLETE
        curves.setdefault(f"{dimension}::{mode_value}", {})[policy] = interval

    reference_scope: dict[str, PairedResult] = {}
    for policy in POLICY_ORDER:
        for dimension in (PATIENT_FACTUALITY, DOCTOR_FACTUALITY):
            policy_ctx = _cell(by_condition, dimension, ReferenceMode.POLICY_CONTEXT, policy)
            full_ref = _cell(by_condition, dimension, ReferenceMode.FULL_REFERENCE, policy)
            result = _compare(policy_ctx, full_ref, "policy_context", "full_reference", resamples, seed)
            if result is not None:
                reference_scope[f"{dimension}::{policy}"] = result

    turn_scope: dict[str, PairedResult] = {}
    for policy in POLICY_ORDER:
        for mode in REFERENCE_MODES:
            patient = _cell(by_condition, PATIENT_FACTUALITY, mode, policy)
            doctor = _cell(by_condition, DOCTOR_FACTUALITY, mode, policy)
            result = _compare(
                patient, doctor, PATIENT_FACTUALITY, DOCTOR_FACTUALITY, resamples, seed
            )
            if result is not None:
                turn_scope[f"{policy}::{mode.value}"] = result

    gradients: dict[str, PairedResult] = {}
    for dimension in (PATIENT_FACTUALITY, DOCTOR_FACTUALITY):
        for mode in REFERENCE_MODES:
            strictest = _cell(by_condition, dimension, mode, POLICY_ORDER[-1])
            loosest = _cell(by_condition, dimension, mode, POLICY_ORDER[0])
            result = _compare(
                strictest, loosest, POLICY_ORDER[-1], POLICY_ORDER[0], resamples, seed
            )
            if result is not None:
                gradients[f"{dimension}::{mode.value}"] = result

    return E0Report(
        curves=curves,
        reference_scope=reference_scope,
        turn_scope=turn_scope,
        gradients=gradients,
        incomplete=_incomplete_counts(results),
        n_cases=len({result.case_id for result in results}),
        n_dialogues=len({result.dialogue_id for result in results}),
    )


def render_report(report: E0Report) -> str:
    """A markdown summary. Every figure carries role, reference mode and interval."""
    lines = [
        "# E0 — measurement confounds in the disclosure→faithfulness trend",
        "",
        (
            f"{report.n_dialogues} dialogues over {report.n_cases} cases. "
            "Intervals are 95% case-clustered bootstrap percentile intervals; "
            "the resampling unit is the case, not the dialogue."
        ),
        "",
        "## Curves",
        "",
        "| Dimension · reference mode | " + " | ".join(POLICY_ORDER) + " |",
        "|---|" + "---|" * len(POLICY_ORDER),
    ]
    for label, curve in sorted(report.curves.items()):
        cells = []
        for policy in POLICY_ORDER:
            interval = curve.get(policy)
            cells.append(
                "—"
                if interval is None
                else f"{interval.estimate:.3f} [{interval.low:.3f}, {interval.high:.3f}]"
            )
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    lines += ["", "## Test 1 — reference scope (policy_context − full_reference)", ""]
    lines += _paired_table(report.reference_scope)
    lines += ["", "## Test 2 — turn scope (patient − doctor)", ""]
    lines += _paired_table(report.turn_scope)
    lines += ["", "## Trend gradient (most restrictive − least restrictive)", ""]
    lines += _paired_table(report.gradients)

    lines += ["", "## INCOMPLETE counts", ""]
    if report.incomplete:
        lines += ["| Condition | Count |", "|---|---|"]
        lines += [f"| {key} | {count} |" for key, count in sorted(report.incomplete.items())]
    else:
        lines.append("None.")

    lines += [
        "",
        (
            "Tests 3 and 4 need regeneration and are not covered here. "
            "No manuscript framing follows from this report alone (plan §6 step 5)."
        ),
        "",
    ]
    return "\n".join(lines)


def _compare(
    arm_a: Mapping[str, float | None],
    arm_b: Mapping[str, float | None],
    label_a: str,
    label_b: str,
    resamples: int,
    seed: int,
) -> PairedResult | None:
    if not arm_a or not arm_b:
        return None
    try:
        return paired_difference(
            arm_a, arm_b, label_a=label_a, label_b=label_b, resamples=resamples, seed=seed
        )
    except StatsError:
        return None  # no case measured in both arms


def _paired_table(results: Mapping[str, PairedResult]) -> list[str]:
    if not results:
        return ["No comparison had cases measured in both arms."]
    lines = [
        "| Comparison | Difference | n cases | dropped | excludes 0 |",
        "|---|---|---|---|---|",
    ]
    for key, result in sorted(results.items()):
        difference = result.difference
        lines.append(
            f"| {key} | {difference.estimate:+.3f} "
            f"[{difference.low:+.3f}, {difference.high:+.3f}] | "
            f"{result.n_cases} | {result.n_dropped} | "
            f"{'yes' if result.excludes_zero else 'no'} |"
        )
    return lines


def _index_by_condition(
    results: Sequence[ScoredDialogue],
) -> dict[tuple[str, str, str], dict[str, float | None]]:
    indexed: dict[tuple[str, str, str], dict[str, float | None]] = {}
    for result in results:
        for dimension, score in result.scores.items():
            key = (dimension, result.reference_mode.value, result.policy_key)
            indexed.setdefault(key, {})[result.case_id] = score.value
    return indexed


def _cell(
    by_condition: Mapping[tuple[str, str, str], Mapping[str, float | None]],
    dimension: str,
    mode: ReferenceMode,
    policy: str,
) -> dict[str, float | None]:
    return dict(by_condition.get((dimension, mode.value, policy), {}))


def _cluster(values: Mapping[str, float | None]) -> dict[str, list[float | None]]:
    return {case: [value] for case, value in values.items()}


def _incomplete_counts(results: Sequence[ScoredDialogue]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        for dimension, score in result.scores.items():
            if score.value is None:
                key = f"{dimension}::{result.reference_mode.value}::{result.policy_key}"
                counts[key] = counts.get(key, 0) + 1
    return counts


# --------------------------------------------------------------------------
# JSONL helpers
# --------------------------------------------------------------------------


def _read_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    if not path.exists():
        raise CorpusError(f"{path} does not exist")
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise CorpusError(f"{path}:{line_no}: not valid JSON: {exc}") from exc
            if not isinstance(item, dict):
                raise CorpusError(f"{path}:{line_no}: expected an object")
            yield line_no, item


def _require(item: Mapping[str, Any], key: str, path: Path, line_no: int) -> Any:
    if key not in item:
        raise CorpusError(f"{path}:{line_no}: missing key {key!r}")
    return item[key]


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _completed_keys(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    return {
        (str(item["dialogue_id"]), str(item["reference_mode"]))
        for _, item in _read_jsonl(path)
        if "dialogue_id" in item and "reference_mode" in item
    }
