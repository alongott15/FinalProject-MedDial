"""Extraction must not invent a reference, and its grounding must resolve.

Two failures found by running the real extractor against a local model rather
than a mock:

* every chunk failing to parse produced an *empty* GTMF and logged an error.
  Downstream, an empty reference is indistinguishable from a genuinely sparse
  case, so a note that was never read counted as a successful extraction --
  the D-08 shape the Azure removal closed elsewhere in the pipeline.
* the model quoted evidence accurately and computed its character offsets
  wrongly, every time, so 100% of entities failed EvidenceSpan validation and
  the reference was ungrounded.

Everything here is synthetic and every provider is a MockProvider.
"""

from __future__ import annotations

import json

import pytest

from gtmf_creation import (
    ExtractionError,
    _repair_evidence_offsets,
    extract_gtmf_chunked,
)
from meddial.llm import MockProvider

NOTE = "Chief Complaint: Sore throat for four days.\nDischarge Diagnosis: Acute pharyngitis."


def _payload(*, char_start: int, char_end: int, note_id: str = "") -> str:
    """One symptom whose quote is right and whose offsets are the caller's problem."""
    return json.dumps(
        {
            "Core_Fields": {
                "Symptoms": [
                    {
                        "description": "sore throat",
                        "evidence": [
                            {
                                "note_id": note_id,
                                "char_start": char_start,
                                "char_end": char_end,
                                "text": "Sore throat",
                            }
                        ],
                    }
                ],
                "Diagnoses": [],
                "Treatment_Options": [],
            },
            "Context_Fields": {},
        }
    )


# -- a note that was not read must not produce a profile --------------------


def test_a_note_no_chunk_could_parse_raises_instead_of_an_empty_reference() -> None:
    provider = MockProvider(["not json at all"])

    with pytest.raises(ExtractionError, match="parseable"):
        extract_gtmf_chunked(NOTE, provider, note_id="s1")


def test_the_refusal_names_the_note_and_the_usual_cause() -> None:
    """The operator needs to know which note, and what to change."""
    provider = MockProvider(["{ truncated"])

    with pytest.raises(ExtractionError) as excinfo:
        extract_gtmf_chunked(NOTE, provider, note_id="case-42")

    message = str(excinfo.value)
    assert "case-42" in message
    assert "max_tokens" in message


# -- evidence offsets are derived from the quote, not trusted ---------------


def test_wrong_offsets_are_relocated_from_the_quoted_text() -> None:
    provider = MockProvider([_payload(char_start=999, char_end=1010)])

    reference = extract_gtmf_chunked(NOTE, provider, note_id="s1")

    span = reference.core.symptoms[0].evidence[0]
    assert span.char_start == NOTE.find("Sore throat")
    assert span.resolves_against(NOTE)
    assert reference.evidence_issues({"s1": NOTE}) == {}


def test_the_note_id_is_stamped_even_when_the_model_leaves_it_blank() -> None:
    provider = MockProvider([_payload(char_start=0, char_end=1)])

    reference = extract_gtmf_chunked(NOTE, provider, note_id="s1")

    assert reference.core.symptoms[0].evidence[0].note_id == "s1"


def test_a_quote_absent_from_the_note_is_left_flagged_not_relocated() -> None:
    """A fabricated citation must keep failing validation."""
    payload = {
        "Core_Fields": {
            "Symptoms": [
                {
                    "description": "invented",
                    "evidence": [
                        {
                            "note_id": "s1",
                            "char_start": 0,
                            "char_end": 11,
                            "text": "no such phrase in the note",
                        }
                    ],
                }
            ],
            "Diagnoses": [],
            "Treatment_Options": [],
        },
        "Context_Fields": {},
    }
    provider = MockProvider([json.dumps(payload)])

    reference = extract_gtmf_chunked(NOTE, provider, note_id="s1")

    assert reference.evidence_issues({"s1": NOTE}), "a fabricated quote must stay flagged"


def test_repair_reports_how_many_spans_it_moved() -> None:
    node = {
        "Symptoms": [
            {"evidence": [{"text": "Sore throat", "char_start": 0, "char_end": 1}]},
            {"evidence": [{"text": "Acute pharyngitis", "char_start": 0, "char_end": 1}]},
            {"evidence": [{"text": "absent", "char_start": 0, "char_end": 1}]},
        ]
    }

    assert _repair_evidence_offsets(node, "s1", NOTE) == 2
