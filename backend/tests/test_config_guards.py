import pytest

from counterfoil.config import UnsafeConfigError, load_settings

TEST_KEY = "rzp_test_FAKEKEYFORTESTS"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "RAZORPAY_KEY_ID",
        "RAZORPAY_KEY_SECRET",
        "RAZORPAY_WEBHOOK_SECRET",
        "ANTHROPIC_API_KEY",
        "COUNTERFOIL_ENV",
        "COUNTERFOIL_DRY_RUN",
        "COUNTERFOIL_LLM_MODE",
        "COUNTERFOIL_SPEND_CAP_USD",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_safe():
    s = load_settings()
    assert s.dry_run is True
    assert s.llm_mode == "replay"
    assert s.can_call_llm is False


def test_a_live_key_refuses_to_boot(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_ABCDEFGHIJKL")
    with pytest.raises(UnsafeConfigError, match="LIVE key"):
        load_settings()


def test_an_unrecognised_key_refuses_to_boot(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "sk_something_else")
    with pytest.raises(UnsafeConfigError, match="rzp_test_"):
        load_settings()


def test_a_test_key_is_accepted(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", TEST_KEY)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "shhh")
    assert load_settings().has_razorpay is True


def test_there_is_no_production_mode(monkeypatch):
    monkeypatch.setenv("COUNTERFOIL_ENV", "prod")
    with pytest.raises(UnsafeConfigError, match="no production mode"):
        load_settings()


def test_spend_cap_must_be_positive(monkeypatch):
    monkeypatch.setenv("COUNTERFOIL_SPEND_CAP_USD", "0")
    with pytest.raises(UnsafeConfigError, match="must be positive"):
        load_settings()


def test_unknown_llm_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("COUNTERFOIL_LLM_MODE", "yolo")
    with pytest.raises(UnsafeConfigError, match="replay|record|live"):
        load_settings()


def test_live_llm_needs_an_actual_key(monkeypatch):
    monkeypatch.setenv("COUNTERFOIL_LLM_MODE", "live")
    assert load_settings().can_call_llm is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    assert load_settings().can_call_llm is True
