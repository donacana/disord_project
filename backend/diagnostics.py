"""Request-local diagnostics; never add debug fields to the public API."""

import json
import logging
import os
from contextvars import ContextVar
from contextlib import contextmanager
import time

_trace = ContextVar('rag_trace', default=None)


def begin(question: str) -> None:
    _trace.set({'question': question, 'insufficient_reason': None})


def record(**fields) -> None:
    _trace.set({**(_trace.get() or {}), **fields})


def snapshot() -> dict:
    return dict(_trace.get() or {})


@contextmanager
def measure(stage: str):
    started = time.perf_counter()
    try:
        yield
    finally:
        timings = dict(snapshot().get('stage_seconds', {}))
        timings[stage] = round(time.perf_counter() - started, 3)
        record(stage_seconds=timings)


def finish(**fields) -> None:
    record(**fields)
    if os.getenv('RAG_DEBUG', '').casefold() == 'true':
        logging.getLogger(__name__).warning('[ANSWER_TRACE] %s', json.dumps(snapshot(), ensure_ascii=False, default=str))
