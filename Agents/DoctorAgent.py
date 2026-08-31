import logging
from collections.abc import Mapping

from meddial.knowledge import DoctorContext
from meddial.llm import DataClassification, LLMProvider, to_chat_messages
from Utils.bias_aware_prompts import DOCTOR_GUIDANCE, DOCTOR_SYSTEM_PROMPT
from Utils.conversation_variety import (
    create_varied_prompt_examples,
    should_doctor_explain_reasoning,
    should_doctor_summarize,
)
from Utils.repetition_filter import RepetitionTracker, detect_symptom_repetition

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DoctorAgent:
    def __init__(
        self,
        provider: LLMProvider,
        patient_profile: dict = None,
        *,
        doctor_context: DoctorContext | None = None,
        guidance_id: str | None = None,
        temperature: float = 0.5,  # Higher than the summarizer for natural variation
        max_tokens: int = 300,
        seed: int | None = None,
    ):
        # Injected, never constructed here, so the run manifest records one
        # model configuration for the whole run (GOV-4).
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._seed = seed
        # ``patient_profile`` remains as a compatibility input for callers
        # outside the governed pipeline.  Production passes a DoctorContext,
        # whose visible mapping cannot reach patient-only fields.
        visible_profile: Mapping = (
            doctor_context.visible
            if doctor_context is not None
            else (patient_profile or {})
        )
        self.patient_profile = dict(visible_profile)
        self.coach_feedback_to_incorporate = None
        self.conversation_phase = "opening"
        self.conversation_turn = 0
        self.last_patient_emotion = "neutral"

        # Add repetition tracking
        self.repetition_tracker = RepetitionTracker("DoctorAgent")

        # Extract demographics and profile information
        demographics_info = "Not specified"
        available_data_summary = []

        if visible_profile:
            context_fields = visible_profile.get(
                "Context_Fields", visible_profile.get("context", {})
            )
            core_fields = visible_profile.get("Core_Fields", visible_profile.get("core", {}))

            # Demographics
            demo = context_fields.get(
                "Patient_Demographics", context_fields.get("demographics", {})
            )
            if demo:
                demographics_info = (
                    f"Age: {demo.get('Age', demo.get('age', 'Not provided'))}, "
                    f"Sex: {demo.get('Sex', demo.get('sex', 'Not provided'))}"
                )

            # Check what data is available
            if core_fields.get("Symptoms", core_fields.get("symptoms")):
                available_data_summary.append("symptoms reported in profile")
            if context_fields.get("Medical_History", context_fields.get("medical_history")):
                available_data_summary.append("medical history")
            if context_fields.get("Allergies", context_fields.get("allergies")):
                available_data_summary.append("allergy information")

        data_available = ", ".join(available_data_summary) if available_data_summary else "limited patient data"

        # What the doctor is briefed to expect is its own experimental factor
        # (D-05). It defaults to the patient's policy so a normal run is
        # unchanged, but the caller can cross the two, and only the caller may:
        # reading the briefing off the patient's policy here is what made
        # disclosure and briefing a single treatment in the thesis pipeline.
        profile_type = (
            patient_profile.get("profile_type", "NO_DIAGNOSIS_NO_TREATMENT")
            if patient_profile
            else "NO_DIAGNOSIS_NO_TREATMENT"
        )
        self.profile_type = profile_type
        self.guidance_id = (
            guidance_id
            or (doctor_context.guidance_id if doctor_context is not None else None)
            or profile_type
        )
        guidance = DOCTOR_GUIDANCE.get(
            self.guidance_id, DOCTOR_GUIDANCE["NO_DIAGNOSIS_NO_TREATMENT"]
        )

        self.system_message = {
            "role": "system",
            "content": DOCTOR_SYSTEM_PROMPT.format(
                demographics=demographics_info,
                data_available=data_available,
                guidance=guidance,
            ),
        }

    def _detect_patient_emotion(self, patient_message: str) -> str:
        message_lower = patient_message.lower()

        if any(word in message_lower for word in ['worried', 'scared', 'afraid', 'concerned', 'anxious']):
            return "anxious"
        elif any(word in message_lower for word in ['frustrated', 'annoyed', 'tired of']):
            return "frustrated"
        elif any(word in message_lower for word in ['hurts', 'painful', 'terrible', 'awful']):
            return "in_pain"
        elif any(word in message_lower for word in ['confused', 'don\'t understand', 'unclear']):
            return "confused"
        else:
            return "neutral"

    def _update_conversation_phase(self, turn_count: int, conversation_history: list):
        if turn_count <= 3:
            self.conversation_phase = "opening"
        elif turn_count <= 8:
            self.conversation_phase = "exploration"
        elif turn_count <= 11:
            self.conversation_phase = "synthesis"
        else:
            self.conversation_phase = "conclusion"

    def respond(self, conversation_history: list) -> str:
        self.conversation_turn += 1

        llm_messages = [self.system_message]

        for message in conversation_history:
            if message['role'].lower() == 'doctor':
                llm_messages.append({'role': 'assistant', 'content': message['content']})
            elif message['role'].lower() == 'patient':
                llm_messages.append({'role': 'user', 'content': message['content']})

        if self.coach_feedback_to_incorporate:
            llm_messages.append({'role': 'user', 'content': f"Feedback for improvement: {self.coach_feedback_to_incorporate}"})
            self.coach_feedback_to_incorporate = None

        # Update conversation tracking
        self._update_conversation_phase(self.conversation_turn, conversation_history)
        patient_turn_count = sum(
            1
            for message in conversation_history
            if message.get("role", "").lower() == "patient"
            and message.get("content", "").strip()
        )

        # Detect patient emotion for empathetic responses
        if conversation_history and conversation_history[-1].get('role', '').lower() == 'patient':
            last_patient_message = conversation_history[-1]['content']
            self.last_patient_emotion = self._detect_patient_emotion(last_patient_message)

        # Phase-specific guidance for natural conversation flow with smooth transitions
        if self.conversation_phase == "opening":
            phase_guidance = "Greet patient warmly and ask open-ended question about their chief concern."

        elif self.conversation_phase == "exploration":
            phase_guidance = "Explore symptoms in depth with FOCUSED follow-up questions (severity, duration, triggers). Prioritize the most relevant questions."
            # Suggest clinical depth
            if should_doctor_summarize(self.conversation_turn, patient_turn_count):
                phase_guidance += " Consider briefly summarizing what you've learned so far."
            # Encourage natural transition to conclusion if sufficient coverage
            if self.conversation_turn >= 6 and patient_turn_count >= 2:
                phase_guidance += " You have gathered good information. After your next question or two, start transitioning toward a conclusion by saying something like 'Based on what you've shared...' or 'Let me explain what I'm thinking...'"

        elif self.conversation_phase == "synthesis":
            phase_guidance = "Begin your clinical assessment NATURALLY. Use transitional phrases like: 'Based on what we've discussed...', 'From what you've told me...', 'Let me share my thoughts...' Then explain your clinical reasoning in simple terms before giving recommendations."
            if should_doctor_explain_reasoning(self.conversation_turn, self.conversation_phase):
                phase_guidance += " Walk the patient through your thinking step-by-step."

        else:  # conclusion
            # Check if we already provided a conclusion
            already_concluded = False
            if conversation_history:
                for msg in conversation_history:
                    if msg.get('role', '').lower() == 'doctor':
                        content_lower = msg['content'].lower()
                        if any(keyword in content_lower for keyword in ['based on', 'sounds like', 'recommend', 'my assessment']):
                            already_concluded = True
                            break

            if already_concluded:
                phase_guidance = "You already provided your conclusion. Keep this response VERY brief - just answer patient's question or provide a final reassuring statement. DO NOT repeat your assessment or recommendations. Just say something like 'You're welcome' or 'Feel free to reach out if symptoms change' and STOP."
            else:
                phase_guidance = "Provide clear assessment, practical self-care advice, and warning signs to watch for. End by asking 'Does that make sense?' or 'Do you have any questions?' to allow patient to acknowledge understanding NATURALLY."

        # Give only generic follow-up guidance based on what has actually been
        # said.  The previous implementation injected the reference's expected
        # symptom names here before the patient disclosed them.
        symptom_hint = ""
        if patient_turn_count and self.conversation_turn <= 8:
            symptom_hint = "Ask deeper follow-up questions about symptoms already mentioned (severity, duration, what makes it better/worse)."

        # Check for symptom over-repetition
        overmentioned_symptoms = detect_symptom_repetition(conversation_history)
        symptom_warning = ""
        if overmentioned_symptoms:
            symptom_warning = f"\n⚠️ CRITICAL: You've mentioned these symptoms too many times: {', '.join(overmentioned_symptoms)}. STOP repeating them!\n"

        # Get repetition stats for feedback
        repetition_stats = self.repetition_tracker.get_usage_stats()
        repetition_warning = ""
        if repetition_stats['phrase_counts']:
            # Find most overused phrases
            overused = [pattern for pattern, count in repetition_stats['phrase_counts'].items() if count >= 3]
            if overused:
                repetition_warning = f"\n⚠️ CRITICAL: You've overused these phrase patterns - COMPLETELY AVOID THEM:\n" + \
                                   "- Starting with 'Thank you for...'\n" + \
                                   "- Starting with 'I understand...'\n" + \
                                   "Use completely different openings!\n"

        # Enhanced prompt with variety, clinical depth, AND anti-repetition
        user_prompt_for_next_turn = (
            f"**Turn {self.conversation_turn} - {self.conversation_phase.title()} Phase**\n"
            f"Guidance: {phase_guidance}\n"
            f"{symptom_hint}\n"
            f"{repetition_warning}"
            f"{symptom_warning}\n"

            "**CRITICAL ANTI-REPETITION RULES:**\n"
            "- NEVER start with 'Thank you for sharing/letting me know/telling me'\n"
            "- NEVER start with 'I understand' or 'I'm sorry you're experiencing'\n"
            "- NEVER repeat the same symptoms back to the patient\n"
            "- Check your last 3 responses - use COMPLETELY different openings\n\n"

            "**Response guidelines:**\n"
            "1. START DIFFERENTLY than your last 3 responses\n"
            "   - Options: 'I see', 'Okay', 'Let me ask about...', 'Tell me more about...', 'Got it', or dive straight into question\n"
            "2. Ask ONE focused question OR provide clinical insight (depending on phase)\n"
            "3. Show empathy contextually (not every turn)\n"
            "4. Build on previous answers without repeating them\n"
            "5. If in synthesis/conclusion phase, explain clinical reasoning\n"
            "6. Provide practical value - education, reassurance, or actionable advice\n\n"

            + create_varied_prompt_examples('doctor') +

            "\nDoctor's response:"
        )

        llm_messages.append({"role": "user", "content": user_prompt_for_next_turn})

        # A ProviderError propagates rather than becoming an utterance (D-08).
        response_content = self._provider.complete(
            to_chat_messages(llm_messages),
            # The profile is derived from a MIMIC-III note.
            classification=DataClassification.RESTRICTED_CLINICAL,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            seed=self._seed,
        ).text

        # Track this response for repetition detection
        self.repetition_tracker.track_response(response_content)

        logger.info(f"[Doctor] Turn {self.conversation_turn} ({self.conversation_phase}): {response_content[:80]}...")
        return response_content
    
    def update_prompt(self, additional_instructions: str):
        self.coach_feedback_to_incorporate = additional_instructions
        logger.info("[Doctor] Coach feedback stored for next response.")
