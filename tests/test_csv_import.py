"""Unit tests for CSV parsing."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.csv_import import CsvError, month_bounds, parse_csv, parse_month


def test_parse_month_and_bounds():
    assert parse_month("2026-10") == date(2026, 10, 1)
    assert month_bounds(date(2026, 10, 1)) == (date(2026, 10, 1), date(2026, 11, 1))
    assert month_bounds(date(2026, 12, 1)) == (date(2026, 12, 1), date(2027, 1, 1))


@pytest.mark.parametrize("value", ["", "2026", "2026-13", "2026-00", "abc"])
def test_parse_month_rejects_bad_input(value):
    with pytest.raises(ValueError):
        parse_month(value)


def test_parse_english_headers():
    raw = (
        b"alarm_no,occurred_at,warehouse_no,temperature\n"
        b"ALM-1,2026-10-03T01:00:00Z,W1,-18.5\n"
    )
    rows, errors = parse_csv(raw, date(2026, 10, 1))
    assert errors == []
    assert len(rows) == 1
    row = rows[0]
    assert row.alarm_no == "ALM-1"
    assert row.occurred_at == datetime(2026, 10, 3, 1, tzinfo=timezone.utc)
    assert row.warehouse_no == "W1"
    assert row.temperature == Decimal("-18.5")
    assert row.month_start == date(2026, 10, 1)


def test_parse_chinese_headers_with_bom_and_naive_time():
    raw = (
        "﻿告警号,发生时间,库房号,温度\nALM-1,2026-10-03 01:00:00,冷库A,-18.5\n"
    ).encode("utf-8")
    rows, errors = parse_csv(raw, date(2026, 10, 1))
    assert errors == []
    assert rows[0].occurred_at == datetime(2026, 10, 3, 1, tzinfo=timezone.utc)


def test_trailing_z_and_offset_normalised_to_utc():
    raw = (
        b"alarm_no,occurred_at,warehouse_no,temperature\n"
        b"ALM-1,2026-10-01T08:00:00+08:00,W1,-18\n"
        b"ALM-2,2026-10-01T00:30:00+08:00,W1,-18\n"
    )
    rows, errors = parse_csv(raw, date(2026, 10, 1))
    # 00:30 +08:00 is still September in UTC -> outside the selected month.
    assert any("不在所选 UTC 月份" in e["message"] for e in errors)
    assert len(rows) == 1
    assert rows[0].occurred_at == datetime(2026, 10, 1, 0, tzinfo=timezone.utc)


def test_row_errors_reported_and_valid_rows_kept():
    raw = (
        b"alarm_no,occurred_at,warehouse_no,temperature\n"
        b"ALM-1,not-a-time,W1,-18\n"
        b",2026-10-02T00:00:00Z,W1,-18\n"
        b"ALM-3,2026-10-02T00:00:00Z,,-18\n"
        b"ALM-4,2026-10-02T00:00:00Z,W1,abc\n"
        b"ALM-5,2026-10-02T00:00:00Z,W1,-17\n"
    )
    rows, errors = parse_csv(raw, date(2026, 10, 1))
    assert len(rows) == 1
    assert rows[0].alarm_no == "ALM-5"
    assert [e["line"] for e in errors] == [2, 3, 4, 5]


def test_missing_column_is_a_file_error():
    with pytest.raises(CsvError):
        parse_csv(b"alarm_no,occurred_at,temperature\nA,2026-10-01T00:00:00Z,-1\n", date(2026, 10, 1))


def test_empty_file_is_an_error():
    with pytest.raises(CsvError):
        parse_csv(b"", date(2026, 10, 1))


def test_blank_rows_skipped():
    raw = (
        b"alarm_no,occurred_at,warehouse_no,temperature\n"
        b"\n,,\n"
        b"ALM-1,2026-10-03T01:00:00Z,W1,-18.5\n"
    )
    rows, errors = parse_csv(raw, date(2026, 10, 1))
    assert errors == []
    assert len(rows) == 1
