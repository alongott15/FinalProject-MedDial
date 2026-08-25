from meddial.llm import ChatMessage, MockLLMProvider


def test_provider_records_model_metadata_per_call():
    provider = MockLLMProvider(["response"], model_name="fixture-model")
    response = provider.generate([ChatMessage(role="user", content="hello")])
    assert response.metadata.provider == "mock"
    assert response.metadata.model == "fixture-model"
    assert provider.call_history[0].to_dict()["model"] == "fixture-model"
