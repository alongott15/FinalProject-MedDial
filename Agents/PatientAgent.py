import logging
from Utils.llms_utils import load_gpt_model, chat_generate
from Utils.bias_aware_prompts import BASE_SYSTEM_PROMPT, PATIENT_PROFILE_TYPE_KNOWLEDGE
from Utils.conversation_variety import PatientPersonality, create_varied_prompt_examples
from Utils.repetition_filter import RepetitionTracker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PatientAgent:
    def __init__(self, profile: dict, llm=None):
        if llm:
            self.llm = llm
        else:
            logger.info("LLM not provided to PatientAgent, loading Azure AI model internally.")
            self.llm = load_gpt_model(temperature=0.6, max_tokens=300)

        self.profile = profile
        self.coach_feedback_to_incorporate = None
        self.mentioned_symptoms = set()
        self.emotional_state = self._determine_emotional_state()
        self.conversation_turn = 0

        # Profile type — controls what the patient knows and may disclose
        self.profile_type = profile.get("profile_type", "NO_DIAGNOSIS_NO_TREATMENT")
        logger.info(f"[Patient] Profile type: {self.profile_type}")

        # Track symptoms for gradual, natural disclosure
        self.symptoms_to_disclose = self._prepare_gradual_disclosure()

        # Add repetition tracking
        self.repetition_tracker = RepetitionTracker("PatientAgent")

        # Extract profile fields
        demographics_str = self._get_demographics(profile)
        chief_complaint_str = self._get_chief_complaint(profile)
        medical_history_str = self._get_medical_history(profile)
        allergies_str = self._get_allergies(profile)
        current_medications_str = self._get_current_medications(profile)
        symptoms_str = self._get_symptoms(profile)

        # Profile-type-specific fields (only included if the patient should know them)
        diagnosis_str = self._get_diagnosis(profile) if self.profile_type == "FULL" else None
        treatment_str = (
            self._get_treatment_options(profile)
            if self.profile_type in ("FULL", "NO_DIAGNOSIS")
            else None
        )

        # Profile-type knowledge instruction (what the patient knows / must NOT reveal)
        knowledge_instruction = self._get_profile_knowledge_instruction()

        # Enhanced patient persona with personality traits
        self.patient_persona = self._create_patient_persona()
        self.personality_traits = self._determine_personality_traits()

        # Get demographic info for personality modeling
        demo = self.profile.get("Context_Fields", {}).get("Patient_Demographics", {})
        age = demo.get('Age', 50)
        sex = demo.get('Sex', 'Unknown')

        # Get personality-based communication traits
        self.personality_profile = PatientPersonality.get_personality_traits(age, sex)
        self.age_language = PatientPersonality.get_age_appropriate_language(age)

        # Create age-appropriate communication style description
        age_style_desc = self._get_age_communication_style(age)

        # Build the profile section — only expose fields the patient is allowed to know
        profile_section = (
            "**YOUR PROFILE (STRICT — ONLY USE INFORMATION BELOW):**\n"
            f"- Demographics: {demographics_str}\n"
            f"- Chief Complaint: {chief_complaint_str}\n"
            f"- Symptoms you are experiencing: {symptoms_str}\n"
            f"- Medical History: {medical_history_str}\n"
            f"- Allergies: {allergies_str}\n"
            f"- Current Medications: {current_medications_str}\n"
        )
        if self.profile_type == "FULL" and diagnosis_str:
            profile_section += f"- Your Diagnosis (you know this): {diagnosis_str}\n"
        if self.profile_type in ("FULL", "NO_DIAGNOSIS") and treatment_str:
            profile_section += f"- Treatment Plan (you know this): {treatment_str}\n"

        # Profile-type-specific conclusion behaviour text (computed before building
        # the system message so it can be used in implicit string concatenation below).
        if self.profile_type in ("NO_DIAGNOSIS", "NO_DIAGNOSIS_NO_TREATMENT"):
            conclusion_behaviour = (
                "- **When doctor gives assessment/conclusion**: This is NEW information for you! "
                "React with genuine curiosity — ask ONE follow-up question at a time about what this "
                "means, what to expect, any side effects, lifestyle changes, or next steps. "
                "Only say you have no more questions when you genuinely don't "
                "(e.g. 'I think that covers everything, thank you.').\n\n"
            )
        else:
            conclusion_behaviour = (
                "- **When doctor gives assessment/conclusion**: Respond naturally — acknowledge "
                "understanding, ask a clarifying question if needed, or express relief/concern\n\n"
            )

        self.system_message = {
            "role": "system",
            "content": (
                f"{BASE_SYSTEM_PROMPT}\n\n"

                "**YOUR ROLE:**\n"
                f"You are {self.patient_persona} seeking medical help.\n"
                f"Emotional state: {self.emotional_state}\n"
                f"Communication style: {age_style_desc}\n\n"

                f"{profile_section}\n"

                f"{knowledge_instruction}\n\n"

                "**CRITICAL GROUNDING RULES:**\n"
                "1. ONLY discuss symptoms listed in your profile above\n"
                "2. If the doctor asks about symptoms NOT in your profile, say you have not experienced them\n"
                "3. Do NOT invent new symptoms, test results, or medical history\n"
                "4. If you do not know a detail (exact duration, specific time), say you are not sure\n"
                "5. Strictly follow your profile-type knowledge boundaries — see above\n\n"

                "**NATURAL CONVERSATION BEHAVIOUR:**\n"
                "- **Turn 1-2**: Share only your main concern briefly, with some initial hesitation\n"
                "- **Turn 3-5**: When asked, reveal 1-2 additional symptoms gradually\n"
                "- **Turn 6+**: Feel more comfortable — less hesitation, more direct answers\n"
                f"{conclusion_behaviour}"

                "**COMMUNICATION STYLE — VARY YOUR RESPONSES:**\n"
                "- Use everyday language initially: 'my chest hurts', 'hard to breathe'\n"
                "- Mirror the doctor's medical terms when they use them\n"
                "- **IMPORTANT**: Do not start every response with 'Um...' or 'Well...'\n"
                "  - Use hesitations ONLY when actually uncertain or uncomfortable\n"
                "  - When answering clear questions, respond more directly\n"
                "- Express uncertainty contextually: 'I think...', 'Maybe...', 'I am not sure if...'\n"
                "- Do not ask 'Should I be worried?' repeatedly — vary your concerns\n"
                "- Keep responses brief and natural (1-3 sentences typically)\n"
            )
        }

    # ── Profile-type helpers ────────────────────────────────────────────────

    def _get_profile_knowledge_instruction(self) -> str:
        """Return the knowledge-boundary instruction block for this profile type."""
        knowledge = PATIENT_PROFILE_TYPE_KNOWLEDGE.get(
            self.profile_type,
            PATIENT_PROFILE_TYPE_KNOWLEDGE["NO_DIAGNOSIS_NO_TREATMENT"]
        )
        return knowledge["system_instruction"]

    def _get_diagnosis(self, profile: dict) -> str:
        """Extract diagnosis string from Core_Fields (used only for FULL profiles)."""
        diagnoses = profile.get("Core_Fields", {}).get("Diagnoses", [])
        if not diagnoses:
            return "Not specified in profile."
        diag_parts = []
        for d in diagnoses:
            if isinstance(d, dict):
                primary = d.get("primary", "")
                notes = d.get("notes", "")
                if primary:
                    if notes and notes != "not provided":
                        diag_parts.append(f"{primary} ({notes})")
                    else:
                        diag_parts.append(primary)
            elif isinstance(d, str) and d.strip():
                diag_parts.append(d.strip())
        return "; ".join(diag_parts) if diag_parts else "Not specified in profile."

    def _get_treatment_options(self, profile: dict) -> str:
        """Extract treatment options from Core_Fields (used for FULL and NO_DIAGNOSIS profiles)."""
        treatments = profile.get("Core_Fields", {}).get("Treatment_Options", [])
        if not treatments:
            return "Not specified in profile."
        tx_parts = []
        for t in treatments:
            if isinstance(t, dict):
                procedure = t.get("procedure", "")
                treatment = t.get("treatment", "")
                meds = t.get("medications", [])
                med_names = [
                    m.get("name", "") for m in meds
                    if isinstance(m, dict) and m.get("name")
                ]
                parts = []
                if procedure and procedure != "not provided":
                    parts.append(procedure)
                if treatment and treatment != "not provided":
                    parts.append(treatment)
                if med_names:
                    parts.append(f"medications: {', '.join(med_names)}")
                if parts:
                    tx_parts.append("; ".join(parts))
            elif isinstance(t, str) and t.strip():
                tx_parts.append(t.strip())
        return " | ".join(tx_parts) if tx_parts else "Not specified in profile."

    # ── Existing helpers ────────────────────────────────────────────────────

    def _get_age_communication_style(self, age: int) -> str:
        """Get age-appropriate communication style description."""
        if age < 30:
            return "Direct and casual, may use modern expressions, asks questions freely"
        elif age < 60:
            return "Balanced and clear, practical communication, experienced with healthcare"
        else:
            return "Respectful and detailed, may have some recall hesitation, values doctor's guidance"

    def _prepare_gradual_disclosure(self) -> list:
        symptoms_list = self.profile.get("Core_Fields", {}).get("Symptoms", [])
        symptoms = []

        for sym in symptoms_list:
            if isinstance(sym, dict):
                desc = sym.get('description', '').strip()
                if desc:
                    symptoms.append(desc.lower())
            elif isinstance(sym, str) and sym.strip():
                symptoms.append(sym.strip().lower())

        priority_symptoms = []
        secondary_symptoms = []

        for symptom in symptoms:
            if any(term in symptom for term in ['pain', 'hurt', 'breath', 'chest']):
                priority_symptoms.append(symptom)
            else:
                secondary_symptoms.append(symptom)

        return priority_symptoms + secondary_symptoms

    def _create_patient_persona(self) -> str:
        demo = self.profile.get("Context_Fields", {}).get("Patient_Demographics", {})
        age = demo.get('Age', 0)
        sex = demo.get('Sex', 'person')

        if age < 30:
            age_desc = "young adult"
        elif age < 60:
            age_desc = "middle-aged person"
        else:
            age_desc = "older adult"

        return f"a {age_desc} {sex.lower()} patient"

    def _determine_personality_traits(self) -> str:
        demo = self.profile.get("Context_Fields", {}).get("Patient_Demographics", {})
        age = demo.get('Age', 0)

        if age < 30:
            base_traits = ["somewhat anxious about health", "asks direct questions"]
        elif age < 60:
            base_traits = ["experienced with healthcare", "practical communicator"]
        else:
            base_traits = ["respectful of medical authority", "detailed in descriptions"]

        return ", ".join(base_traits)

    def _determine_emotional_state(self) -> str:
        symptoms = self.profile.get("Core_Fields", {}).get("Symptoms", [])
        symptom_text = " ".join([str(s).lower() for s in symptoms])

        if any(indicator in symptom_text for indicator in ["chest pain", "breath", "severe"]):
            return "anxious and worried"
        elif any(indicator in symptom_text for indicator in ["pain", "hurt", "discomfort"]):
            return "uncomfortable and seeking relief"
        else:
            return "concerned but hopeful"

    def _get_demographics(self, profile: dict) -> str:
        demo = profile.get("Context_Fields", {}).get("Patient_Demographics", {})
        if not demo:
            return "Not specified in profile."
        return f"Age: {demo.get('Age', 'N/A')}, Sex: {demo.get('Sex', 'N/A')}"

    def _get_chief_complaint(self, profile: dict) -> str:
        return profile.get("Additional_Context", {}).get("Chief_Complaint", "Not specified in profile.")

    def _get_medical_history(self, profile: dict) -> str:
        history_data = profile.get("Context_Fields", {}).get("Medical_History", {})
        if isinstance(history_data, dict):
            return history_data.get("Past_Medical_History", "Not specified in profile.")
        return "Not specified in profile."

    def _get_allergies(self, profile: dict) -> str:
        allergies = profile.get("Context_Fields", {}).get("Allergies", [])
        return ", ".join(allergies) if allergies else "None known or not specified in profile."

    def _get_current_medications(self, profile: dict) -> str:
        meds = profile.get("Context_Fields", {}).get("Current_Medications", [])
        if not meds:
            return "Not specified in profile."
        med_names = []
        for med_item in meds:
            if isinstance(med_item, dict) and "name" in med_item:
                med_names.append(med_item["name"])
            elif isinstance(med_item, str):
                med_names.append(med_item)
        return ", ".join(med_names) if med_names else "Not specified in profile."

    def _get_symptoms(self, profile: dict) -> str:
        symptoms_list = profile.get("Core_Fields", {}).get("Symptoms", [])
        if not symptoms_list:
            return "No specific symptoms listed in profile to discuss."
        symptom_details = []
        for sym_item in symptoms_list:
            if isinstance(sym_item, dict):
                desc = sym_item.get('description')
                if not desc:
                    continue
                symptom_details.append(desc)
            elif isinstance(sym_item, str):
                symptom_details.append(sym_item)
        return "; ".join(symptom_details) if symptom_details else "No specific symptoms listed in profile to discuss."

    def _get_symptoms_for_turn(self) -> list:
        available_symptoms = [s for s in self.symptoms_to_disclose if s not in self.mentioned_symptoms]

        if self.conversation_turn <= 2:
            return available_symptoms[:1] if available_symptoms else []
        elif self.conversation_turn <= 5:
            return available_symptoms[:2] if available_symptoms else []
        else:
            return available_symptoms[:3] if available_symptoms else []

    # ── Response generation ─────────────────────────────────────────────────

    def respond(self, conversation_history: list) -> str:
        self.conversation_turn += 1

        llm_messages = [self.system_message]
        for message in conversation_history:
            if message['role'].lower() == 'patient':
                llm_messages.append({'role': 'assistant', 'content': message['content']})
            elif message['role'].lower() == 'doctor':
                llm_messages.append({'role': 'user', 'content': message['content']})

        if self.coach_feedback_to_incorporate:
            llm_messages.append({'role': 'user', 'content': f"Feedback for improvement: {self.coach_feedback_to_incorporate}"})
            self.coach_feedback_to_incorporate = None

        # Determine what symptoms can be mentioned this turn (gradual disclosure)
        symptoms_to_mention = self._get_symptoms_for_turn()

        if conversation_history and conversation_history[-1]['role'].lower() == 'doctor':
            last_doctor_message = conversation_history[-1]['content']
            doctor_lower = last_doctor_message.lower()

            # Check if doctor is asking about symptoms
            asking_about_symptoms = any(phrase in doctor_lower for phrase in [
                "tell me", "describe", "any other", "what else", "symptoms", "how do you feel",
                "experiencing", "happening"
            ])

            # Detect if doctor is giving assessment/conclusion
            doctor_concluding = any(phrase in doctor_lower for phrase in [
                "based on", "from what you've told me", "my assessment", "sounds like",
                "appears to be", "likely", "recommend", "suggest", "treatment", "next steps",
                "what i think", "my recommendation", "you should", "i'd advise"
            ])

            # Turn-based guidance for natural conversation with personality
            if doctor_concluding:
                if self.profile_type in ("NO_DIAGNOSIS", "NO_DIAGNOSIS_NO_TREATMENT"):
                    turn_guidance = (
                        "The doctor has just shared their diagnosis/assessment — this is NEW information "
                        "for you. React naturally and ask ONE follow-up question: what does this mean for "
                        "you, what should you expect, are there side effects, what lifestyle changes are "
                        "needed, or what are the next steps. Ask one question at a time. "
                        "Only say you have no more questions when you genuinely have none "
                        "(e.g. 'I think that covers everything, thank you' or 'No more questions')."
                    )
                else:
                    turn_guidance = (
                        "The doctor is giving their assessment/recommendations. Respond NATURALLY — "
                        "acknowledge understanding ('Okay', 'That makes sense'), ask a clarifying "
                        "question if something is unclear, or express how you feel about the assessment."
                    )
            elif self.conversation_turn <= 2:
                turn_guidance = "Brief and slightly hesitant initially. Share only your main concern. Don't overuse 'Um...' or 'Well...'."
            elif self.conversation_turn <= 5:
                turn_guidance = "Share more details when asked. Hesitate only when genuinely uncertain, not every response."
            else:
                turn_guidance = "Feel comfortable — be more direct and less hesitant. Answer questions clearly."

            # Symptom disclosure hint
            symptom_hint = ""
            if symptoms_to_mention and asking_about_symptoms:
                symptom_hint = f"Can mention if relevant: {', '.join(symptoms_to_mention[:2])}"
                self.mentioned_symptoms.update(symptoms_to_mention[:2])

            # Hesitation guidance based on turn
            if self.conversation_turn <= 2:
                hesitation_guidance = "You may use a brief hesitation if uncertain."
            elif self.conversation_turn > 5:
                hesitation_guidance = "You're more comfortable now — answer more directly, less hesitation."
            else:
                hesitation_guidance = "Use hesitation only when genuinely uncertain about the answer."

            # Profile-type disclosure reminder (injected every turn)
            profile_knowledge = PATIENT_PROFILE_TYPE_KNOWLEDGE.get(
                self.profile_type,
                PATIENT_PROFILE_TYPE_KNOWLEDGE["NO_DIAGNOSIS_NO_TREATMENT"]
            )
            disclosure_reminder = (
                f"\n⚠️ PROFILE REMINDER ({self.profile_type}): {profile_knowledge['disclosure_rules']}\n"
            )

            # Check repetition stats
            repetition_stats = self.repetition_tracker.get_usage_stats()
            repetition_warning = ""
            if repetition_stats['phrase_counts']:
                overused = [p for p, c in repetition_stats['phrase_counts'].items() if c >= 2]
                if overused:
                    repetition_warning = (
                        f"\n⚠️ CRITICAL: You've started responses with 'Um...' or 'Well...' "
                        f"{len(overused)} times. STOP! Answer directly.\n"
                    )

            user_prompt_for_next_turn = (
                f"Turn {self.conversation_turn}\n"
                f"Doctor said: \"{last_doctor_message}\"\n"
                f"Guidance: {turn_guidance}\n"
                f"{hesitation_guidance}\n"
                f"{symptom_hint}\n"
                f"{disclosure_reminder}"
                f"{repetition_warning}\n"

                "**CRITICAL ANTI-REPETITION RULES:**\n"
                "- DO NOT start with 'Um...', 'Well...', or 'Uh...'\n"
                "- DO NOT ask 'Should I be worried?' or 'Is this serious?' again\n"
                "- Answer DIRECTLY if doctor asks a clear question\n"
                "- Check your last 3 responses — use COMPLETELY different openings\n\n"

                "**Response guidelines:**\n"
                "1. Keep response brief and natural (1-3 sentences)\n"
                "2. START DIFFERENTLY than your last 3 responses\n"
                "3. Use everyday language, but mirror doctor's medical terms when appropriate\n"
                "4. ONLY discuss symptoms from your profile\n"
                "5. Follow your profile-type knowledge boundaries (see system instructions above)\n\n"

                + create_varied_prompt_examples('patient') +

                "\nPatient's response:"
            )
            llm_messages.append({"role": "user", "content": user_prompt_for_next_turn})
        else:
            # Opening response
            profile_knowledge = PATIENT_PROFILE_TYPE_KNOWLEDGE.get(
                self.profile_type,
                PATIENT_PROFILE_TYPE_KNOWLEDGE["NO_DIAGNOSIS_NO_TREATMENT"]
            )
            user_prompt_for_next_turn = (
                f"Turn {self.conversation_turn} — Starting consultation\n"
                "Share only your main concern briefly. Be somewhat hesitant initially, but don't start with 'Um...'.\n"
                f"⚠️ PROFILE REMINDER ({self.profile_type}): {profile_knowledge['disclosure_rules']}\n\n"

                + create_varied_prompt_examples('patient') +

                "\nPatient's response:"
            )
            llm_messages.append({"role": "user", "content": user_prompt_for_next_turn})

        response_content = chat_generate(self.llm, llm_messages)

        # Track this response for repetition detection
        self.repetition_tracker.track_response(response_content)

        logger.info(f"[Patient] Turn {self.conversation_turn} ({self.profile_type}): {response_content[:80]}...")
        return response_content

    def update_prompt(self, additional_instructions: str):
        self.coach_feedback_to_incorporate = additional_instructions
        logger.info("[Patient] Coach feedback stored for next response.")
