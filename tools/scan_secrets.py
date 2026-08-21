#!/usr/bin/env python3
"""Dependency-free secret and PII scanner. Runs as a git pre-commit hook.

Why this exists when .pre-commit-config.yaml already wires up gitleaks: that
config only protects a machine where someone has actually run
``pre-commit install``, and gitleaks itself has to be downloaded. This script
needs nothing but the Python that is already here, so the protection is real
from the first commit rather than from whenever the toolchain got set up.

Usage:
    python tools/scan_secrets.py --staged   # what is about to be committed
    python tools/scan_secrets.py --all      # every tracked + untracked file
    python tools/scan_secrets.py --history  # every blob in every commit

Exit code 1 means: do not commit this.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Files whose whole job is to describe secret shapes. Scanning them finds only
# the patterns themselves.
PATH_ALLOWLIST = {
    "tools/scan_secrets.py",
    ".gitleaks.toml",
    ".gitignore",
}

# Placeholders that are meant to be committed.
ALLOWED_LITERALS = {
    "rzp_test_xxxxxxxxxxxxxx",
    "rzp_test_FAKEKEYFORTESTS",
    "rzp_live_ABCDEFGHIJKL",
}

BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mp3", ".wav", ".so", ".dll",
    ".pyc", ".exe", ".bin",
}

# File types where a realistic-looking phone/email/card is a genuine leak
# rather than prose. Keeps the PII rules sharp instead of noisy.
DATA_SUFFIXES = {".json", ".jsonl", ".csv", ".tsv", ".ndjson", ".yaml", ".yml"}
DATA_DIRS = ("llm_fixtures/", "data/", "fixtures/")


@dataclass(frozen=True)
class Rule:
    rule_id: str
    pattern: re.Pattern[str]
    message: str
    data_files_only: bool = False


RULES: tuple[Rule, ...] = (
    Rule(
        "razorpay.live_key",
        re.compile(r"rzp_live_[A-Za-z0-9]{10,}"),
        "Razorpay LIVE key id. Stop, rotate it in the dashboard, then continue.",
    ),
    Rule(
        "razorpay.test_key",
        re.compile(r"rzp_test_[A-Za-z0-9]{10,}"),
        "Razorpay test key id. Test keys still belong in .env, not in git.",
    ),
    Rule(
        "anthropic.api_key",
        re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),
        "Anthropic API key.",
    ),
    Rule(
        "aws.access_key",
        re.compile(r"AKIA[0-9A-Z]{16}"),
        "AWS access key id.",
    ),
    Rule(
        "google.api_key",
        re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
        "Google API key.",
    ),
    Rule(
        "slack.token",
        re.compile(r"xox[abprs]-[0-9A-Za-z\-]{10,}"),
        "Slack token.",
    ),
    Rule(
        "github.token",
        re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
        "GitHub token.",
    ),
    Rule(
        "generic.private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY"),
        "Private key block.",
    ),
    Rule(
        "generic.jwt",
        re.compile(r"eyJ[A-Za-z0-9\-_]{10,}\.eyJ[A-Za-z0-9\-_]{10,}\."),
        "JSON Web Token.",
    ),
    Rule(
        "generic.assigned_secret",
        re.compile(
            # Not \b: the keyword is usually a *suffix* of the identifier
            # (RAZORPAY_WEBHOOK_SECRET), and an underscore is a word character,
            # so \b never fires there.
            # The optional quote after \w* matters for JSON and dict literals,
            # where the key's closing quote sits between the name and the colon.
            r"(?i)(?<![A-Za-z0-9])(?:api[_-]?key|secret|password|passwd|token|auth)\w*[\"']?\s*[=:]\s*"
            r"[\"']?([A-Za-z0-9+/=_\-]{16,})[\"']?"
        ),
        "Hard-coded credential. Read it from the environment instead.",
    ),
    Rule(
        "pii.phone_in",
        re.compile(r"(?<!\d)(?:\+?91[\-\s]?)?[6-9]\d{9}(?!\d)"),
        "Indian phone number in a data file. Synthetic data must be unmistakably fake.",
        data_files_only=True,
    ),
    Rule(
        "pii.card_in",
        re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)"),
        "Card-like number in a data file.",
        data_files_only=True,
    ),
    Rule(
        "pii.email_in",
        re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
        "Email address in a data file.",
        data_files_only=True,
    ),
)

# Values that look like credentials but are obviously not.
_PLACEHOLDER = re.compile(
    r"(?i)^(?:x{4,}|y{4,}|\.{3,}|<.*>|\$\{.*\}|changeme|placeholder|example|"
    r"your[_-]?\w*|fake\w*|dummy\w*|none|null|true|false|os\.environ.*|"
    r"process\.env.*|[0-9a-f]{64}|counterfoil|postgresql.*)$"
)

# Ordinary identifiers: enum values, constants, config keys. Real credentials
# mix case and digits; ``authentication_dropoff`` does not. Without this the
# scanner cries wolf on every enum in the domain layer and gets switched off,
# which is strictly worse than a slightly narrower rule.
_WORDY = re.compile(r"^(?:[a-z]+(?:[_-][a-z]+)*|[A-Z]+(?:[_-][A-Z]+)*)$")


@dataclass(frozen=True)
class Finding:
    path: str
    line_no: int
    rule_id: str
    message: str
    excerpt: str


def _is_data_file(rel_path: str) -> bool:
    return Path(rel_path).suffix.lower() in DATA_SUFFIXES or any(
        rel_path.startswith(d) for d in DATA_DIRS
    )


def _redact(match_text: str) -> str:
    if len(match_text) <= 8:
        return "*" * len(match_text)
    return f"{match_text[:4]}{'*' * 8}{match_text[-2:]}"


def scan_text(rel_path: str, text: str) -> list[Finding]:
    if rel_path in PATH_ALLOWLIST:
        return []

    is_data = _is_data_file(rel_path)
    findings: list[Finding] = []

    for line_no, line in enumerate(text.splitlines(), start=1):
        if "noqa: secret" in line:
            continue
        for rule in RULES:
            if rule.data_files_only and not is_data:
                continue
            for match in rule.pattern.finditer(line):
                hit = match.group(0)
                if hit in ALLOWED_LITERALS:
                    continue
                if rule.rule_id == "generic.assigned_secret":
                    value = match.group(1)
                    if (
                        _PLACEHOLDER.match(value)
                        or _WORDY.match(value)
                        or value in ALLOWED_LITERALS
                    ):
                        continue
                findings.append(
                    Finding(rel_path, line_no, rule.rule_id, rule.message, _redact(hit))
                )
    return findings


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout


def _readable(rel_path: str, blob: bytes) -> str | None:
    if Path(rel_path).suffix.lower() in BINARY_SUFFIXES or b"\x00" in blob[:8000]:
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan_staged() -> list[Finding]:
    names = [n for n in _git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines() if n]
    findings: list[Finding] = []
    for name in names:
        blob = subprocess.run(
            ["git", "show", f":{name}"], cwd=REPO, capture_output=True, check=False
        ).stdout
        text = _readable(name, blob)
        if text is not None:
            findings.extend(scan_text(name, text))
    return findings


def scan_worktree() -> list[Finding]:
    names = [
        n
        for n in _git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
        if n
    ]
    findings: list[Finding] = []
    for name in names:
        path = REPO / name
        if not path.is_file():
            continue
        text = _readable(name, path.read_bytes())
        if text is not None:
            findings.extend(scan_text(name, text))
    return findings


def scan_history() -> list[Finding]:
    """Every blob ever committed. Run this before making the repo public."""
    findings: list[Finding] = []
    seen: set[str] = set()
    revs = _git("rev-list", "--all").split()
    for rev in revs:
        for line in _git("ls-tree", "-r", rev).splitlines():
            meta, _, name = line.partition("\t")
            sha = meta.split()[2]
            if sha in seen:
                continue
            seen.add(sha)
            blob = subprocess.run(
                ["git", "cat-file", "-p", sha], cwd=REPO, capture_output=True, check=False
            ).stdout
            text = _readable(name, blob)
            if text is not None:
                findings.extend(scan_text(f"{rev[:8]}:{name}", text))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true", help="scan the staged index (hook mode)")
    group.add_argument("--all", action="store_true", help="scan the whole working tree")
    group.add_argument("--history", action="store_true", help="scan every blob in every commit")
    args = parser.parse_args()

    if args.history:
        findings, scope = scan_history(), "git history"
    elif args.all:
        findings, scope = scan_worktree(), "working tree"
    else:
        findings, scope = scan_staged(), "staged changes"

    if not findings:
        print(f"scan_secrets: clean ({scope})")
        return 0

    print(f"\nscan_secrets: {len(findings)} finding(s) in {scope}\n", file=sys.stderr)
    for f in findings:
        print(f"  {f.path}:{f.line_no}", file=sys.stderr)
        print(f"    [{f.rule_id}] {f.message}", file=sys.stderr)
        print(f"    matched: {f.excerpt}\n", file=sys.stderr)
    print(
        "Commit refused. Move the value into .env, or append '# noqa: secret' to the\n"
        "line if this is genuinely a placeholder.\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
