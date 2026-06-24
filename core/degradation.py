"""Thread-local degradation event recorder.

Each thread maintains its own log. Callers drain the log at thread boundaries
to collect all degradation events from a pipeline step.

Usage:
    from core.degradation import record, drain

    # Record a degradation event (thread-safe, no lock needed):
    record("parsing", "mineru_failed", "MinerU returned empty, falling back to pdfplumber")

    # Collect and clear events from current thread:
    events = drain()
"""

import threading
import time

_thread_local = threading.local()


def _ensure_log():
    if not hasattr(_thread_local, "log"):
        _thread_local.log = []


def record(stage: str, event: str, detail: str = ""):
    """Record a degradation/failover event in the current thread's log.

    Thread-safe: each thread has its own log via threading.local().

    Args:
        stage: Pipeline stage (e.g. "parsing", "vector_search", "agent_audit")
        event: Event name (e.g. "mineru_failed", "structured_llm_failed")
        detail: Human-readable description of what happened
    """
    _ensure_log()
    _thread_local.log.append({
        "timestamp": time.time(),
        "stage": stage,
        "event": event,
        "detail": detail,
    })


def drain() -> list[dict]:
    """Return and clear the current thread's degradation log."""
    _ensure_log()
    events = _thread_local.log.copy()
    _thread_local.log.clear()
    return events
