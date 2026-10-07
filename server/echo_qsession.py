#!/usr/bin/env python3
"""Phase 5D.6 — bounded Question Session Manager for Echo one-shot Question Mode.

Separates the *Echo device state* (owned by EchoController, always returns to
IDLE after a turn) from the *Hermes conversational session* (a bounded, reused
AIAgent that survives across independent wake cycles inside a continuity TTL).

Lifecycle::

    turn 1: IDLE -> wake -> get_or_create() -> new Q  -> ask/answer -> touch -> IDLE
    turn 2: IDLE -> wake -> get_or_create() -> reuse Q -> ask/answer -> touch -> IDLE
    ... after TTL ... -> expire() -> next wake creates a new Q

Reset causes: TTL expiry, explicit reset phrase/command, fatal agent error,
server shutdown, and (future) transition into Live Mode.  Reset only discards
the Question-Mode conversation; it never touches global Hermes memory.
"""

from __future__ import annotations

import json
import threading
import time
import uuid

# Explicit reset phrases (deterministic, no LLM).  These reset the Question-Mode
# conversational session only.
RESET_PHRASES = ("換個話題", "忘掉剛才的對話", "開新對話")
RESET_REPLY = "好，我們換個新話題。"


def _norm(text: str) -> str:
    return "".join((text or "").split()).lower()


def is_reset_phrase(text: str) -> bool:
    """True when the utterance is an explicit reset command (not a long sentence
    that merely contains the words)."""
    t = _norm(text)
    if not t:
        return False
    for p in RESET_PHRASES:
        if t == _norm(p):
            return True
        # allow a trailing/leading filler of at most 4 chars, e.g. "那換個話題吧"
        if p in t and len(t) <= len(p) + 4:
            return True
    return False


def sanitize(obj, depth: int = 0):
    """Redact secret-looking keys and bound string sizes for tool logging."""
    _SECRET = ("token", "secret", "password", "api_key", "apikey", "authorization",
               "cookie", "credential", "access_key", "private_key")
    if depth > 4:
        return "..."
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(s in str(k).lower() for s in _SECRET):
                out[k] = "<redacted>"
            else:
                out[k] = sanitize(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [sanitize(v, depth + 1) for v in list(obj)[:20]]
    if isinstance(obj, str):
        return obj if len(obj) <= 300 else obj[:300] + "…"
    return obj


def summarize_result(result, limit: int = 240) -> str:
    """Short, secret-free summary of a tool result for the [tool-result] log line."""
    try:
        if isinstance(result, (dict, list)):
            s = json.dumps(sanitize(result), ensure_ascii=False)
        else:
            s = str(result)
    except Exception:  # noqa: BLE001
        s = repr(result)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit] + "…"


class QuestionSession:
    """One bounded Hermes conversational session (one reused AIAgent)."""

    def __init__(self, key: str, session_id: str, agent, now: float):
        self.key = key
        self.session_id = session_id
        self.agent = agent
        self.created_at = now
        self.last_used = now
        self.turns = 0
        self.lock = threading.RLock()
        self.trace_lock = threading.Lock()
        self.current_trace: list = []
        self.cancel_event = threading.Event()

    def age(self, now: float | None = None) -> float:
        return (now or time.time()) - self.last_used


