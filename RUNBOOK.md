# Runbook

Everything here runs offline, with no credentials and no network calls.
Credentials only become relevant when you opt into the live Razorpay lane.

## Requirements

- Python 3.12+
- Git

## Setup

```bash
python -m venv .venv
```

```bash
pip install -r backend/requirements.txt
```

```bash
cp .env.example .env
```

`.env` is gitignored. Leaving every value blank is fine: the defaults are
dry-run, LLM replay mode, and no provider calls.

## Verify the install

```bash
python -m pytest backend/tests -q
```

## Check for secrets before committing

The git hook runs this automatically on staged files. To sweep everything:

```bash
python tools/scan_secrets.py --all
```

Before making the repository public, sweep every blob in every commit:

```bash
python tools/scan_secrets.py --history
```

## Enable the full hook set (optional)

The native hook in `.git/hooks/pre-commit` works with no dependencies. To add
gitleaks, ruff, and the large-file guard on top:

```bash
pip install pre-commit
```

```bash
pre-commit install
```

## Safety switches

| Variable | Default | Effect |
|---|---|---|
| `COUNTERFOIL_DRY_RUN` | `true` | No outbound side effects of any kind |
| `COUNTERFOIL_LLM_MODE` | `replay` | Serve model responses from committed fixtures; spends nothing |
| `COUNTERFOIL_SPEND_CAP_USD` | `2.00` | A run halts when cumulative model spend reaches this |
| `RAZORPAY_KEY_ID` | unset | Must start with `rzp_test_`; the app refuses to boot otherwise |

There is no `COUNTERFOIL_ENV=prod`. Setting it raises `UnsafeConfigError`.
