import json
from pathlib import Path

import pytest

from decision_controller.model import Model, decode, request_body


@pytest.mark.parametrize("content", [
    '{"A":1,"A":0,"B":0}', '{"A":true,"B":0}', '{"A":NaN,"B":0}',
    '{"A":1}', '{"A":1,"B":0,"C":0}', '{"A":0,"B":0}',
    '{"A":-1,"B":1}', '{"A":2,"B":0}', '[1,0]', '```json\n{"A":1,"B":0}\n```',
])
def test_reject_invalid_probabilities(content: str) -> None:
    with pytest.raises(ValueError):
        decode(content, ["A", "B"])


def test_normalization_preserves_model_ranking() -> None:
    probabilities, total = decode('{"A":0.225,"B":0.675}', ["A", "B"])
    assert total == pytest.approx(.9)
    assert probabilities == pytest.approx({"A":.25,"B":.75})


def test_endpoint_request_and_output(tmp_path: Path) -> None:
    options = [{"label":"A", "id":"continue", "description":"Advance dialogue"},
               {"label":"B", "id":"wait", "description":"Wait"}]
    body = request_body({"mode":"dialog"}, options, "test-model")
    assert body["chat_template_kwargs"] == {"enable_thinking":False}
    assert [message["role"] for message in body["messages"]] == ["user"]
    assert "A. continue: Advance dialogue" in body["messages"][0]["content"]
    class Response:
        status_code = 200
        text = json.dumps({"choices":[{"finish_reason":"stop", "message":{"content":'{"A":0.9,"B":0.1}'}}]})
        def raise_for_status(self) -> None:
            pass
        def json(self) -> dict:
            return json.loads(self.text)
    class Session:
        def post(self, endpoint: str, **kwargs: object) -> Response:
            assert endpoint == "http://test/v1/chat/completions"
            assert kwargs["json"] == body
            return Response()
    model = Model("http://test/v1/chat/completions", "test-model", tmp_path / "calls.jsonl")
    model.session = Session()
    result = model.decide({"mode":"dialog"}, options)
    assert result["choice_id"] == "continue"
    assert result["latency_ms"] >= 0
    logged = json.loads((tmp_path / "calls.jsonl").read_text())
    assert logged["raw_response"] == Response.text
    assert "authorization" not in logged
