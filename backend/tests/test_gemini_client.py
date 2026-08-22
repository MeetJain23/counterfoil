"""Gemini client tests, run entirely without a network or a key.

The transport is injected, so the schema translation, the response parsing and
every error path are exercised against canned payloads. What these cannot
cover is whether Google's API still looks like this, which is what
`tools/check_llm.py` is for.
"""

import json
import urllib.error

import pytest

from counterfoil.kernel.diagnose.llm import RESPONSE_SCHEMA
from counterfoil.llm import Ask, GeminiClient, LLMError, to_gemini_schema

ASK = Ask(system="classify things", user="<provider_payload>x</provider_payload>", schema=RESPONSE_SCHEMA)

ANSWER = {
    "cause": "bank_downtime",
    "confidence": 0.77,
    "rationale": "the description blames the bank",
    "key_evidence": ["unable to reach the issuing bank"],
}


def canned(payload, capture=None):
    def transport(url, body, timeout):
        if capture is not None:
            capture["url"] = url
            capture["body"] = body
            capture["timeout"] = timeout
        return payload

    return transport


def ok_payload(data=None, finish="STOP"):
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": json.dumps(data or ANSWER)}]},
                "finishReason": finish,
            }
        ],
        "usageMetadata": {"promptTokenCount": 812, "candidatesTokenCount": 64},
    }


# --------------------------------------------------------------------- #
# schema translation                                                    #
# --------------------------------------------------------------------- #


def test_types_are_uppercased():
    out = to_gemini_schema(RESPONSE_SCHEMA)
    assert out["type"] == "OBJECT"
    assert out["properties"]["cause"]["type"] == "STRING"
    assert out["properties"]["confidence"]["type"] == "NUMBER"
    assert out["properties"]["key_evidence"]["type"] == "ARRAY"
    assert out["properties"]["key_evidence"]["items"]["type"] == "STRING"


def test_unsupported_keys_are_dropped():
    """Gemini rejects additionalProperties outright rather than ignoring it."""
    assert "additionalProperties" not in to_gemini_schema(RESPONSE_SCHEMA)


def test_enum_and_required_survive_translation():
    out = to_gemini_schema(RESPONSE_SCHEMA)
    assert "bank_downtime" in out["properties"]["cause"]["enum"]
    assert "unknown" in out["properties"]["cause"]["enum"]
    assert set(out["required"]) == {"cause", "confidence", "rationale", "key_evidence"}


def test_property_ordering_is_supplied():
    out = to_gemini_schema(RESPONSE_SCHEMA)
    assert out["propertyOrdering"] == list(RESPONSE_SCHEMA["properties"])


def test_translation_is_recursive_and_does_not_mutate_the_input():
    before = json.dumps(RESPONSE_SCHEMA, sort_keys=True)
    to_gemini_schema(RESPONSE_SCHEMA)
    assert json.dumps(RESPONSE_SCHEMA, sort_keys=True) == before


# --------------------------------------------------------------------- #
# the request we actually send                                          #
# --------------------------------------------------------------------- #


def test_the_request_is_shaped_the_way_gemini_expects():
    seen = {}
    client = GeminiClient(api_key="k-test", transport=canned(ok_payload(), seen))
    client.ask(ASK)

    body = seen["body"]
    assert body["systemInstruction"]["parts"][0]["text"] == ASK.system
    assert body["contents"][0]["role"] == "user"
    assert body["contents"][0]["parts"][0]["text"] == ASK.user
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["temperature"] == 0.0


def test_classification_is_deterministic_by_construction():
    """Temperature zero, because recorded fixtures should mean something."""
    seen = {}
    GeminiClient(api_key="k", transport=canned(ok_payload(), seen)).ask(ASK)
    assert seen["body"]["generationConfig"]["temperature"] == 0.0


def test_the_key_goes_in_the_query_string_not_the_body():
    seen = {}
    GeminiClient(api_key="k-secret", transport=canned(ok_payload(), seen)).ask(ASK)
    assert "key=k-secret" in seen["url"]
    assert "k-secret" not in json.dumps(seen["body"])


# --------------------------------------------------------------------- #
# parsing                                                               #
# --------------------------------------------------------------------- #


