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
    extract_gtmf,
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


def test_an_unparseable_answer_raises_instead_of_an_empty_reference() -> None:
    provider = MockProvider(["not json at all"])

    with pytest.raises(ExtractionError, match="parseable"):
        extract_gtmf(NOTE, provider, note_id="s1")


def test_a_balanced_but_malformed_answer_raises_too() -> None:
    """The failure that produced empty references in a real run.

    ``{ truncated`` never balances, so it was refused. A response that *does*
    balance but does not parse -- a missing comma, an unescaped quote, what a
    small model emits constantly -- was answered with a fabricated skeleton:
    right keys, empty lists. That is truthy, so the refusal never fired and the
    note was written as a reference with nothing in it.
    """
    provider = MockProvider(['{"Core_Fields": {"Symptoms": [{"a": "b" "c": "d"}]}}'])

    with pytest.raises(ExtractionError, match="parseable"):
        extract_gtmf(NOTE, provider, note_id="s1")


def test_an_empty_but_parseable_extraction_is_reported(caplog) -> None:
    """It parsed, so it is not an ExtractionError -- but it must not pass quietly.

    An all-empty core is indistinguishable from a sparse case once written, so
    the case that produced it is named while the run is still in front of you.
    """
    payload = {
        "Core_Fields": {"Symptoms": [], "Diagnoses": [], "Treatment_Options": []},
        "Context_Fields": {},
    }
    provider = MockProvider([json.dumps(payload)])

    with caplog.at_level("WARNING"):
        reference = extract_gtmf(NOTE, provider, note_id="case-7")

    assert reference.core.symptoms == []
    assert "case-7" in caplog.text
    assert "no symptoms, diagnoses or treatments" in caplog.text


def test_the_refusal_names_the_note_and_the_usual_cause() -> None:
    """The operator needs to know which note, and what to change."""
    provider = MockProvider(["{ truncated"])

    with pytest.raises(ExtractionError) as excinfo:
        extract_gtmf(NOTE, provider, note_id="case-42")

    message = str(excinfo.value)
    assert "case-42" in message
    assert "max_tokens" in message


# -- evidence offsets are derived from the quote, not trusted ---------------


def test_wrong_offsets_are_relocated_from_the_quoted_text() -> None:
    provider = MockProvider([_payload(char_start=999, char_end=1010)])

    reference = extract_gtmf(NOTE, provider, note_id="s1")

    span = reference.core.symptoms[0].evidence[0]
    assert span.char_start == NOTE.find("Sore throat")
    assert span.resolves_against(NOTE)
    assert reference.evidence_issues({"s1": NOTE}) == {}


def test_the_note_id_is_stamped_even_when_the_model_leaves_it_blank() -> None:
    provider = MockProvider([_payload(char_start=0, char_end=1)])

    reference = extract_gtmf(NOTE, provider, note_id="s1")

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

    reference = extract_gtmf(NOTE, provider, note_id="s1")

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


# -- the two failure modes a 200-case run actually produced ------------------


WRAPPED_NOTE = (
    "Discharge Medications:\n"
    "1. Keflex 500 mg Capsule Sig: One (1) Capsule PO four times a\n"
    "day for 10 days.\n"
)


def test_a_quote_the_note_hard_wraps_still_resolves() -> None:
    """The single commonest reason a genuine quote failed to resolve.

    MIMIC notes wrap mid-sentence. A model quoting across the wrap writes the
    space rather than the newline, so an otherwise verbatim quote is absent
    from the note by one character, and the entity is reported ungrounded.
    """
    payload = {
        "Core_Fields": {
            "Diagnoses": [],
            "Treatment_Options": [],
            "Symptoms": [
                {
                    "description": "cellulitis",
                    "evidence": [
                        {
                            "note_id": "s1",
                            "char_start": 0,
                            "char_end": 1,
                            "text": "Keflex 500 mg Capsule Sig: One (1) Capsule PO four times a day",
                        }
                    ],
                }
            ],
        },
        "Context_Fields": {},
    }
    provider = MockProvider([json.dumps(payload)])

    reference = extract_gtmf(WRAPPED_NOTE, provider, note_id="s1")

    span = reference.core.symptoms[0].evidence[0]
    assert span.resolves_against(WRAPPED_NOTE)
    assert "\n" in span.text, "the span carries the note's own wording, wrap included"
    assert reference.evidence_issues({"s1": WRAPPED_NOTE}) == {}


def test_a_fabricated_quote_is_still_refused_by_the_flexible_match() -> None:
    """Whitespace tolerance must not become a licence to invent."""
    payload = {
        "Core_Fields": {
            "Symptoms": [
                {
                    "description": "invented",
                    "evidence": [
                        {
                            "note_id": "s1",
                            "char_start": 0,
                            "char_end": 5,
                            "text": "Keflex 500 mg PO twice a day for 10 days",
                        }
                    ],
                }
            ],
        },
        "Context_Fields": {},
    }
    provider = MockProvider([json.dumps(payload)])

    reference = extract_gtmf(WRAPPED_NOTE, provider, note_id="s1")

    assert reference.evidence_issues({"s1": WRAPPED_NOTE}), "a quote not in the note must stay flagged"


def test_a_null_optional_field_does_not_discard_the_extraction() -> None:
    """30 of 92 failures in one run were a null in a field with no content.

    A model that cannot fill an optional descriptive field writes null about as
    often as it omits the key. Discarding the whole reference over that threw
    away extractions that had already located dozens of evidence spans.
    """
    payload = {
        "Core_Fields": {
            "Treatment_Options": [
                {"procedure": "laparoscopic cholecystectomy", "details": None,
                 "treatment": None, "evidence": []}
            ],
            "Symptoms": [{"description": "sore throat", "onset": None, "evidence": []}],
        },
        "Context_Fields": {
            "Discharge_Medications": [{"name": "Keflex", "purpose": None}],
        },
    }
    provider = MockProvider([json.dumps(payload)])

    reference = extract_gtmf(NOTE, provider, note_id="s1")

    assert reference.core.treatments[0].procedure == "laparoscopic cholecystectomy"
    assert reference.core.treatments[0].details == "not provided"
    assert reference.core.symptoms[0].onset == "not provided"
    assert reference.context.discharge_medications[0].purpose == "not provided"
