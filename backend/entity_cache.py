"""Disposable local cache of grounded extraction results, never article facts DB."""

import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

if __package__:
    from . import config
else:
    import config

VERSION = 'article-mentions-v1'
logger = logging.getLogger(__name__)


def key(passage: str) -> str:
    model = os.getenv('OPENAI_CHAT_MODEL', 'gpt-4o-mini').strip() or 'gpt-4o-mini'
    return hashlib.sha256(f'{VERSION}\0{model}\0{passage}'.encode()).hexdigest()


def _connect():
    path = Path(config.TREND_ENTITY_CACHE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=1)
    connection.execute('CREATE TABLE IF NOT EXISTS extraction '
                       '(key TEXT PRIMARY KEY, value TEXT NOT NULL, complete INTEGER NOT NULL, expires REAL NOT NULL)')
    return connection


def get_many(keys: list[str]) -> dict[str, tuple[list[tuple[str, str]], bool]]:
    result = {}
    if not keys:
        return result
    try:
        with closing(_connect()) as connection:
            for offset in range(0, len(keys), 400):
                batch = keys[offset:offset + 400]
                query = 'SELECT key,value,complete FROM extraction WHERE expires > ? AND key IN (' + ','.join('?' for _ in batch) + ')'
                for cache_key, value, complete in connection.execute(query, [time.time(), *batch]):
                    try:
                        mentions = json.loads(value)
                        if not isinstance(mentions, list) or not all(
                            isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)
                            and item[1] in {'idol_or_group', 'actor', 'entertainer'} for item in mentions
                        ):
                            continue
                        result[cache_key] = ([tuple(item) for item in mentions], bool(complete))
                    except (TypeError, ValueError):
                        continue
    except (OSError, sqlite3.Error):
        logger.warning('Entity cache unavailable; extracting from DB passages.')
    return result


def put_many(values: dict[str, tuple[list[tuple[str, str]], bool]]) -> None:
    if not values:
        return
    now = time.time()
    try:
        with closing(_connect()) as connection, connection:
            connection.executemany('INSERT OR REPLACE INTO extraction VALUES (?, ?, ?, ?)', [
                (cache_key, json.dumps(mentions, ensure_ascii=False), int(complete), now + (
                    config.TREND_ENTITY_CACHE_TTL_SECONDS if complete else config.TREND_ENTITY_RETRY_SECONDS))
                for cache_key, (mentions, complete) in values.items()
            ])
            connection.execute('DELETE FROM extraction WHERE expires <= ?', (now,))
    except (OSError, sqlite3.Error):
        logger.warning('Entity cache write skipped; current extraction remains usable.')