def test_a_good_response_parses():
    answer = GeminiClient(api_key="k", transport=canned(ok_payload())).ask(ASK)
    assert answer.data == ANSWER
    assert answer.input_tokens == 812
    assert answer.output_tokens == 64
    assert answer.cost_usd == 0.0        # free tier


@pytest.mark.parametrize(
    "payload,fragment",
    [
        ({"error": {"code": 429, "message": "quota"}}, "returned an error"),
        ({"candidates": []}, "nothing usable"),
        ({"promptFeedback": {"blockReason": "SAFETY"}}, "SAFETY"),
        (ok_payload(finish="MAX_TOKENS"), "stopped early"),
        ({"candidates": [{"content": {"parts": []}}]}, "unexpected"),
    ],
)
def test_bad_responses_raise_llm_error(payload, fragment):
    client = GeminiClient(api_key="k", transport=canned(payload))
    with pytest.raises(LLMError, match=fragment):
        client.ask(ASK)


def test_non_json_content_raises_llm_error():
    payload = {"candidates": [{"content": {"parts": [{"text": "sorry, I cannot"}]}, "finishReason": "STOP"}]}
    with pytest.raises(LLMError, match="not valid JSON"):
        GeminiClient(api_key="k", transport=canned(payload)).ask(ASK)


def test_network_failures_become_llm_errors():
    def dead(url, body, timeout):
        raise urllib.error.URLError("name resolution failed")

    with pytest.raises(LLMError, match="could not reach"):
        GeminiClient(api_key="k", transport=dead).ask(ASK)


def test_http_errors_become_llm_errors():
    def refused(url, body, timeout):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)

    with pytest.raises(LLMError, match="429"):
        GeminiClient(api_key="k", transport=refused).ask(ASK)


# --------------------------------------------------------------------- #
# the diagnoser does not care which provider it got                     #
# --------------------------------------------------------------------- #


def test_the_diagnoser_degrades_on_a_gemini_failure_like_any_other(tmp_path):
    from counterfoil.domain.diagnosis import DiagnosisPath
    from counterfoil.kernel.diagnose.llm import LLMDiagnoser
    from counterfoil.llm import Budget, FixtureStore
    from test_llm_diagnoser import event

    def dead(url, body, timeout):
        raise urllib.error.URLError("offline")

    dx = LLMDiagnoser(
        client=GeminiClient(api_key="k", transport=dead),
        fixtures=FixtureStore(tmp_path / "fx", mode="live"),
        budget=Budget(cap_usd=1.0),
    )
    assert dx(event()).path is DiagnosisPath.DEGRADED


def test_a_gemini_answer_becomes_the_same_diagnosis_as_an_anthropic_one(tmp_path):
    from counterfoil.domain.diagnosis import DiagnosisPath, RootCause
    from counterfoil.kernel.diagnose.llm import LLMDiagnoser
    from counterfoil.llm import Budget, FixtureStore
    from test_llm_diagnoser import event

    dx = LLMDiagnoser(
        client=GeminiClient(api_key="k", transport=canned(ok_payload())),
        fixtures=FixtureStore(tmp_path / "fx", mode="live"),
        budget=Budget(cap_usd=1.0),
    )
    result = dx(event())
    assert result.cause is RootCause.BANK_DOWNTIME
    assert result.path is DiagnosisPath.LLM
    assert result.confidence == pytest.approx(0.77)


def test_fixtures_are_provider_agnostic(tmp_path):
    """A fixture recorded on one provider replays under the other.

    The cache key is the situation, not the model, so switching provider does
    not invalidate recorded evidence.
    """
    from counterfoil.kernel.diagnose.llm import LLMDiagnoser
    from counterfoil.llm import Budget, FixtureStore, ScriptedClient
    from test_llm_diagnoser import event

    recorder = LLMDiagnoser(
        client=GeminiClient(api_key="k", transport=canned(ok_payload())),
        fixtures=FixtureStore(tmp_path / "fx", mode="record"),
        budget=Budget(cap_usd=1.0),
    )
    recorded = recorder(event())

    replayer = LLMDiagnoser(
        client=ScriptedClient(answers=[]),
        fixtures=FixtureStore(tmp_path / "fx", mode="replay"),
        budget=Budget(cap_usd=0.0),
    )
    assert replayer(event()).cause is recorded.cause
