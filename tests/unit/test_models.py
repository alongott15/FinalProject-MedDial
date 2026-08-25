from Models.classes import CoreFields, Medication, StructuredClinicalReference


def test_pydantic_v2_mutable_defaults_are_isolated():
    first = CoreFields()
    second = CoreFields()
    first.Symptoms.append({"description": "cough"})
    assert second.Symptoms == []

    first_med = Medication(name="a")
    second_med = Medication(name="b")
    first_med.evidence.append({"source_note_id": "one"})
    assert second_med.evidence == []


def test_scr_accepts_backward_compatible_minimal_payload():
    reference = StructuredClinicalReference()
    assert reference.extraction_status == "VALID"
    assert reference.schema_name == "Structured Clinical Reference"
