"""The secret scanner is a security control, so it gets tested like one.

Two failure modes matter equally: missing a real credential, and crying wolf
often enough that someone disables the hook.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCANNER = Path(__file__).resolve().parents[2] / "tools" / "scan_secrets.py"
_spec = importlib.util.spec_from_file_location("scan_secrets", _SCANNER)
scan_secrets = importlib.util.module_from_spec(_spec)
# Register before executing: @dataclass resolves annotations through
# sys.modules[cls.__module__], which is None for an unregistered module.
sys.modules["scan_secrets"] = scan_secrets
_spec.loader.exec_module(scan_secrets)

scan_text = scan_secrets.scan_text


def ids(findings):
    return {f.rule_id for f in findings}


# --------------------------------------------------------------------- #
# it catches the things that would actually hurt                        #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "content,expected",
    [
        ('KEY = "rzp_live_9xKq2mNpLd8Fga"', "razorpay.live_key"),  # noqa: secret
        ('KEY = "rzp_test_7bHq1mZpKd4Fgb"', "razorpay.test_key"),  # noqa: secret
        ('k="sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345"', "anthropic.api_key"),  # noqa: secret
        ('aws = "AKIAIOSFODNN7EXAMPLE"', "aws.access_key"),  # noqa: secret
        ('gh = "ghp_16CharsMinimumxxxxxxxxxxxxxxxxxxxxxx"', "github.token"),  # noqa: secret
        ("-----BEGIN RSA PRIVATE KEY-----", "generic.private_key"),  # noqa: secret
        ('WEBHOOK_SECRET = "Zq8Lm3Rt9Xv2Bn6Kp4Wd7Yc1"', "generic.assigned_secret"),  # noqa: secret
    ],
)
def test_catches_real_credentials(content, expected):
    assert expected in ids(scan_text("backend/app.py", content))


def test_findings_are_redacted_in_output():
    """The scanner must not print the secret it just found."""
    secret = "rzp_live_9xKq2mNpLd8Fga"  # noqa: secret
    finding = scan_text("app.py", f'k = "{secret}"')[0]
    assert secret not in finding.excerpt
    assert finding.excerpt.startswith("rzp_")
    assert "*" in finding.excerpt


def test_scans_nested_and_multiline_content():
    text = 'ok = 1\n\nconfig = {\n  "token": "Zq8Lm3Rt9Xv2Bn6Kp4Wd7Yc1"\n}\n'  # noqa: secret
    findings = scan_text("app.py", text)
    assert findings and findings[0].line_no == 4


# --------------------------------------------------------------------- #
# it stays quiet on the things that are fine                            #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "content",
    [
        'AUTHENTICATION_DROPOFF = "authentication_dropoff"',   # enum member
        'ISSUER_DECLINE_HARD = "issuer_decline_hard"',
        "RAZORPAY_KEY_SECRET=",                                 # empty in .env.example
        'api_key = os.environ["ANTHROPIC_API_KEY"]',            # read from env
        'token = "${GITHUB_TOKEN}"',                            # interpolated
        'password = "changeme"',
        'secret = "your-secret-here"',
        'DATABASE_URL=postgresql+psycopg://counterfoil:counterfoil@db:5432/counterfoil',
    ],
)
def test_does_not_cry_wolf(content):
    assert scan_text("backend/app.py", content) == []


def test_documented_placeholders_are_allowed():
    assert scan_text(".env.example", "RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxxxx") == []


def test_noqa_escape_hatch():
    line = 'k = "rzp_test_7bHq1mZpKd4Fgb"  # noqa: secret'
    assert scan_text("docs/example.md", line) == []


def test_the_scanner_does_not_flag_its_own_patterns():
    assert scan_text("tools/scan_secrets.py", _SCANNER.read_text(encoding="utf-8")) == []


# --------------------------------------------------------------------- #
# PII rules are scoped to data files, not prose                         #
# --------------------------------------------------------------------- #


def test_pii_is_flagged_in_data_files():
    row = '{"phone": "9876543210", "email": "arjun@example.com"}'
    found = ids(scan_text("llm_fixtures/batch.jsonl", row))
    assert "pii.phone_in" in found
    assert "pii.email_in" in found


def test_pii_rules_do_not_fire_on_source_or_prose():
    assert scan_text("README.md", "Reach the team at hello@merchant.co.in") == []
    assert scan_text("backend/app.py", '_PHONE = "9876543210"') == []


def test_card_numbers_in_data_files_are_caught():
    assert "pii.card_in" in ids(scan_text("data/seed.json", '{"card": "4111111111111111"}'))
