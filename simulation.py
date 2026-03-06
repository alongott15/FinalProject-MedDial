import logging
import time
import collections
from typing import Tuple, List, Dict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Natural conversation conclusion keywords - includes transitional phrases
DOCTOR_CONCLUSION_KEYWORDS = [
    "based on our conversation", "my assessment is", "based on what you've told me",
    "i believe you have", "my diagnosis is", "this appears to be", "what i think is happening",
    "my recommendation is", "i'd like to recommend", "the treatment plan", "our next steps",
    "my clinical impression", "i think we're looking at", "the most likely explanation",
    "i would diagnose this as", "my assessment shows", "given your symptoms",
    # Natural transitional phrases
    "from what you've shared", "from what you've told me", "let me explain what i'm thinking",
    "let me share my thoughts", "based on what we've discussed", "here's what i think",
    "let me walk you through", "my sense is that", "what this sounds like to me"
]


PATIENT_UNDERSTANDING_KEYWORDS = [
    # Direct understanding
    "i understand", "that makes sense", "okay", "thank you", "alright",
    "got it", "i see", "that helps", "sounds good", "that's clear",
    "i think i understand", "that explains it",
    # More natural acknowledgments
    "that's helpful", "appreciate", "makes sense", "good to know",
    "i feel better", "that's reassuring", "okay i'll do that",
    # Simple affirmations (very common in real conversations)
    "yes", "sure", "right", "yeah", "ok", "great", "perfect",
    # Grateful closings
    "thanks", "thank", "appreciated"
]

PATIENT_CONFUSION_KEYWORDS = [
    "i don't understand", "can you explain", "what does that mean", "i'm confused",
    "could you clarify", "what is", "i'm not sure", "can you repeat",
    "could you go over that", "i need more explanation", "what exactly"
]

# Phrases that signal the patient has genuinely exhausted their questions.
# Used for NO_DIAGNOSIS / NO_DIAGNOSIS_NO_TREATMENT profiles where the
# conversation must continue until the patient explicitly has no more questions.
PATIENT_NO_MORE_QUESTIONS_KEYWORDS = [
    "no more questions", "no other questions", "no further questions",
    "don't have any more questions", "don't have any other questions",
    "i have no more", "i have no other questions",
    "that's all i needed", "that covers everything", "covers it for me",
    "i think that covers", "i'm all set", "i think i'm good now",
    "nothing else to ask", "nothing more to ask",
    "answered all my questions", "all my questions have been",
    "no, i'm good", "no, that's all", "no, nothing else",
    "i think i have everything i need",
]

def detect_conversation_loops(conversation_history: List[Dict], window_size: int = 6) -> bool:
    """Simple loop detection"""
    if len(conversation_history) < window_size:
        return False
    
    recent_messages = conversation_history[-window_size:]
    
    # Check for repetitive patterns
    doctor_messages = [msg['content'].lower()[:100] for msg in recent_messages if msg['role'].lower() == 'doctor']
    patient_messages = [msg['content'].lower()[:100] for msg in recent_messages if msg['role'].lower() == 'patient']
    
    # Simple repetition check
    if len(doctor_messages) >= 2 and doctor_messages[-1] == doctor_messages[-2]:
        return True
    if len(patient_messages) >= 2 and patient_messages[-1] == patient_messages[-2]:
        return True
    
    # Check for circular questioning
    question_patterns = ['what', 'how', 'when', 'where', 'why', 'can you', 'could you']
    recent_doctor_questions = [
        msg for msg in doctor_messages 
        if any(pattern in msg for pattern in question_patterns)
    ]
    
    # If doctor is repeating similar questions
    if len(recent_doctor_questions) >= 3:
        return True
    
    return False

def is_valid_agent_response(response_text: str) -> bool:
    """Basic response validation"""
    if not response_text or not response_text.strip():
        logger.warning("Agent returned an empty response.")
        return False
    if "[ERROR:" in response_text:
        logger.error(f"Agent returned an error: {response_text}")
        return False
    if len(response_text.strip()) < 10:
        logger.warning("Agent response too short")
        return False
    return True

