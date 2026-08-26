import pytest

from meddial.llm import (
    AzureAIFoundryProvider,
    ChatMessage,
    DataBoundaryError,
    DataClassification,
    LocalOpenAICompatibleProvider,
    MockLLMProvider,
    ProviderBoundary,
    ensure_provider_compatible,
)


def test_provider_records_model_metadata_per_call():
    provider = MockLLMProvider(["response"], model_name="fixture-model")
    response = provider.generate([ChatMessage(role="user", content="hello")])
    assert response.metadata.provider == "mock"
    assert response.metadata.model == "fixture-model"
    assert provider.call_history[0].to_dict()["model"] == "fixture-model"


def test_restricted_clinical_data_rejects_external_provider():
    provider = MockLLMProvider(["unused"])
    provider.provider_boundary = ProviderBoundary.EXTERNAL_SERVICE
    with pytest.raises(DataBoundaryError):
        ensure_provider_compatible(provider, DataClassification.RESTRICTED_CLINICAL)


def test_local_provider_rejects_unapproved_public_host():
    with pytest.raises(DataBoundaryError):
        LocalOpenAICompatibleProvider("model", "https://models.example.com/v1")


def test_local_provider_accepts_loopback_without_network_call():
    provider = LocalOpenAICompatibleProvider("model", "http://127.0.0.1:8000/v1")
    assert provider.provider_boundary is ProviderBoundary.LOCAL_CONTROLLED


def test_azure_rejects_restricted_clinical_before_sdk_or_network_access():
    with pytest.raises(DataBoundaryError):
        AzureAIFoundryProvider(data_classification=DataClassification.RESTRICTED_CLINICAL)
