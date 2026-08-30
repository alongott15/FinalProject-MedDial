import json
import logging
import re
from typing import Dict, List, Optional, Tuple

from deepeval.metrics import GEval
from deepeval.models import DeepEvalBaseLLM
from deepeval.test_case import LLMTestCase, LLMTestCaseParams

from Utils.bias_aware_prompts import PATIENT_PROFILE_TYPE_KNOWLEDGE
from meddial.llm import (
    DataClassification,
    LLMProvider,
    ProviderError,
    to_chat_messages,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class JudgeEvaluationError(Exception):
    """A dimension could not be measured.

    Raised instead of returning a substitute number (EVAL-4, defect D-06).
    The scorer that used to catch these exceptions and quietly switch to an
    ad-hoc direct-LLM scorer has been deleted: a value in a results table must
    come from the scorer its provenance names, and a dimension that cannot be
    measured is reported as unmeasured.
    """


# ---------------------------------------------------------------------------
# Azure AI Foundry → deepeval adapter
# ---------------------------------------------------------------------------

class ProviderDeepEvalLLM(DeepEvalBaseLLM):
    """
    Thin wrapper that exposes an ``LLMProvider`` as a deepeval-compatible
    ``DeepEvalBaseLLM``, so GEval metrics run through the same governed
    provider as the rest of the pipeline rather than a second client.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        temperature: float = 0.1,
        max_tokens: int = 1000,
        seed: int | None = None,
    ):
        self.provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._seed = seed
        super().__init__()

    def load_model(self) -> LLMProvider:
        return self.provider

    def generate(self, prompt: str) -> Tuple[str, Optional[dict]]:
        result = self.provider.complete(
            to_chat_messages([{"role": "user", "content": prompt}]),
            # Judge prompts embed the dialogue, which is MIMIC-derived.
            classification=DataClassification.RESTRICTED_CLINICAL,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            seed=self._seed,
        )
        return result.text, None

    async def a_generate(self, prompt: str) -> Tuple[str, Optional[dict]]:
        # Async interface required by deepeval — delegates to sync implementation
        return self.generate(prompt)

    def get_model_name(self) -> str:
        return f"{self.provider.model_family}-judge"


# ---------------------------------------------------------------------------
# Main judge agent
# ---------------------------------------------------------------------------

class DeepEvalJudgeAgent:
    """
    Validates synthetic doctor–patient dialogues using deepeval's GEval
    framework together with a RAGAS-style faithfulness metric.

    Each evaluation returns a dict with the same contract as the legacy
    ``JudgeAgent`` so it can be dropped-in as a replacement inside
    ``DialogueGenerationPipeline``.

    Parameters
    ----------
    llm : AzureAIFoundryClient, optional
        Pre-initialised Azure client. Loaded internally if not supplied.
    threshold : float
        Minimum combined score to decide ``"REALISTIC"`` (default 0.70).
    weights : dict, optional
        Score weights for the three metrics. Keys: ``naturalness``,
        ``profile_compliance``, ``ragas_faithfulness``. Must sum to 1.0.
        Defaults to ``{naturalness: 0.40, profile_compliance: 0.30,
        ragas_faithfulness: 0.30}``.
    """

    DEFAULT_WEIGHTS: Dict[str, float] = {
        "naturalness": 0.40,
        "profile_compliance": 0.30,
        "ragas_faithfulness": 0.30,
    }

    def __init__(
        self,
        provider: LLMProvider,
        threshold: float = 0.70,
        weights: Optional[Dict[str, float]] = None,
        *,
        temperature: float = 0.1,
        max_tokens: int = 1000,
        seed: int | None = None,
    ):
        # Injected, never constructed here (GOV-4). The caller is responsible
        # for passing a judge from a different family than the generator.
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._seed = seed

        self.threshold = threshold
        self.weights = weights or self.DEFAULT_WEIGHTS
        self.deepeval_llm = ProviderDeepEvalLLM(
            provider,
            temperature=temperature,
            max_tokens=max_tokens,
            seed=seed,
        )

    def _complete(self, messages: List[Dict[str, str]]) -> str:
        """Run one judge call. Raises ProviderError; never returns error text."""
        return self._provider.complete(
            to_chat_messages(messages),
            # Judge prompts embed the dialogue, which is MIMIC-derived.
            classification=DataClassification.RESTRICTED_CLINICAL,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            seed=self._seed,
        ).text

        logger.info(
            f"DeepEvalJudgeAgent ready | threshold={threshold} | weights={self.weights}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate_dialogue(
        self,
        dialogue: list,
        patient_profile: dict,
        dialogue_transcript: str = None,
    ) -> Dict:
        """
        Evaluate a dialogue for naturalness, patient profile compliance,
        and RAGAS faithfulness.

        Parameters
        ----------
        dialogue : list of dict
            Conversation turns, each with ``role`` and ``content`` keys.
        patient_profile : dict
            The GTMF profile that was given to the patient agent
            (may be a partial profile with ``profile_type`` set).
        dialogue_transcript : str, optional
            Pre-formatted transcript string. Generated automatically if absent.

        Returns
        -------
        dict
            ``decision``                 — "REALISTIC" or "UNREALISTIC"
            ``score``                    — combined float 0.0–1.0
            ``justification``            — human-readable explanation
            ``feedback_for_improvement`` — per-aspect feedback strings
            ``deepeval_scores``          — breakdown by metric
        """
        logger.info("DeepEvalJudgeAgent: starting evaluation …")

        if dialogue_transcript is None:
            dialogue_transcript = self._format_dialogue(dialogue)

        profile_summary = self._format_profile(patient_profile)
        profile_type = patient_profile.get("profile_type", "NO_DIAGNOSIS_NO_TREATMENT")
        patient_turns = self._extract_patient_turns(dialogue)

        # --- Metric 1: Naturalness ---
        naturalness = self._compute_naturalness(dialogue_transcript, profile_summary)

        # --- Metric 2: Patient profile compliance ---
        compliance = self._compute_profile_compliance(
            dialogue_transcript, patient_turns, patient_profile, profile_type
        )

        # --- Metric 3: RAGAS faithfulness ---
        faithfulness = self._compute_ragas_faithfulness(
            patient_turns, patient_profile, profile_type
        )

        # --- Weighted combination ---
        w = self.weights
        combined = (
            w["naturalness"] * naturalness
            + w["profile_compliance"] * compliance
            + w["ragas_faithfulness"] * faithfulness
        )
        combined = max(0.0, min(1.0, combined))

        decision = "REALISTIC" if combined >= self.threshold else "UNREALISTIC"

        justification = (
            f"Combined score {combined:.2f} "
            f"(naturalness={naturalness:.2f} ×{w['naturalness']}, "
            f"profile_compliance={compliance:.2f} ×{w['profile_compliance']}, "
            f"ragas_faithfulness={faithfulness:.2f} ×{w['ragas_faithfulness']}). "
            f"Profile type: {profile_type}."
        )

        feedback = self._build_feedback(
            naturalness, compliance, faithfulness, profile_type
        )

        result = {
            "decision": decision,
            "score": combined,
            "justification": justification,
            "feedback_for_improvement": feedback,
            "deepeval_scores": {
                "naturalness": naturalness,
                "profile_compliance": compliance,
                "ragas_faithfulness": faithfulness,
                "profile_type": profile_type,
                "weights": w,
            },
        }

        logger.info(
            f"Evaluation done: {decision} | score={combined:.3f} | "
            f"nat={naturalness:.2f} comp={compliance:.2f} faith={faithfulness:.2f}"
        )
        return result

    # ------------------------------------------------------------------
    # Metric 1 — Naturalness (GEval)
    # ------------------------------------------------------------------

    def _compute_naturalness(
        self, dialogue_transcript: str, profile_summary: str
    ) -> float:
        """GEval naturalness: does the dialogue read like a real consultation?"""
        try:
            metric = GEval(
                name="Medical Dialogue Naturalness",
                criteria=(
                    "Evaluate whether this doctor–patient dialogue sounds natural and "
                    "realistic for a primary-care outpatient consultation.\n\n"
                    "A high-scoring (natural) dialogue:\n"
                    "1. Has appropriate conversational flow; turns build on each other.\n"
                    "2. Uses realistic medical questioning — progressive, open-then-closed.\n"
                    "3. Patient responses match the symptom profile provided.\n"
                    "4. Language is human (varied sentence starters, natural hesitations).\n"
                    "5. Clinical reasoning is plausible for a mild/common condition.\n"
                    "6. Doctor arrives at a conclusion consistent with gathered information.\n\n"
                    "A low-scoring (unrealistic) dialogue:\n"
                    "- Robotic or formulaic phrasing repeated across turns.\n"
                    "- Patient or doctor hallucinating details absent from the profile.\n"
                    "- Clinical conclusions that contradict the symptom picture.\n"
                    "- ICU-level events or severity mismatch for a mild case."
                ),
                evaluation_params=[
                    LLMTestCaseParams.INPUT,
                    LLMTestCaseParams.ACTUAL_OUTPUT,
                ],
                model=self.deepeval_llm,
                threshold=0.5,
                strict_mode=False,
                verbose_mode=False,
            )

            test_case = LLMTestCase(
                input=f"Patient profile context:\n{profile_summary}",
                actual_output=dialogue_transcript,
            )

            metric.measure(test_case)
            score = float(metric.score or 0.0)
            logger.info(f"[Naturalness] GEval score: {score:.3f}")
            return score

        except Exception as exc:
            raise JudgeEvaluationError(f"naturalness GEval failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Metric 2 — Patient Profile Compliance (GEval)
    # ------------------------------------------------------------------

    def _compute_profile_compliance(
        self,
        dialogue_transcript: str,
        patient_turns: List[str],
        patient_profile: dict,
        profile_type: str,
    ) -> float:
        """
        GEval compliance: does the patient reveal only what their profile
        type permits?

        The evaluation focuses on patient utterances (not the doctor's turns).
        Profile-type rules come from ``PATIENT_PROFILE_TYPE_KNOWLEDGE``.
        """
        knowledge = PATIENT_PROFILE_TYPE_KNOWLEDGE.get(
            profile_type,
            PATIENT_PROFILE_TYPE_KNOWLEDGE["NO_DIAGNOSIS_NO_TREATMENT"],
        )

        criteria = (
            f"Evaluate whether the PATIENT in this medical dialogue strictly respects "
            f"the knowledge boundaries defined by their profile type.\n\n"
            f"Profile Type: {profile_type}\n"
            f"Profile Description: {knowledge['description']}\n"
            f"Disclosure Rules: {knowledge['disclosure_rules']}\n\n"
            "Scoring guide:\n"
            "- 1.0 (perfect): patient never mentions diagnosis/treatment outside their "
            "profile type; only discusses what they are allowed to know.\n"
            "- 0.5 (partial): patient occasionally hints at forbidden information but "
            "does not explicitly state it.\n"
            "- 0.0 (fail): patient explicitly names a diagnosis or treatment they should "
            "not know, or consistently contradicts their profile type.\n\n"
            "Focus ONLY on the patient's utterances. Ignore the doctor's turns."
        )

        patient_text = "\n".join(f"Patient: {t}" for t in patient_turns)

        try:
            metric = GEval(
                name="Patient Profile Compliance",
                criteria=criteria,
                evaluation_params=[
                    LLMTestCaseParams.INPUT,
                    LLMTestCaseParams.ACTUAL_OUTPUT,
                ],
                model=self.deepeval_llm,
                threshold=0.5,
                strict_mode=False,
                verbose_mode=False,
            )

            test_case = LLMTestCase(
                input=(
                    f"Profile Type: {profile_type}\n"
                    f"What the patient is allowed to know: {knowledge['description']}\n"
                    f"Full dialogue for context:\n{dialogue_transcript}"
                ),
                actual_output=f"Patient utterances only:\n{patient_text}",
            )

            metric.measure(test_case)
            score = float(metric.score or 0.0)
            logger.info(f"[ProfileCompliance] GEval score: {score:.3f}")
            return score

        except Exception as exc:
            raise JudgeEvaluationError(f"profile compliance GEval failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Metric 3 — RAGAS Faithfulness (manual, Azure AI Foundry)
    # ------------------------------------------------------------------

    def _compute_ragas_faithfulness(
        self,
        patient_turns: List[str],
        patient_profile: dict,
        profile_type: str,
    ) -> float:
        """
        RAGAS faithfulness algorithm applied to patient turns.

        Steps
        -----
        1. Extract atomic factual statements from all patient utterances.
        2. Build a profile context that reflects what the patient is
           allowed to know (based on profile_type).
        3. For each statement ask the LLM: "Can this be inferred from the
           patient's profile context?"
        4. Score = faithful_count / total_count.

        A higher score means patient claims are consistently grounded in
        the actual profile; hallucinated details bring the score down.
        """
        if not patient_turns:
            logger.warning("[RAGASFaithfulness] No patient turns; returning 0.0")
            return 0.0

        statements = self._extract_atomic_statements(patient_turns)
        if not statements:
            logger.info("[RAGASFaithfulness] No factual statements extracted; returning 1.0")
            return 1.0

        profile_context = self._build_faithfulness_context(patient_profile, profile_type)

        faithful_count = sum(
            1 for stmt in statements
            if self._is_statement_faithful(stmt, profile_context)
        )

        score = faithful_count / len(statements)
        logger.info(
            f"[RAGASFaithfulness] {faithful_count}/{len(statements)} statements faithful "
            f"→ score={score:.3f}"
        )
        return score

    def _extract_atomic_statements(self, patient_turns: List[str]) -> List[str]:
        """
        Use the LLM to decompose patient utterances into atomic factual
        claims (symptoms, history, medications, diagnoses, treatments).
        Conversational filler and questions are excluded.
        """
        combined = "\n".join(patient_turns)

        messages = [
            {
                "role": "system",
                "content": (
                    "You extract atomic, verifiable factual statements from patient medical "
                    "dialogue. Each statement should express a single claim about the patient's "
                    "medical situation: symptoms, history, medications, allergies, diagnoses, or "
                    "treatments. Exclude questions, emotional filler, and conversational phrases. "
                    "Output ONLY a valid JSON array of strings — no other text."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Patient utterances:\n{combined}\n\n"
                    "Extract all medical factual claims. Example output:\n"
                    '["I have had a headache for three days.", '
                    '"The pain is on my left side.", '
                    '"I take ibuprofen daily.", '
                    '"I was told I have high blood pressure."]'
                ),
            },
        ]

        try:
            response = self._complete(messages)
            json_match = re.search(r"\[.*?\]", response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                statements = [str(s).strip() for s in data if isinstance(s, str) and str(s).strip()]
                logger.debug(f"[RAGASFaithfulness] Extracted {len(statements)} statements.")
                return statements
        except Exception as exc:
            logger.warning(f"[RAGASFaithfulness] Statement extraction failed: {exc}")

        return []

    def _build_faithfulness_context(
        self, patient_profile: dict, profile_type: str
    ) -> str:
        """
        Construct a profile context string that represents exactly what the
        patient is *allowed to know* according to their profile type.

        This context is used as ground truth when checking each patient
        statement for faithfulness.
        """
        knowledge = PATIENT_PROFILE_TYPE_KNOWLEDGE.get(
            profile_type,
            PATIENT_PROFILE_TYPE_KNOWLEDGE["NO_DIAGNOSIS_NO_TREATMENT"],
        )

        core = patient_profile.get("Core_Fields", {})
        ctx = patient_profile.get("Context_Fields", {})
        extra = patient_profile.get("Additional_Context", {})

        # --- Always available ---
        symptoms = core.get("Symptoms", [])
        symptom_strs = [
            s.get("description", "") if isinstance(s, dict) else str(s)
            for s in symptoms
        ]

        demo = ctx.get("Patient_Demographics", {})
        age = demo.get("Age", "unknown") if isinstance(demo, dict) else "unknown"
        sex = demo.get("Sex", "unknown") if isinstance(demo, dict) else "unknown"

        history = ctx.get("Medical_History", {})
        past_history = (
            history.get("Past_Medical_History", "not provided")
            if isinstance(history, dict)
            else str(history)
        )

        allergies = ctx.get("Allergies", [])
        allergy_str = ", ".join(allergies) if allergies else "none reported"

        current_meds = ctx.get("Current_Medications", [])
        med_names = []
        for m in current_meds:
            if isinstance(m, dict):
                n = m.get("name", "")
                if n:
                    med_names.append(n)
            elif isinstance(m, str) and m.strip():
                med_names.append(m.strip())

        chief_complaint = extra.get("Chief_Complaint", "not provided")

        context_lines = [
            f"Patient profile context (type: {profile_type})",
            f"Chief complaint: {chief_complaint}",
            f"Symptoms the patient knows: {'; '.join(s for s in symptom_strs if s) or 'none listed'}",
            f"Medical history: {past_history}",
            f"Allergies: {allergy_str}",
            f"Current medications (background): {', '.join(med_names) or 'none listed'}",
            f"Demographics: age {age}, sex {sex}",
        ]

        # --- Diagnosis: only for FULL profile ---
        if knowledge["knows_diagnosis"]:
            diagnoses = core.get("Diagnoses", [])
            diag_strs = []
            for d in diagnoses:
                if isinstance(d, dict):
                    p = d.get("primary", "")
                    if p:
                        diag_strs.append(p)
                elif isinstance(d, str) and d.strip():
                    diag_strs.append(d.strip())
            context_lines.append(
                f"Diagnosis the patient knows: "
                f"{'; '.join(diag_strs) or 'none specified'}"
            )
        else:
            context_lines.append(
                "Diagnosis: patient does NOT know their formal diagnosis — "
                "any claim to know the diagnosis name is unfaithful."
            )

        # --- Treatment: only for FULL and NO_DIAGNOSIS profiles ---
        if knowledge["knows_treatment"]:
            tx_options = core.get("Treatment_Options", [])
            tx_strs = []
            for t in tx_options:
                if isinstance(t, dict):
                    proc = t.get("procedure", t.get("treatment", ""))
                    if proc and proc != "not provided":
                        tx_strs.append(proc)
                elif isinstance(t, str) and t.strip():
                    tx_strs.append(t.strip())
            context_lines.append(
                f"Treatment/plan the patient knows: "
                f"{'; '.join(tx_strs) or 'none specified'}"
            )
        else:
            context_lines.append(
                "Treatment plan: patient does NOT know any formal treatment plan — "
                "any specific treatment claim is unfaithful."
            )

        return "\n".join(context_lines)

    def _is_statement_faithful(self, statement: str, profile_context: str) -> bool:
        """
        Ask the LLM whether a single patient statement can be inferred from
        the patient's allowed profile context.

        Returns ``True`` if the statement is grounded (faithful), ``False``
        otherwise. Failures raise. There is no conservative default: scoring
        an unverified statement as faithful inflates the reported metric,
        which is the failure mode D-08 and D-06 exist to prevent.
        """
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a medical fact-checker. Given a patient profile context and a "
                    "single statement made by the patient, decide whether the statement is "
                    "faithful — i.e., it can be inferred from or is consistent with the profile. "
                    "Answer ONLY with the word 'Yes' or the word 'No'."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Patient Profile Context:\n{profile_context}\n\n"
                    f"Patient Statement: \"{statement}\"\n\n"
                    "Is this statement faithful to (supported by) the patient profile context? "
                    "Answer only: Yes or No"
                ),
            },
        ]

        try:
            response = self._complete(messages).strip().lower()
        except ProviderError:
            # Do not swallow: defaulting to True would score an unverified
            # statement as faithful and inflate the reported metric (D-08).
            raise
        except Exception as exc:
            raise JudgeEvaluationError(f"faithfulness check failed: {exc}") from exc
        return response.startswith("yes")

    # ------------------------------------------------------------------
    # Feedback builder
    # ------------------------------------------------------------------

    def _build_feedback(
        self,
        naturalness: float,
        compliance: float,
        faithfulness: float,
        profile_type: str,
    ) -> dict:
        """Build actionable per-aspect feedback for the improvement agent."""
        knowledge = PATIENT_PROFILE_TYPE_KNOWLEDGE.get(
            profile_type,
            PATIENT_PROFILE_TYPE_KNOWLEDGE["NO_DIAGNOSIS_NO_TREATMENT"],
        )

        if naturalness < 0.60:
            flow_fb = (
                "Dialogue lacks natural flow. Responses are too formulaic or robotic. "
                "Patient should vary sentence starters and use everyday language; "
                "doctor should ask more progressive, open-ended questions."
            )
        else:
            flow_fb = "Dialogue flow is natural and realistic."

        if compliance < 0.70:
            patient_fb = (
                f"PROFILE COMPLIANCE ISSUE [{profile_type}]: "
                f"Patient may be disclosing information outside their knowledge boundary. "
                f"Reminder — {knowledge['disclosure_rules']}"
            )
        else:
            patient_fb = (
                f"Patient correctly respects {profile_type} profile knowledge boundaries."
            )

        if faithfulness < 0.70:
            grounding_fb = (
                "Patient statements contain claims not supported by their profile. "
                "Patient may be hallucinating symptoms, history, or other details. "
                "All patient claims must be grounded in the provided profile."
            )
        else:
            grounding_fb = "Patient statements are faithful to the profile."

        return {
            "patient_side": patient_fb,
            "doctor_side": (
                "Doctor's clinical questions should be progressive and build on patient "
                "responses. Avoid repeating the same questions."
            ),
            "conversation_flow": flow_fb,
            "groundedness": grounding_fb,
        }

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    def _format_dialogue(self, dialogue: list) -> str:
        return "\n".join(
            f"{turn.get('role', 'Unknown')}: {turn.get('content', '')}"
            for turn in dialogue
        )

    def _format_profile(self, profile: dict) -> str:
        core = profile.get("Core_Fields", {})
        ctx = profile.get("Context_Fields", {})
        extra = profile.get("Additional_Context", {})
        profile_type = profile.get("profile_type", "UNKNOWN")

        symptoms = core.get("Symptoms", [])
        symptom_list = [
            s.get("description", "") for s in symptoms if isinstance(s, dict)
        ]
        diagnoses = core.get("Diagnoses", [])
        diagnosis_list = [
            d.get("primary", "") for d in diagnoses if isinstance(d, dict)
        ]
        treatments = core.get("Treatment_Options", [])
        treatment_list = [
            t.get("procedure", t.get("treatment", ""))
            for t in treatments if isinstance(t, dict)
        ]

        demo = ctx.get("Patient_Demographics", {})
        age = demo.get("Age", "Unknown") if isinstance(demo, dict) else "Unknown"
        sex = demo.get("Sex", "Unknown") if isinstance(demo, dict) else "Unknown"
        chief = extra.get("Chief_Complaint", "Not specified")

        return (
            f"Patient Profile (type: {profile_type})\n"
            f"- Age: {age}, Sex: {sex}\n"
            f"- Chief Complaint: {chief}\n"
            f"- Symptoms: {', '.join(s for s in symptom_list if s) or 'None specified'}\n"
            f"- Diagnoses: {', '.join(d for d in diagnosis_list if d) or 'None (not in profile)'}\n"
            f"- Treatment Options: {', '.join(t for t in treatment_list if t) or 'None (not in profile)'}"
        )

    def _extract_patient_turns(self, dialogue: list) -> List[str]:
        return [
            turn.get("content", "")
            for turn in dialogue
            if turn.get("role", "").lower() == "patient"
        ]

    # ------------------------------------------------------------------
    # Compatibility helpers (mirrors JudgeAgent public API)
    # ------------------------------------------------------------------

    def set_few_shot_examples(self, examples: list):
        """No-op kept for API compatibility with JudgeAgent."""
        logger.info(
            "DeepEvalJudgeAgent does not use few-shot examples (GEval handles grounding)."
        )

    def set_threshold(self, threshold: float):
        self.threshold = threshold
        logger.info(f"DeepEvalJudgeAgent threshold updated to {threshold}")
