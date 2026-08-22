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
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)

    with pytest.raises(LLMError, match="400"):
        GeminiClient(api_key="k", transport=refused, max_retries=0).ask(ASK)


# --------------------------------------------------------------------- #
# rate limiting (see FAILURES.md 005)                                   #
# --------------------------------------------------------------------- #


def http_error(code, body=b""):
    def raiser(url, request_body, timeout):
        err = urllib.error.HTTPError(url, code, "err", {}, None)
        err.read = lambda: body
        raise err

    return raiser


def flaky(fail_times, code=429, body=b""):
    """Fails a few times, then succeeds. What a rate limit actually looks like."""
    state = {"n": 0}

    def transport(url, request_body, timeout):
        state["n"] += 1
        if state["n"] <= fail_times:
            err = urllib.error.HTTPError(url, code, "err", {}, None)
            err.read = lambda: body
            raise err
        return ok_payload()

    return transport


def recorder():
    slept = []
    return slept, slept.append


def test_a_rate_limit_is_retried_rather_than_surfaced():
    slept, sleep = recorder()
    client = GeminiClient(api_key="k", transport=flaky(2), sleep=sleep)
    assert client.ask(ASK).data == ANSWER
    assert len(slept) == 2


def test_backoff_grows_between_attempts():
    slept, sleep = recorder()
    GeminiClient(api_key="k", transport=flaky(3), sleep=sleep).ask(ASK)
    assert slept == sorted(slept)
    assert slept[-1] > slept[0]


def test_the_providers_own_retry_delay_is_obeyed_over_our_guess():
    body = b'{"error":{"code":429},"details":[{"retryDelay":"27s"}]}'
    slept, sleep = recorder()
    GeminiClient(api_key="k", transport=flaky(1, body=body), sleep=sleep).ask(ASK)
    assert slept == [27.0]


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_transient_failures_are_retried(code):
    slept, sleep = recorder()
    client = GeminiClient(api_key="k", transport=flaky(1, code=code), sleep=sleep)
    assert client.ask(ASK).data == ANSWER


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_permanent_failures_are_not_retried(code):
    slept, sleep = recorder()
    with pytest.raises(LLMError):
        GeminiClient(api_key="k", transport=http_error(code), sleep=sleep).ask(ASK)
    assert slept == []


def test_retries_give_up_eventually():
    slept, sleep = recorder()
    client = GeminiClient(api_key="k", transport=http_error(429), sleep=sleep, max_retries=3)
    with pytest.raises(LLMError, match="429"):
        client.ask(ASK)
    assert len(slept) == 3


def test_pacing_waits_between_calls():
    """Free tier caps requests per minute, so a batch must pace itself."""
    slept, sleep = recorder()
    now = {"t": 0.0}
    client = GeminiClient(
        api_key="k",
        transport=canned(ok_payload()),
        sleep=sleep,
        clock=lambda: now["t"],
        min_interval_seconds=6.0,
    )
    client.ask(ASK)          # first call does not wait
    client.ask(ASK)          # second is immediate, so it waits the full interval
    assert slept == [6.0]


def test_pacing_does_not_wait_when_enough_time_already_passed():
    slept, sleep = recorder()
    now = {"t": 0.0}
    client = GeminiClient(
        api_key="k",
        transport=canned(ok_payload()),
        sleep=sleep,
        clock=lambda: now["t"],
        min_interval_seconds=6.0,
    )
    client.ask(ASK)
    now["t"] = 30.0
    client.ask(ASK)
    assert slept == []


def test_pacing_is_off_by_default():
    slept, sleep = recorder()
    client = GeminiClient(api_key="k", transport=canned(ok_payload()), sleep=sleep)
    client.ask(ASK)
    client.ask(ASK)
    assert slept == []


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


# --------------------------------------------------------------------- #
# model retirement (see FAILURES.md 004)                                #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "message,expected",
    [
        (
            'This model models/gemini-2.5-flash is no longer available to new '
            'users. Please update your code to use models/gemini-3.6-flash for '
            'the latest features and improvements.',
            "gemini-3.6-flash",
        ),
        ("please use models/gemini-3-pro instead", "gemini-3-pro"),
        ("404 not found", None),
        ("", None),
    ],
)
def test_a_retirement_notice_yields_an_actionable_model_name(message, expected):
    from counterfoil.llm.gemini_client import suggested_replacement

    assert suggested_replacement(message) == expected


def test_a_retired_model_degrades_with_the_replacement_still_readable(tmp_path):  # noqa: E501
    """The 404 body carries the fix; it has to survive into the rationale."""
    import json as _json

    from counterfoil.domain.diagnosis import DiagnosisPath
    from counterfoil.kernel.diagnose.llm import LLMDiagnoser
    from counterfoil.llm import Budget, FixtureStore
    from counterfoil.llm.gemini_client import suggested_replacement
    from test_llm_diagnoser import event

    body = _json.dumps({
        "error": {
            "code": 404,
            "message": "This model models/gemini-2.5-flash is no longer available "
                       "to new users. Please update your code to use "
                       "models/gemini-3.6-flash for the latest features.",
            "status": "NOT_FOUND",
        }
    }).encode()

    class _Body:
        def read(self):
            return body

    def retired(url, body_, timeout):
        err = urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        err.read = _Body().read
        raise err

    dx = LLMDiagnoser(
        client=GeminiClient(api_key="k", model="gemini-2.5-flash", transport=retired),
        fixtures=FixtureStore(tmp_path / "fx", mode="live"),
        budget=Budget(cap_usd=1.0),
    )
    result = dx(event())

    assert result.path is DiagnosisPath.DEGRADED
    assert suggested_replacement(result.rationale) == "gemini-3.6-flash"
