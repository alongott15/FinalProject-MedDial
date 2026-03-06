import random
from typing import List

# ============================================================================
# DOCTOR RESPONSE VARIETY
# ============================================================================

DOCTOR_ACKNOWLEDGMENTS = [
    "I appreciate you telling me that",
    "That's helpful to know",
    "I see",
    "Okay",
    "Got it",
    "Thank you",
    "I understand",
    "",  # No acknowledgment - just continue
]

DOCTOR_EMPATHY_PHRASES = [
    "I can see this is concerning for you",
    "That sounds uncomfortable",
    "I understand this must be worrying",
    "That must be difficult",
    "I can imagine that's frustrating",
    "I hear your concern",
]

DOCTOR_TRANSITION_PHRASES = [
    "Let me ask about",
    "I'd like to know more about",
    "Can you tell me about",
    "Help me understand",
    "I'm curious about",
    "Tell me more about",
]

DOCTOR_REFLECTION_TEMPLATES = [
    "So if I understand correctly, {summary}",
    "Let me make sure I have this right - {summary}",
    "From what you're telling me, {summary}",
    "It sounds like {summary}",
    "What I'm hearing is {summary}",
]

DOCTOR_CLINICAL_REASONING = [
    "Based on what you're describing",
    "Given these symptoms",
    "From what you've shared",
    "Considering what you've told me",
    "Looking at the overall picture",
]

DOCTOR_EDUCATIONAL_PHRASES = [
    "What often happens with this is",
    "One thing to understand is",
    "The reason I ask is",
    "It's important to note that",
    "What this could mean is",
]

# ============================================================================
# PATIENT RESPONSE VARIETY
# ============================================================================

# Hesitation markers - use sparingly and contextually
PATIENT_HESITATIONS = [
    "",  # No hesitation (50% of time)
    "",  # No hesitation
    "Um... ",
    "Well... ",
    "Let me think... ",
    "Hmm... ",
    "I guess... ",
    "You know, ",
]

# Response starters for different contexts
PATIENT_UNCERTAIN_STARTERS = [
    "I'm not really sure, but ",
    "I think ",
    "I'm not certain, but ",
    "If I remember correctly, ",
    "I believe ",
]

PATIENT_CONFIDENT_STARTERS = [
    "Yes, ",
    "No, ",
    "Definitely ",
    "Actually, ",
    "",  # Direct answer
]

PATIENT_WORRY_EXPRESSIONS = [
    "I'm worried about",
    "I'm concerned about",
    "What worries me is",
    "I'm anxious about",
    "I'm nervous about",
    "That's what scares me -",
]

PATIENT_CLARIFICATION_REQUESTS = [
    "What do you mean by that?",
    "Can you explain that?",
    "I'm not sure I understand",
    "What does that mean?",
    "Could you clarify?",
]

# ============================================================================
# PERSONALITY-BASED VARIATIONS
# ============================================================================

class PatientPersonality:
    """Patient personality profiles that influence communication style"""

    @staticmethod
    def get_personality_traits(age: int, sex: str) -> dict:
        """Get personality-based communication traits"""
        traits = {
            "formality": 0.5,  # 0 = casual, 1 = formal
            "verbosity": 0.5,  # 0 = brief, 1 = detailed
            "directness": 0.5,  # 0 = indirect, 1 = direct
            "anxiety_level": 0.5,  # 0 = calm, 1 = anxious
            "medical_literacy": 0.5,  # 0 = low, 1 = high
        }

        # Age-based adjustments
        if age < 30:
            traits["formality"] = 0.3
            traits["directness"] = 0.7
            traits["medical_literacy"] = 0.6
        elif age < 60:
            traits["formality"] = 0.5
            traits["directness"] = 0.6
            traits["verbosity"] = 0.6
        else:  # 60+
            traits["formality"] = 0.7
            traits["directness"] = 0.4
            traits["verbosity"] = 0.7
            traits["anxiety_level"] = 0.6

        return traits

    @staticmethod
    def get_age_appropriate_language(age: int) -> dict:
        """Get age-appropriate language patterns"""
        if age < 30:
            return {
                "filler_words": ["like", "kind of", "sort of", "honestly"],
                "intensity": ["really", "super", "pretty"],
                "casual_phrases": ["I've been feeling pretty rough", "It's been rough"]
            }
        elif age < 60:
            return {
                "filler_words": ["you know", "I mean"],
                "intensity": ["quite", "rather", "very"],
                "casual_phrases": ["I haven't been feeling well", "I'm not feeling great"]
            }
        else:  # 60+
            return {
                "filler_words": ["well", "you see"],
                "intensity": ["quite", "rather"],
                "casual_phrases": ["I've been under the weather", "I'm not feeling myself"]
            }


