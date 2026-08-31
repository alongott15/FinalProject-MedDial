import logging
import random
from meddial.llm import DataClassification, LLMProvider, to_chat_messages
from Utils.bias_aware_prompts import DOCTOR_GUIDANCE, DOCTOR_SYSTEM_PROMPT
from Utils.conversation_variety import should_doctor_summarize, should_doctor_explain_reasoning, get_symptom_follow_up_question, DOCTOR_CLINICAL_REASONING, DOCTOR_EDUCATIONAL_PHRASES, create_varied_prompt_examples
from Utils.repetition_filter import RepetitionTracker, detect_symptom_repetition

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DoctorAgent:
    def __init__(
        self,
        provider: LLMProvider,
        patient_profile: dict = None,
        *,
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
        self.patient_profile = patient_profile
        self.coach_feedback_to_incorporate = None
        self.conversation_phase = "opening"
        self.discussed_symptoms = set()
        self.conversation_turn = 0
        self.last_patient_emotion = "neutral"

        # Add repetition tracking
        self.repetition_tracker = RepetitionTracker("DoctorAgent")

        # Extract demographics and profile information
        demographics_info = "Not specified"
        available_data_summary = []

        if patient_profile:
            # Demographics
            demo = patient_profile.get("Context_Fields", {}).get("Patient_Demographics", {})
            if demo:
                demographics_info = (
                    f"Age: {demo.get('Age', 'Not provided')}, "
                    f"Sex: {demo.get('Sex', 'Not provided')}"
                )

            # Check what data is available
            if patient_profile.get("Core_Fields", {}).get("Symptoms"):
                available_data_summary.append("symptoms reported in profile")
            if patient_profile.get("Context_Fields", {}).get("Medical_History"):
                available_data_summary.append("medical history")
            if patient_profile.get("Context_Fields", {}).get("Allergies"):
                available_data_summary.append("allergy information")

        # Extract key symptoms for guidance
        self.key_symptoms = []
        if patient_profile:
            symptoms = patient_profile.get("Core_Fields", {}).get("Symptoms", [])
            for symptom in symptoms:
                if isinstance(symptom, dict):
                    desc = symptom.get("description", "").strip()
                    if desc:
                        self.key_symptoms.append(desc.lower())

        data_available = ", ".join(available_data_summary) if available_data_summary else "limited patient data"

        # What the doctor is briefed to expect is its own experimental factor
        # (D-05). It defaults to the patient's policy so a normal run is
        # unchanged, but the caller can cross the two, and only the caller may:
        # reading the briefing off the patient's policy here is what made
        # disclosure and briefing a single treatment in the thesis pipeline.
        profile_type = patient_profile.get("profile_type", "NO_DIAGNOSIS_NO_TREATMENT") if patient_profile else "NO_DIAGNOSIS_NO_TREATMENT"
        self.profile_type = profile_type
        self.guidance_id = guidance_id or profile_type
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

    def _track_clinical_findings(self, conversation_history: list):
        if not conversation_history:
            return

        recent_patient_responses = [
            msg['content'].lower() for msg in conversation_history[-4:]
            if msg.get('role', '').lower() == 'patient'
        ]

        for response in recent_patient_responses:
            for symptom in self.key_symptoms:
                if symptom.lower() in response and symptom not in self.discussed_symptoms:
                    self.discussed_symptoms.add(symptom)

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
        self._track_clinical_findings(conversation_history)

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
            if should_doctor_summarize(self.conversation_turn, len(self.discussed_symptoms)):
                phase_guidance += " Consider briefly summarizing what you've learned so far."
            # Encourage natural transition to conclusion if sufficient coverage
            if self.conversation_turn >= 6 and len(self.discussed_symptoms) >= 2:
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

        # Track symptom exploration with follow-up suggestions
        remaining_symptoms = [s for s in self.key_symptoms if s not in self.discussed_symptoms]
        symptom_hint = ""
        if remaining_symptoms and self.conversation_turn <= 10:
            # Provide specific follow-up question suggestion
            first_symptom = remaining_symptoms[0]
            follow_up = get_symptom_follow_up_question(first_symptom)
            symptom_hint = f"Unexplored symptoms: {', '.join(remaining_symptoms[:2])}. Example follow-up: '{follow_up}'"
        elif self.discussed_symptoms and self.conversation_turn <= 8:
            # Suggest deeper exploration of discussed symptoms
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