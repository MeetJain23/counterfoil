import os

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


# --------------------------------------------------------------------- #
# .env loading                                                          #
# --------------------------------------------------------------------- #


def test_dotenv_is_read(tmp_path, monkeypatch):
    from counterfoil.config import load_dotenv

    env = tmp_path / ".env"
    env.write_text(
        '# a comment\n'
        'ANTHROPIC_API_KEY="sk-ant-from-file"\n'
        "RAZORPAY_KEY_ID='" + TEST_KEY + "'\n"
        "\n"
        "MALFORMED_LINE_NO_EQUALS\n",
        encoding="utf-8",
    )
    assert load_dotenv(env) == 2
    s = load_settings()
    assert s.anthropic_api_key == "sk-ant-from-file"
    assert s.razorpay_key_id == TEST_KEY


def test_a_real_env_var_beats_the_file(tmp_path, monkeypatch):
    from counterfoil.config import load_dotenv

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-shell")
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-ant-from-file\n", encoding="utf-8")
    load_dotenv(env)
    assert load_settings().anthropic_api_key == "sk-ant-from-shell"


def test_a_missing_dotenv_is_not_an_error(tmp_path):
    from counterfoil.config import load_dotenv

    assert load_dotenv(tmp_path / "nope.env") == 0


def test_dotenv_values_are_literal(tmp_path):
    """No shell expansion, no substitution, nothing that could execute."""
    from counterfoil.config import load_dotenv

    env = tmp_path / ".env"
    env.write_text("COUNTERFOIL_LLM_MODEL=$(whoami)\n", encoding="utf-8")
    load_dotenv(env, override=True)
    assert os.environ["COUNTERFOIL_LLM_MODEL"] == "$(whoami)"
