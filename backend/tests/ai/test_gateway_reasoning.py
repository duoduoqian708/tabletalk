"""gateway 思考强度 → reasoning_effort 映射测试。"""
from app.ai.gateway import LLMGateway


def _payload(reasoning):
    gw = LLMGateway({
        "provider": "cloud", "model": "gpt-5",
        "base_url": "https://api.example.com/v1", "api_key": "k",
        "reasoning": reasoning,
    })
    _, payload, _ = gw._request([], None, False)
    return payload


def test_low_maps_to_effort_low():
    assert _payload("low").get("reasoning_effort") == "low"


def test_medium_maps_to_effort_medium():
    assert _payload("medium").get("reasoning_effort") == "medium"


def test_high_maps_to_effort_high():
    assert _payload("high").get("reasoning_effort") == "high"


def test_off_no_effort_param():
    assert "reasoning_effort" not in _payload("off")


def test_none_no_effort_param():
    assert "reasoning_effort" not in _payload(None)


def test_mock_never_sends_effort():
    gw = LLMGateway({"provider": "mock", "model": "mock", "reasoning": "high"})
    _, payload, _ = gw._request([], None, False)
    assert "reasoning_effort" not in payload


def test_bool_false_no_param():
    assert "reasoning_effort" not in _payload(False)


def test_bool_true_maps_to_high():
    assert _payload(True).get("reasoning_effort") == "high"