def is_conversation_substantial(conversation_history: List[Dict], min_turns: int = 6) -> bool:
    """
    Check if conversation has reached substantial length.
    Reduced minimum from 8 to 6 to allow shorter, focused dialogues.
    """
    return len(conversation_history) >= min_turns

def has_sufficient_symptom_coverage(conversation_history: List[Dict]) -> bool:
    """
    Check if the doctor has gathered enough information to form an assessment.
    Looks for evidence of systematic questioning.
    """
    if len(conversation_history) < 6:
        return False

    # Count doctor questions about symptoms
    doctor_messages = [msg['content'].lower() for msg in conversation_history if msg['role'].lower() == 'doctor']

    # Look for key clinical inquiry patterns
    symptom_questions = 0
    key_patterns = [
        'when', 'where', 'how long', 'describe', 'what does', 'scale',
        'worse', 'better', 'trigger', 'happen', 'associated', 'any other'
    ]

    for msg in doctor_messages:
        if any(pattern in msg for pattern in key_patterns):
            symptom_questions += 1

    # Need at least 3-4 substantive questions for adequate coverage
    return symptom_questions >= 3

def simulate_dialogue(doctor_agent, patient_agent, max_turns=30, consecutive_confusion_limit=3, loop_detection_window=6, min_turns=6, profile_type="FULL") -> Tuple[List[Dict], str]:
    """
    Dynamic dialogue simulation with flexible turn limits.

    Dialogues can end naturally based on:
    - FULL profile: Doctor concludes and patient shows understanding (as early as 6 turns)
    - NO_DIAGNOSIS / NO_DIAGNOSIS_NO_TREATMENT: Doctor concludes, then conversation
      continues until the patient explicitly has no more questions
    - max_turns is a safety limit (30), not a target

    Quality assessment is handled entirely by CoachAgent.
    """
    conversation_history = []
    transcript_log = []

    logger.info(f"🎬 Starting dialogue simulation: max_turns={max_turns} (safety limit), min_turns={min_turns}")

    # Doctor initiates
    doctor_message = doctor_agent.respond([])
    time.sleep(0.75)

    if not is_valid_agent_response(doctor_message):
        error_msg = "Doctor: [Simulation ended due to invalid initial response from Doctor Agent]"
        transcript_log.append(error_msg)
        return conversation_history, "\n".join(transcript_log)

    conversation_history.append({"role": "Doctor", "content": doctor_message})
    transcript_log.append(f"Doctor: {doctor_message}")
    logger.info(f"🩺 Turn 0 (Doctor): {doctor_message[:100]}...")

    turn_count = 0
    doctor_has_concluded = False
    patient_confusion_streak = 0
    post_conclusion_exchanges = 0  # Track exchanges after doctor concludes

    while turn_count < max_turns:
        turn_count += 1
        logger.info(f"--- 🔄 Turn {turn_count}/{max_turns} ---")

        # Patient responds
        patient_message = patient_agent.respond(conversation_history)
        time.sleep(0.75)

        if not is_valid_agent_response(patient_message):
            error_msg = f"Patient: [Simulation ended due to invalid response from Patient Agent at turn {turn_count}]"
            transcript_log.append(error_msg)
            break

        conversation_history.append({"role": "Patient", "content": patient_message})
        transcript_log.append(f"Patient: {patient_message}")
        logger.info(f"🤒 Turn {turn_count} (Patient): {patient_message[:100]}...")
        
        # Simple loop detection
        if detect_conversation_loops(conversation_history, loop_detection_window):
            logger.warning(f"⚠️ Conversation loop detected at turn {turn_count}")
            transcript_log.append("[Simulation ended due to detected conversation loop]")
            break

        # Patient response analysis after doctor conclusion
        if doctor_has_concluded:
            post_conclusion_exchanges += 1
            patient_text_lower = patient_message.lower()
            shows_no_more_questions = any(kw in patient_text_lower for kw in PATIENT_NO_MORE_QUESTIONS_KEYWORDS)
            shows_understanding = any(kw in patient_text_lower for kw in PATIENT_UNDERSTANDING_KEYWORDS)
            shows_confusion = any(kw in patient_text_lower for kw in PATIENT_CONFUSION_KEYWORDS)

            if profile_type in ("NO_DIAGNOSIS", "NO_DIAGNOSIS_NO_TREATMENT"):
                # For these profiles the conversation continues until the patient
                # explicitly signals they have no more questions.
                if shows_no_more_questions:
                    logger.info(f"✅ Patient has no more questions at turn {turn_count}")
                    transcript_log.append("[Patient has no more questions — dialogue concluded]")
                    break
                elif shows_confusion:
                    patient_confusion_streak += 1
                    logger.info(f"❓ Patient confused after conclusion T{turn_count} (Streak: {patient_confusion_streak})")
                    if patient_confusion_streak >= consecutive_confusion_limit:
                        logger.warning(f"⚠️ Patient remained confused for {patient_confusion_streak} turns")
                        transcript_log.append("[Simulation ended: Patient remained confused after multiple clarification attempts]")
                        break
                else:
                    patient_confusion_streak = max(0, patient_confusion_streak - 1)
            else:
                # FULL profile: end as soon as patient shows understanding, or
                # after 2 post-conclusion exchanges regardless.
                if shows_understanding and not shows_confusion:
                    logger.info(f"✅ Patient showed understanding of conclusion at turn {turn_count}")
                    transcript_log.append("[Patient showed understanding of Doctor's conclusion]")
                    break
                elif post_conclusion_exchanges >= 2:
                    logger.info(f"✅ Ending after {post_conclusion_exchanges} post-conclusion exchanges")
                    transcript_log.append("[Dialogue concluded naturally]")
                    break
                elif shows_confusion:
                    patient_confusion_streak += 1
                    logger.info(f"❓ Patient confused after conclusion T{turn_count} (Streak: {patient_confusion_streak})")
                    if patient_confusion_streak >= consecutive_confusion_limit:
                        logger.warning(f"⚠️ Patient remained confused for {patient_confusion_streak} turns")
                        transcript_log.append("[Simulation ended: Patient remained confused after multiple clarification attempts]")
                        break
                else:
                    patient_confusion_streak = max(0, patient_confusion_streak - 1)
        
        if turn_count >= max_turns:
             logger.warning(f"⏰ Maximum turns ({max_turns}) reached after patient response")
             transcript_log.append(f"[Maximum turns ({max_turns}) reached]")
             break

        # Doctor responds again
        doctor_message = doctor_agent.respond(conversation_history)
        time.sleep(0.75)

        if not is_valid_agent_response(doctor_message):
            error_msg = f"Doctor: [Simulation ended due to invalid response from Doctor Agent at turn {turn_count}]"
            transcript_log.append(error_msg)
            break

        conversation_history.append({"role": "Doctor", "content": doctor_message})
        transcript_log.append(f"Doctor: {doctor_message}")
        logger.info(f"🩺 Turn {turn_count} (Doctor): {doctor_message[:100]}...")

        # Enhanced conclusion detection
        doctor_text_lower = doctor_message.lower()
        contains_conclusion_keywords = any(keyword in doctor_text_lower for keyword in DOCTOR_CONCLUSION_KEYWORDS)

        # Basic conclusion requirements
        is_substantial_response = len(doctor_message.split()) > 20
        contains_clinical_reasoning = any(phrase in doctor_text_lower for phrase in [
            'diagnosis', 'recommend', 'treatment', 'plan', 'next steps', 'assessment',
            'sounds like', 'appears to be', 'likely'
        ])

        is_genuine_conclusion = (
            contains_conclusion_keywords and
            (is_substantial_response or contains_clinical_reasoning)
        )

        # Allow conclusion if:
        # 1. Minimum turns met (6) AND sufficient symptom coverage
        # 2. OR conversation is substantial (8+) with clinical reasoning
        can_conclude = (
            (is_conversation_substantial(conversation_history, min_turns) and
             has_sufficient_symptom_coverage(conversation_history))
            or
            is_conversation_substantial(conversation_history, min_turns=8)
        )

        # Only allow ONE conclusion - prevent re-concluding
        if not doctor_has_concluded and is_genuine_conclusion and can_conclude:
            doctor_has_concluded = True
            patient_confusion_streak = 0
            post_conclusion_exchanges = 0  # Reset counter when conclusion first detected
            logger.info(f"🎯 Doctor conclusion detected at turn {turn_count} (total messages: {len(conversation_history)})")
        elif doctor_has_concluded and is_genuine_conclusion:
            # Doctor is re-concluding - this is repetitive, end the dialogue
            logger.warning(f"⚠️ Doctor re-concluding (repetitive) - ending dialogue")
            transcript_log.append("[Dialogue ended: Doctor repeated conclusion]")
            break
        
        if turn_count >= max_turns:
             logger.warning(f"⏰ Maximum turns ({max_turns}) reached after doctor response")
             transcript_log.append(f"[Maximum turns ({max_turns}) reached]")
             break

    # Simple completion logging
    full_transcript_string = "\n".join(transcript_log)
    
    logger.info(f"🏁 Dialogue simulation completed!")
    logger.info(f"📈 Basic metrics: Turns={turn_count}, Messages={len(conversation_history)}")
    
    return conversation_history, full_transcript_string


