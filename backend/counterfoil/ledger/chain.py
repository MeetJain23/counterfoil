"""The counterfoil: an append-only, hash-chained record of every decision.

Each entry commits to the hash of the entry before it, so the trail is
tamper-evident rather than merely written down. Rewriting or deleting any past
entry breaks verification at that point and every point after it.

The ledger also refuses to record raw personal data. If a payload contains
something shaped like a full phone number or email address, the write raises
rather than silently persisting it.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

GENESIS_HASH = "0" * 64

_FULL_PHONE = re.compile(r"(?<!\d)(?:\+?91[\-\s]?)?[6-9]\d{9}(?!\d)")
_FULL_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_CARD_LIKE = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")


class Stage(str, enum.Enum):
    DETECTED = "detected"
    DIAGNOSED = "diagnosed"
    DECIDED = "decided"
    EXECUTED = "executed"
    OBSERVED = "observed"
    HALTED = "halted"


class PiiInLedgerError(ValueError):
    """Raised when a payload would have written unredacted personal data."""


def _scan_for_pii(value: Any, path: str = "$") -> None:
    if isinstance(value, str):
        for pattern, label in (
            (_FULL_EMAIL, "email address"),
            (_FULL_PHONE, "phone number"),
            (_CARD_LIKE, "card-like number"),
        ):
            if pattern.search(value):
                raise PiiInLedgerError(
                    f"refusing to write {label} to the ledger at {path}; redact it first"
                )
    elif isinstance(value, dict):
        for k, v in value.items():
            _scan_for_pii(k, f"{path}.{k}")
            _scan_for_pii(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _scan_for_pii(v, f"{path}[{i}]")


def canonical(payload: Any) -> str:
    """Deterministic JSON. Key order and separators are fixed so the hash is stable."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    ts: str
    run_id: str
    event_id: str
    arm: str
    stage: str
    payload: dict[str, Any]
    prev_hash: str
    entry_hash: str = field(default="")

    def compute_hash(self) -> str:
        body = canonical({
            "seq": self.seq,
            "ts": self.ts,
            "run_id": self.run_id,
            "event_id": self.event_id,
            "arm": self.arm,
            "stage": self.stage,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        })
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        return canonical(asdict(self))


@dataclass(frozen=True)
class ChainBreak:
    seq: int
    reason: str


class Ledger:
    """Append-only hash chain persisted as JSONL.

    JSONL on purpose: a judge can open the file, read it, and re-verify the
    chain with twenty lines of Python. Auditability that needs a running
    database to inspect is not really auditability.
    """

    def __init__(self, path: str | Path, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq, self._head = self._resume()

    def _resume(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS_HASH
        last = None
        for last in self._read_raw():
            pass
        if last is None:
            return 0, GENESIS_HASH
        return int(last["seq"]) + 1, str(last["entry_hash"])

    def _read_raw(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def append(
        self,
        *,
        event_id: str,
        stage: Stage,
        payload: dict[str, Any],
        arm: str = "agent",
    ) -> LedgerEntry:
        _scan_for_pii(payload)
        with self._lock:
            draft = LedgerEntry(
                seq=self._seq,
                ts=datetime.now(timezone.utc).isoformat(),
                run_id=self.run_id,
                event_id=event_id,
                arm=arm,
                stage=stage.value,
                payload=payload,
                prev_hash=self._head,
            )
            entry = LedgerEntry(**{**asdict(draft), "entry_hash": draft.compute_hash()})
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(entry.to_json() + "\n")
            self._seq += 1
            self._head = entry.entry_hash
            return entry

    def entries(self) -> Iterator[LedgerEntry]:
        for raw in self._read_raw():
            yield LedgerEntry(**raw)

    def verify(self) -> ChainBreak | None:
        """Walk the chain. Returns the first break found, or None if intact."""
        prev = GENESIS_HASH
        expected_seq = 0
        for entry in self.entries():
            if entry.seq != expected_seq:
                return ChainBreak(entry.seq, f"sequence gap: expected {expected_seq}")
            if entry.prev_hash != prev:
                return ChainBreak(entry.seq, "prev_hash does not match preceding entry")
            if entry.compute_hash() != entry.entry_hash:
                return ChainBreak(entry.seq, "entry contents do not match its hash")
            prev = entry.entry_hash
            expected_seq += 1
        return None

    def timeline(self, event_id: str) -> list[LedgerEntry]:
        return [e for e in self.entries() if e.event_id == event_id]

    @property
    def head(self) -> str:
        return self._head