class QuestionSessionManager:
    """Bounded, TTL-expiring store of QuestionSession objects keyed by a stable key.

    ``build_agent(session_id)`` must return a fresh AIAgent with tool callbacks
    already wired to the supplied ``QuestionSession``.
    """

    def __init__(self, build_agent, ttl: float = 300.0, max_sessions: int = 2,
                 log=None, trace=None):
        self._build_agent = build_agent
        self.ttl = float(ttl)
        self.max_sessions = int(max_sessions)
        self._sessions: dict[str, QuestionSession] = {}
        self._lock = threading.RLock()
        self._log = log or (lambda m: print(m, flush=True))
        self._trace = trace or (lambda *a, **k: None)

    # ---- internal ----
    def _close(self, sess: QuestionSession, reason: str) -> None:
        try:
            sess.agent.close()
        except Exception as exc:  # noqa: BLE001
            self._log(f"[qsession] close id={sess.session_id} error={exc}")
        self._trace("qsession", action="close", id=sess.session_id, reason=reason,
                    age=round(sess.age(), 1), turns=sess.turns)

    def _create(self, key: str, now: float) -> QuestionSession:
        sid = f"echo-q-{uuid.uuid4().hex[:12]}"
        sess = QuestionSession(key, sid, None, now)
        sess.agent = self._build_agent(sess)
        self._sessions[key] = sess
        self._trace("qsession", action="create", id=sid, ttl=self.ttl)
        return sess

    # ---- public API ----
    def get_or_create(self, key: str = "echo", reason: str = "wake") -> tuple[QuestionSession, bool]:
        now = time.time()
        self.expire(now=now)
        with self._lock:
            sess = self._sessions.get(key)
            if sess is not None:
                self._trace("qsession", action="reuse", id=sess.session_id,
                            age=round(sess.age(now), 1), turns=sess.turns, reason=reason)
                return sess, True
            # Enforce the cap: drop the oldest idle session first.
            if len(self._sessions) >= self.max_sessions:
                oldest = min(self._sessions.values(), key=lambda s: s.last_used)
                self._sessions.pop(oldest.key, None)
                with oldest.lock:
                    self._close(oldest, "cap_evict")
            return self._create(key, now), False

    def touch(self, sess: QuestionSession) -> None:
        with self._lock:
            sess.last_used = time.time()
            sess.turns += 1

    def expire(self, now: float | None = None, reason: str = "ttl") -> list[str]:
        now = now or time.time()
        expired = []
        with self._lock:
            for key, sess in list(self._sessions.items()):
                if sess.age(now) > self.ttl:
                    self._sessions.pop(key, None)
                    with sess.lock:
                        self._close(sess, reason)
                    expired.append(sess.session_id)
        return expired

    def reset(self, key: str = "echo", reason: str = "explicit") -> str | None:
        with self._lock:
            sess = self._sessions.pop(key, None)
        if sess is None:
            return None
        with sess.lock:
            self._close(sess, reason)
        return sess.session_id

    def get(self, key: str = "echo") -> QuestionSession | None:
        with self._lock:
            return self._sessions.get(key)

    def status(self) -> list[dict]:
        now = time.time()
        with self._lock:
            out = []
            for s in self._sessions.values():
                msgs = getattr(s.agent, "_session_messages", None) or []
                last_user = ""
                for m in reversed(msgs):
                    if isinstance(m, dict) and m.get("role") == "user":
                        last_user = " ".join(str(m.get("content") or "").split())[:80]
                        break
                out.append({
                    "key": s.key,
                    "session_id": s.session_id,
                    "age_s": round(s.age(now), 1),
                    "turns": s.turns,
                    "ttl_s": self.ttl,
                    "cancelled": s.cancel_event.is_set(),
                    "history_len": len(msgs),
                    "last_user": last_user,
                })
            return out

    def shutdown(self, reason: str = "server_shutdown") -> None:
        with self._lock:
            items = list(self._sessions.values())
            self._sessions.clear()
        for sess in items:
            with sess.lock:
                self._close(sess, reason)

    # ---- tool-call observability ----
    def record_tool_call(self, sess: QuestionSession, name: str, args) -> None:
        with sess.trace_lock:
            sess.current_trace.append({"kind": "call", "name": name,
                                       "args": json.dumps(sanitize(args), ensure_ascii=False)})
        self._trace("tool-call", session=sess.session_id, name=name,
                    args=json.dumps(sanitize(args), ensure_ascii=False))

    def record_tool_result(self, sess: QuestionSession, name: str, result) -> None:
        summary = summarize_result(result)
        with sess.trace_lock:
            sess.current_trace.append({"kind": "result", "name": name, "summary": summary})
        self._trace("tool-result", session=sess.session_id, name=name, success="true",
                    summary=summary)
