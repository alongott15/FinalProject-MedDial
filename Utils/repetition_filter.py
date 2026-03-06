import logging
import re
from typing import List, Dict
from collections import Counter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RepetitionTracker:
    """Tracks phrase usage and prevents excessive repetition"""

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self.phrase_counts = Counter()
        self.recent_openings = []  # Track last 5 sentence openings
        self.max_opening_history = 5

        # Define problematic patterns to detect
        self.problematic_patterns = {
            'doctor': [
                r'^thank you for (sharing|letting me know|telling me)',
                r'^i understand',
                r"^i'm sorry",
                r'^i appreciate',
                r'^that sounds',
            ],
            'patient': [
                r'^um\.{3}',
                r'^well\.{3}',
                r'^uh\.{3}',
                r'should i be worried',
                r'is this (something )?serious',
                r'is that normal',
            ]
        }

    def extract_opening_phrase(self, text: str) -> str:
        """Extract the opening 3-5 words of a response"""
        # Remove markdown formatting
        clean_text = re.sub(r'\*\*.*?\*\*', '', text)
        # Get first sentence
        first_sentence = clean_text.split('.')[0].strip()
        # Get first few words
        words = first_sentence.lower().split()[:5]
        return ' '.join(words)

    def is_repetitive_opening(self, response: str) -> bool:
        """Check if the response opening is too similar to recent responses"""
        opening = self.extract_opening_phrase(response)

        if not opening:
            return False

        # Check against recent openings
        similarity_count = sum(1 for recent in self.recent_openings
                              if self._calculate_similarity(opening, recent) > 0.7)

        return similarity_count >= 2  # If similar to 2+ recent responses

    def _calculate_similarity(self, phrase1: str, phrase2: str) -> float:
        """Simple word overlap similarity"""
        words1 = set(phrase1.split())
        words2 = set(phrase2.split())

        if not words1 or not words2:
            return 0.0

        intersection = words1.intersection(words2)
        union = words1.union(words2)

        return len(intersection) / len(union) if union else 0.0

    def track_response(self, response: str):
        """Track this response for future repetition detection"""
        opening = self.extract_opening_phrase(response)

        # Update recent openings
        self.recent_openings.append(opening)
        if len(self.recent_openings) > self.max_opening_history:
            self.recent_openings.pop(0)

        # Track phrase patterns
        agent_type = 'doctor' if 'doctor' in self.agent_name.lower() else 'patient'
        for pattern in self.problematic_patterns.get(agent_type, []):
            if re.search(pattern, response.lower()):
                self.phrase_counts[pattern] += 1

    def get_repetition_warning(self, response: str) -> str:
        """Generate a warning message if response is repetitive"""
        warnings = []

        # Check opening repetition
        if self.is_repetitive_opening(response):
            warnings.append("⚠️ You're starting responses similarly to recent turns - vary your opening")

        # Check problematic phrases
        agent_type = 'doctor' if 'doctor' in self.agent_name.lower() else 'patient'
        for pattern in self.problematic_patterns.get(agent_type, []):
            if re.search(pattern, response.lower()):
                count = self.phrase_counts[pattern]
                if count >= 3:  # Used 3+ times already
                    warnings.append(f"⚠️ You've used this phrase pattern {count} times - find alternative phrasing")

        return ' '.join(warnings) if warnings else None

    def get_usage_stats(self) -> Dict:
        """Get statistics on phrase usage"""
        return {
            'total_tracked_responses': len(self.recent_openings),
            'phrase_counts': dict(self.phrase_counts),
            'recent_openings': self.recent_openings
        }


def detect_symptom_repetition(conversation_history: List[Dict]) -> List[str]:
    """Detect if the same symptoms are being repeated unnecessarily"""
    symptom_keywords = [
        'pain', 'headache', 'fever', 'cough', 'nausea', 'dizziness',
        'fatigue', 'weakness', 'shortness of breath', 'chest pain'
    ]

    # Track how many times each symptom is mentioned
    symptom_mentions = Counter()

    for msg in conversation_history[-6:]:  # Last 6 messages
        content_lower = msg['content'].lower()
        for symptom in symptom_keywords:
            if symptom in content_lower:
                symptom_mentions[symptom] += 1

    # Find symptoms mentioned 3+ times recently
    overmentioned = [symptom for symptom, count in symptom_mentions.items() if count >= 3]

    return overmentioned