# ============================================================================
# RESPONSE SELECTION FUNCTIONS
# ============================================================================

def get_doctor_acknowledgment(skip_probability: float = 0.3) -> str:
    """
    Get a varied doctor acknowledgment phrase.
    skip_probability: chance to return empty string (no acknowledgment)
    """
    if random.random() < skip_probability:
        return ""
    return random.choice(DOCTOR_ACKNOWLEDGMENTS)


def get_doctor_empathy(context: str = None) -> str:
    """Get contextual empathy phrase"""
    return random.choice(DOCTOR_EMPATHY_PHRASES)


def get_doctor_transition() -> str:
    """Get varied transition phrase"""
    return random.choice(DOCTOR_TRANSITION_PHRASES)


def get_doctor_reflection_start() -> str:
    """Get reflective listening phrase template"""
    return random.choice(DOCTOR_REFLECTION_TEMPLATES)


def get_patient_hesitation(is_uncertain: bool = False, use_hesitation_prob: float = 0.4) -> str:
    """
    Get patient hesitation marker.
    is_uncertain: if True, more likely to hesitate
    use_hesitation_prob: base probability of using hesitation
    """
    if is_uncertain:
        use_hesitation_prob = 0.7

    if random.random() < use_hesitation_prob:
        return random.choice(PATIENT_HESITATIONS)
    return ""


def get_patient_response_starter(is_uncertain: bool = False, is_confident: bool = False) -> str:
    """Get appropriate response starter based on certainty"""
    if is_confident:
        return random.choice(PATIENT_CONFIDENT_STARTERS)
    elif is_uncertain:
        return random.choice(PATIENT_UNCERTAIN_STARTERS)
    return ""


def should_use_filler_words(turn_number: int, personality_traits: dict) -> bool:
    """Determine if filler words should be used this turn"""
    # Less filler as conversation progresses
    base_prob = max(0.2, 0.5 - (turn_number * 0.03))

    # Adjust based on personality
    if personality_traits.get("directness", 0.5) > 0.7:
        base_prob *= 0.5

    return random.random() < base_prob


# ============================================================================
# CONVERSATION DYNAMICS
# ============================================================================

def should_doctor_summarize(turn_count: int, symptoms_discussed: int) -> bool:
    """Determine if doctor should summarize findings"""
    # Summarize in synthesis phase if enough symptoms discussed
    if 8 <= turn_count <= 12 and symptoms_discussed >= 3:
        return random.random() < 0.4
    return False


def should_doctor_explain_reasoning(turn_count: int, conversation_phase: str) -> bool:
    """Determine if doctor should explain clinical reasoning"""
    if conversation_phase in ["synthesis", "conclusion"] and turn_count >= 8:
        return random.random() < 0.5
    return False


def should_patient_ask_clarification(medical_term_used: bool, turn_count: int) -> bool:
    """Determine if patient should ask for clarification"""
    if medical_term_used and turn_count >= 3:
        return random.random() < 0.3
    return False


def build_natural_response(
    base_content: str,
    add_acknowledgment: bool = False,
    add_empathy: bool = False,
    add_transition: bool = False,
    is_patient: bool = False,
    is_uncertain: bool = False
) -> str:
    """
    Build a natural-sounding response with varied elements.

    Args:
        base_content: The main content of the response
        add_acknowledgment: Add doctor acknowledgment (doctor only)
        add_empathy: Add empathetic phrase (doctor only)
        add_transition: Add transition phrase (doctor only)
        is_patient: This is a patient response
        is_uncertain: Patient is uncertain (patient only)

    Returns:
        Naturally constructed response
    """
    parts = []

    if is_patient:
        # Patient response construction
        hesitation = get_patient_hesitation(is_uncertain)
        if hesitation:
            parts.append(hesitation)

        starter = get_patient_response_starter(is_uncertain)
        if starter:
            parts.append(starter)

        parts.append(base_content)
    else:
        # Doctor response construction
        if add_acknowledgment:
            ack = get_doctor_acknowledgment()
            if ack:
                parts.append(ack + ".")

        if add_empathy:
            parts.append(get_doctor_empathy() + ".")

        if add_transition:
            parts.append(get_doctor_transition())

        parts.append(base_content)

    return " ".join(parts).strip()