def simulate_dialogue_yield(doctor_agent, patient_agent, max_turns=30, consecutive_confusion_limit=3, loop_detection_window=6, min_turns=6, profile_type="FULL"):
    """
    Dynamic yield-based dialogue simulation with flexible turn limits.

    Dialogues can end naturally based on clinical coverage and natural conclusion.
    For NO_DIAGNOSIS / NO_DIAGNOSIS_NO_TREATMENT profiles, the conversation
    continues after the doctor's assessment until the patient has no more questions.
    max_turns is a safety limit (30), not a target.
    """
    conversation_history = []
    logger.info(f"🎬 Starting yield dialogue simulation: max_turns={max_turns} (safety limit), min_turns={min_turns}")

    # Doctor initiates
    doctor_message_content = doctor_agent.respond([])
    if not is_valid_agent_response(doctor_message_content):
        yield {"role": "Doctor", "content": "[Simulation ended due to invalid initial response from Doctor Agent]"}
        return
        
    turn_info = {"role": "Doctor", "content": doctor_message_content}
    conversation_history.append(turn_info)
    yield turn_info
    logger.info(f"🩺 Yielded Turn 0 (Doctor): {doctor_message_content[:100]}...")

    turn_count = 0
    doctor_has_concluded = False
    patient_confusion_streak = 0
    post_conclusion_exchanges = 0  # Track exchanges after doctor concludes

    while turn_count < max_turns:
        turn_count += 1
        logger.info(f"--- 🔄 Yield Turn {turn_count}/{max_turns} ---")

        # Patient responds
        patient_message_content = patient_agent.respond(conversation_history)
        if not is_valid_agent_response(patient_message_content):
            yield {"role": "Patient", "content": f"[Simulation ended due to invalid response from Patient Agent at turn {turn_count}]"}
            break
        
        turn_info = {"role": "Patient", "content": patient_message_content}
        conversation_history.append(turn_info)
        yield turn_info
        logger.info(f"🤒 Yielded Turn {turn_count} (Patient): {patient_message_content[:100]}...")

        # Simple loop detection
        if detect_conversation_loops(conversation_history, loop_detection_window):
            logger.warning(f"⚠️ Loop in yield simulation at turn {turn_count}")
            yield {"role": "System", "content": "[Simulation ended due to detected conversation loop]"}
            break

        # Patient response analysis (mirrors non-yield version)
        if doctor_has_concluded:
            post_conclusion_exchanges += 1
            patient_text_lower = patient_message_content.lower()
            shows_no_more_questions = any(kw in patient_text_lower for kw in PATIENT_NO_MORE_QUESTIONS_KEYWORDS)
            shows_understanding = any(kw in patient_text_lower for kw in PATIENT_UNDERSTANDING_KEYWORDS)
            shows_confusion = any(kw in patient_text_lower for kw in PATIENT_CONFUSION_KEYWORDS)

            if profile_type in ("NO_DIAGNOSIS", "NO_DIAGNOSIS_NO_TREATMENT"):
                if shows_no_more_questions:
                    logger.info(f"✅ Patient has no more questions (yield) at turn {turn_count}")
                    yield {"role": "System", "content": "[Patient has no more questions — dialogue concluded]"}
                    break
                elif shows_confusion:
                    patient_confusion_streak += 1
                    if patient_confusion_streak >= consecutive_confusion_limit:
                        logger.warning(f"⚠️ Patient confusion streak in yield: {patient_confusion_streak} turns")
                        yield {"role": "System", "content": "[Simulation ended: Patient remained confused after multiple clarification attempts]"}
                        break
                else:
                    patient_confusion_streak = max(0, patient_confusion_streak - 1)
            else:
                # FULL profile: original behaviour
                if shows_understanding and not shows_confusion:
                    logger.info(f"✅ Patient understanding in yield at turn {turn_count}")
                    yield {"role": "System", "content": "[Patient showed understanding of Doctor's conclusion]"}
                    break
                elif post_conclusion_exchanges >= 2:
                    logger.info(f"✅ Yield ending after {post_conclusion_exchanges} post-conclusion exchanges")
                    yield {"role": "System", "content": "[Dialogue concluded naturally]"}
                    break
                elif shows_confusion:
                    patient_confusion_streak += 1
                    if patient_confusion_streak >= consecutive_confusion_limit:
                        logger.warning(f"⚠️ Patient confusion streak in yield: {patient_confusion_streak} turns")
                        yield {"role": "System", "content": "[Simulation ended: Patient remained confused after multiple clarification attempts]"}
                        break
                else:
                    patient_confusion_streak = max(0, patient_confusion_streak - 1)
        
        if turn_count >= max_turns:
             logger.warning(f"⏰ Maximum turns ({max_turns}) reached after patient response (yield)")
             yield {"role": "System", "content": f"[Maximum turns ({max_turns}) reached]"}
             break

        # Doctor responds
        doctor_message_content = doctor_agent.respond(conversation_history)
        if not is_valid_agent_response(doctor_message_content):
            yield {"role": "Doctor", "content": f"[Simulation ended due to invalid response from Doctor Agent at turn {turn_count}]"}
            break
        
        turn_info = {"role": "Doctor", "content": doctor_message_content}
        conversation_history.append(turn_info)
        yield turn_info
        logger.info(f"🩺 Yielded Turn {turn_count} (Doctor): {doctor_message_content[:100]}...")

        # Enhanced conclusion detection (same as non-yield version)
        doctor_text_lower = doctor_message_content.lower()
        contains_conclusion_keywords = any(keyword in doctor_text_lower for keyword in DOCTOR_CONCLUSION_KEYWORDS)

        is_substantial_response = len(doctor_message_content.split()) > 20
        contains_clinical_reasoning = any(phrase in doctor_text_lower for phrase in [
            'diagnosis', 'recommend', 'treatment', 'plan', 'next steps', 'assessment',
            'sounds like', 'appears to be', 'likely'
        ])

        is_genuine_conclusion = contains_conclusion_keywords and (is_substantial_response or contains_clinical_reasoning)

        # Allow conclusion if minimum turns met and sufficient coverage OR conversation is substantial
        can_conclude = (
            (is_conversation_substantial(conversation_history, min_turns) and
             has_sufficient_symptom_coverage(conversation_history))
            or
            is_conversation_substantial(conversation_history, min_turns=8)
        )

        # Only allow ONE conclusion - prevent re-concluding
        if not doctor_has_concluded and is_genuine_conclusion and can_conclude:
            doctor_has_concluded = True
            patient_confusion_streak = 0
            post_conclusion_exchanges = 0  # Reset counter when conclusion first detected
            logger.info(f"🎯 Doctor conclusion in yield at turn {turn_count} (total messages: {len(conversation_history)})")
        elif doctor_has_concluded and is_genuine_conclusion:
            # Doctor is re-concluding - this is repetitive, end the dialogue
            logger.warning(f"⚠️ Doctor re-concluding (repetitive) in yield - ending dialogue")
            yield {"role": "System", "content": "[Dialogue ended: Doctor repeated conclusion]"}
            break
        
        if turn_count >= max_turns:
            logger.warning(f"⏰ Maximum turns ({max_turns}) reached after doctor response (yield)")
            yield {"role": "System", "content": f"[Maximum turns ({max_turns}) reached]"}
            break
            
    logger.info(f"🏁 Yield simulation completed - {len(conversation_history)} messages exchanged")