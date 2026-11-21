"""Shared test fixtures.

Tests run against a real PostgreSQL database.  Set ``TEST_DATABASE_URL``
(or ``DATABASE_URL``) to a scratch database; the schema is dropped and rebuilt
before every test.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@/alarms_test?host=/tmp"),
)

from app.db import database_url, init_schema  # noqa: E402


SCHEMA_DROP = """
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.relname FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind IN ('r', 'p')
      AND (c.relname LIKE 'alarm%')
  LOOP
    EXECUTE format('DROP TABLE IF EXISTS public.%I CASCADE', r.relname);
  END LOOP;
END $$;
"""


@pytest.fixture()
def db():
    with psycopg.connect(database_url(), autocommit=True) as conn:
        conn.execute(SCHEMA_DROP)
    init_schema()
    yield
    with psycopg.connect(database_url(), autocommit=True) as conn:
        conn.execute(SCHEMA_DROP)


@pytest.fixture()
def clock():
    def parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    return parse


def october(value: str) -> date:
    return date(2026, 10, 1)
