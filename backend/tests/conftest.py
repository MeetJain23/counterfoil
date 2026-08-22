import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def no_test_may_sleep(monkeypatch):
    """Fail loudly rather than waiting.

    Every delay in this codebase is injectable precisely so tests never wait
    for one. This has been got wrong twice: a new retry path picked up the real
    time.sleep through a dataclass default and the suite went from three
    seconds to over two minutes, which reads as slowness rather than as a bug.
    Now it reads as a failure, with the fix named in the message.
    """

    def refuse(seconds):
        raise AssertionError(
            f"a test tried to sleep for {seconds}s. Inject a fake clock instead: "
            "see build() in test_gemini_client.py"
        )

    monkeypatch.setattr(time, "sleep", refuse)
