import logging
import random
from Utils.llms_utils import load_gpt_model, chat_generate
from Utils.bias_aware_prompts import BASE_SYSTEM_PROMPT, PATIENT_PROFILE_TYPE_KNOWLEDGE
from Utils.conversation_variety import should_doctor_summarize, should_doctor_explain_reasoning, get_symptom_follow_up_question, DOCTOR_CLINICAL_REASONING, DOCTOR_EDUCATIONAL_PHRASES, create_varied_prompt_examples
from Utils.repetition_filter import RepetitionTracker, detect_symptom_repetition

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DoctorAgent:
    def __init__(self, patient_profile: dict = None):
        self.llm = load_gpt_model(temperature=0.5, max_tokens=300)  # Increased for more natural variation
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

        # Profile-type context: what the patient already knows when they arrive
        profile_type = patient_profile.get("profile_type", "NO_DIAGNOSIS_NO_TREATMENT") if patient_profile else "NO_DIAGNOSIS_NO_TREATMENT"
        self.profile_type = profile_type
        profile_knowledge = PATIENT_PROFILE_TYPE_KNOWLEDGE.get(
            profile_type, PATIENT_PROFILE_TYPE_KNOWLEDGE["NO_DIAGNOSIS_NO_TREATMENT"]
        )
        profile_type_guidance = self._build_profile_type_guidance(profile_type, profile_knowledge)

        self.system_message = {
            "role": "system",
            "content": (
                f"{BASE_SYSTEM_PROMPT}\n\n"

                "**YOUR ROLE:**\n"
                f"You are a primary care physician conducting a consultation for a patient with a light, common medical issue.\n"
                f"Patient demographics: {demographics_info}\n"
                f"Available patient data: {data_available}\n\n"

                f"{profile_type_guidance}\n\n"

                "**CONSULTATION APPROACH FOR LIGHT CASES:**\n"
                "1. Start with a warm greeting and open-ended question (e.g., 'How have you been feeling lately?')\n"
                "2. Listen to patient's chief complaint and explore key symptoms with focused follow-up questions\n"
                "3. PRIORITIZE the most important questions - quality over quantity\n"
                "4. After 6-8 exchanges, if you have enough information, provide assessment and conclude\n"
                "5. Show empathy naturally and contextually (not every turn)\n"
                "6. Provide education and clinical reasoning - explain WHY you're asking certain questions\n"
                "7. Occasionally summarize what you've heard to show active listening\n"
                "8. Keep questions appropriate for a light, common condition (not severe/ICU-level)\n\n"

                "**COMMUNICATION GUIDELINES - NATURAL CONVERSATION:**\n"
                "- Vary your response style - don't start every response with 'Thank you' or 'I understand'\n"
                "- Sometimes acknowledge briefly ('I see', 'Okay'), sometimes just continue directly\n"
                "- Ask follow-up questions to explore symptoms in depth (severity, duration, triggers, alleviating factors)\n"
                "- Build on what patient shares naturally\n"
                "- Reference earlier parts of conversation when relevant\n"
                "- Use transitional phrases: 'Let me ask about...', 'Tell me more about...'\n"
                "- Occasionally explain your clinical thinking: 'Based on what you're describing...'\n"
                "- Provide brief education when appropriate: 'What often happens with this is...'\n\n"

                "**PROVIDE CLINICAL VALUE:**\n"
                "- Explain likely mechanisms or causes in simple terms when appropriate\n"
                "- Educate about warning signs to watch for\n"
                "- Offer reassurance when findings suggest common, benign issues\n"
                "- Provide practical self-care advice beyond just 'see your doctor'\n"
                "- Help patient understand connections between symptoms\n\n"

                "**AVOID REPETITION:**\n"
                "- Don't ask about the same symptom multiple times unless seeking clarification\n"
                "- Vary your phrasing and approach\n"
                "- Don't repeat the same symptoms back to the patient every turn\n"
                "- Progress the conversation forward\n\n"

                "**CRITICAL GROUNDING RULES:**\n"
                "- Base your questions and assessment ONLY on what the patient tells you in the conversation\n"
                "- Do not assume or invent symptoms, test results, or history not mentioned\n"
                "- If you're unsure about something, ask the patient directly\n"
                "- Do not escalate a light case to severe diagnoses without strong evidence from conversation\n"
                "- Stay focused on light, common conditions (cough, sore throat, headache, mild fever, etc.)\n"
            )
        }


    def _build_profile_type_guidance(self, profile_type: str, profile_knowledge: dict) -> str:
        """
        Return consultation guidance specific to what the patient already knows.

        The doctor adapts its questioning strategy based on the patient's
        starting knowledge state so the conversation remains coherent:

        - FULL: patient already knows their diagnosis and treatment — the
          consultation is about understanding, follow-up, or concerns.
        - NO_DIAGNOSIS: patient knows their symptoms and medications but not
          the diagnosis — the doctor should focus on reaching and explaining a diagnosis.
        - NO_DIAGNOSIS_NO_TREATMENT: patient knows only symptoms — this is
          typically a first consultation; the doctor gathers information and
          then provides both diagnosis and treatment plan.
        """
        if profile_type == "FULL":
            return (
                "**PATIENT KNOWLEDGE CONTEXT — FULL PROFILE:**\n"
                "This patient is already aware of their diagnosis and treatment plan. "
                "They may reference their condition or medications during the consultation.\n"
                "- Do NOT act surprised if the patient mentions their diagnosis by name.\n"
                "- Focus questions on symptom details, current management, and any new concerns.\n"
                "- Your assessment should align with or refine the patient's existing understanding.\n"
                "- You can confirm or clarify the diagnosis the patient already knows."
            )
        elif profile_type == "NO_DIAGNOSIS":
            return (
                "**PATIENT KNOWLEDGE CONTEXT — NO DIAGNOSIS PROFILE:**\n"
                "This patient knows their symptoms and current medications but has NOT been "
                "told their formal diagnosis. They may mention their medications without knowing "
                "what condition those medications treat.\n"
                "- Do NOT assume the patient knows what is wrong with them.\n"
                "- If the patient mentions medications, you may ask why they were prescribed "
                "(but the patient may not know the reason).\n"
                "- A key goal of this consultation is to gather enough information to reach "
                "and clearly communicate a diagnosis.\n"
                "- When you conclude, explain the diagnosis clearly — this may be new information "
                "for the patient."
            )
        else:  # NO_DIAGNOSIS_NO_TREATMENT
            return (
                "**PATIENT KNOWLEDGE CONTEXT — SYMPTOMS ONLY PROFILE:**\n"
                "This patient knows ONLY their symptoms. They have no prior diagnosis or "
                "treatment plan for this condition — this is effectively a first consultation.\n"
                "- The patient will not mention any diagnosis or treatment plan.\n"
                "- Do NOT ask 'are you already on treatment for this?' unless symptom context "
                "makes it clinically relevant.\n"
                "- This consultation has two clear goals: (1) gather a thorough symptom picture "
                "and (2) provide both a diagnosis and a treatment/management plan at the end.\n"
                "- Conclude with a complete assessment: diagnosis + clear treatment recommendations."
            )

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

        response_content = chat_generate(self.llm, llm_messages)

        # Track this response for repetition detection
        self.repetition_tracker.track_response(response_content)

        logger.info(f"[Doctor] Turn {self.conversation_turn} ({self.conversation_phase}): {response_content[:80]}...")
        return response_content
    
    def update_prompt(self, additional_instructions: str):
        self.coach_feedback_to_incorporate = additional_instructions
        logger.info("[Doctor] Coach feedback stored for next response.")