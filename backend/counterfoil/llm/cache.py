"""Two layers of not paying for the same answer twice.

**Memoisation** keys on the semantic content of a case rather than the prompt
string. A batch of a thousand failures contains far fewer than a thousand
distinct situations: the same generic gateway message recurs hundreds of times.
Diagnosing it once and reusing the answer is not an optimisation, it is the
correct behaviour, and it is what keeps the model bill in rupees rather than
thousands of rupees.

**Fixtures** persist those answers to disk and are committed to the repository.
That makes the whole eval reproducible by anyone who clones it, with no API key
and no spend, and it makes the development loop free. The recorded responses
are the evidence for the numbers in the README, so they belong in git next to
the code that produced them.

Three modes:

``replay``  never calls the API; a miss is a degraded diagnosis, not a call
``record``  calls on a miss and writes the answer to the fixture store
``live``    calls on a miss and does not persist
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


class CacheMiss(LookupError):
    """Replay mode was asked for something that was never recorded."""


@dataclass(frozen=True)
class CachedResponse:
    payload: dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""


def fingerprint(parts: dict[str, Any]) -> str:
    """A stable key for a situation, independent of prompt wording.

    Deliberately not a hash of the prompt: changing a word in the system prompt
    should not invalidate every recorded answer, because the *question* did not
    change. The tradeoff is that a genuine change to the taxonomy needs the
    fixtures re-recorded, which is why ``schema_version`` is part of the key.
    """
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


class FixtureStore:
    """A directory of one JSON file per distinct question."""

    def __init__(self, root: Path | str, mode: str = "replay") -> None:
        if mode not in {"replay", "record", "live"}:
            raise ValueError(f"mode must be replay|record|live, got {mode!r}")
        self.root = Path(root)
        self.mode = mode
        self._memo: dict[str, CachedResponse] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        if mode == "record":
            self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> CachedResponse | None:
        with self._lock:
            if key in self._memo:
                self.hits += 1
                return self._memo[key]

        path = self._path(key)
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            cached = CachedResponse(**raw)
            with self._lock:
                self._memo[key] = cached
                self.hits += 1
            return cached

        with self._lock:
            self.misses += 1
        return None

    def put(self, key: str, response: CachedResponse) -> None:
        with self._lock:
            self._memo[key] = response
        if self.mode == "record":
            self.root.mkdir(parents=True, exist_ok=True)
            self._path(key).write_text(
                json.dumps(asdict(response), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    @property
    def may_call_api(self) -> bool:
        return self.mode in {"record", "live"}

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def stats(self) -> str:
        return (
            f"fixtures[{self.mode}] {self.hits} hits, {self.misses} misses "
            f"({self.hit_rate:.0%} hit rate), {len(self._memo)} distinct questions"
        )