# ============================================================================
# CLINICAL DEPTH HELPERS
# ============================================================================

SYMPTOM_FOLLOW_UP_QUESTIONS = {
    "pain": [
        "Where exactly is the pain located?",
        "Can you describe what the pain feels like?",
        "On a scale of 1-10, how would you rate the pain?",
        "Does anything make it better or worse?",
        "When did you first notice the pain?",
    ],
    "shortness of breath": [
        "Does it happen at rest or with activity?",
        "Do you feel like you can't catch your breath?",
        "Does lying down make it better or worse?",
        "Have you had any wheezing?",
    ],
    "fever": [
        "Have you taken your temperature?",
        "When did the fever start?",
        "Have you noticed any chills or sweating?",
        "Does it come and go or stay constant?",
    ],
    "headache": [
        "Where is the headache located?",
        "What does it feel like - throbbing, sharp, dull?",
        "Have you had headaches like this before?",
        "Does anything trigger it or make it worse?",
    ],
    "cough": [
        "Is it a dry cough or are you bringing anything up?",
        "How long have you had the cough?",
        "Is it worse at certain times of day?",
        "Does anything trigger the coughing?",
    ],
    "fatigue": [
        "How long have you been feeling tired?",
        "Does rest help, or do you still feel exhausted?",
        "Has this affected your daily activities?",
        "Any changes in your sleep patterns?",
    ],
    "dizziness": [
        "Is it a spinning sensation or more like lightheadedness?",
        "When does it happen - standing up, turning your head?",
        "How long do the episodes last?",
        "Any nausea or vomiting with it?",
    ]
}


def get_symptom_follow_up_question(symptom_description: str) -> str:
    """Get a relevant follow-up question for a symptom"""
    symptom_lower = symptom_description.lower()

    for key_symptom, questions in SYMPTOM_FOLLOW_UP_QUESTIONS.items():
        if key_symptom in symptom_lower:
            return random.choice(questions)

    # Generic follow-up if no specific match
    generic_questions = [
        "When did this start?",
        "Has it been getting better or worse?",
        "Does anything seem to trigger it?",
        "How much is this affecting your day-to-day life?",
    ]
    return random.choice(generic_questions)


# ============================================================================
# FEW-SHOT EXAMPLES FOR VARIED RESPONSES
# ============================================================================

def create_varied_prompt_examples(agent_type: str) -> str:
    """Create few-shot examples showing varied responses"""

    if agent_type == 'doctor':
        return """
**Examples of VARIED doctor responses (use these as inspiration, not templates):**

Example 1: "I see. Tell me more about when this started."
Example 2: "Okay. How severe would you say the pain is on a scale of 1-10?"
Example 3: "Let me ask about associated symptoms - any nausea or dizziness?"
Example 4: "That's helpful information. Does anything make it better or worse?"
Example 5: "Based on what you're describing, I'd like to understand the timing better."
Example 6: "Alright. Have you noticed any patterns to when this happens?"
Example 7: "Got it. One more question - does this affect your daily activities?"

Notice: Each starts DIFFERENTLY. No repetitive "Thank you for sharing".
"""
    else:  # patient
        return """
**Examples of VARIED patient responses (use these as inspiration, not templates):**

Example 1: "It started about a week ago."
Example 2: "I'd say maybe a 6 or 7."
Example 3: "No, I haven't had any nausea."
Example 4: "Resting seems to help a little."
Example 5: "It's mostly in the evenings."
Example 6: "Yeah, it's hard to do my usual activities."
Example 7: "I'm not sure exactly, maybe a few days?"

Notice: Some direct answers, some with mild hesitation. NOT all starting with "Um..."
"""
