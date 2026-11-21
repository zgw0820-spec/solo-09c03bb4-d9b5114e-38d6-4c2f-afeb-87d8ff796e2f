"""CSV parsing and row validation for historical alarm batches.

The CSV must contain the alarm number, occurrence time, warehouse number and
temperature.  Both Chinese and English header names are accepted.  All times
are interpreted as UTC; naive timestamps are treated as UTC and a trailing
``Z`` is accepted.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable


MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")

HEADER_ALIASES = {
    "alarm_no": {
        "alarm_no", "alarmno", "alarm_no.", "告警号", "告警编号", "警报号", "编号",
    },
    "occurred_at": {
        "occurred_at", "occurredat", "occurred_time", "occurrence_time",
        "alarm_time", "time", "发生时间", "告警时间", "时间",
    },
    "warehouse_no": {
        "warehouse_no", "warehouseno", "warehouse", "warehouse_id",
        "库房号", "库房编号", "仓库号", "冷库号",
    },
    "temperature": {
        "temperature", "temp", "温度",
    },
}

REQUIRED_FIELDS = ("alarm_no", "occurred_at", "warehouse_no", "temperature")


class CsvError(Exception):
    """Raised when the file as a whole cannot be read."""


@dataclass(frozen=True)
class AlarmRow:
    line_no: int
    alarm_no: str
    occurred_at: datetime
    warehouse_no: str
    temperature: Decimal

    @property
    def month_start(self) -> date:
        utc = self.occurred_at.astimezone(timezone.utc)
        return date(utc.year, utc.month, 1)

    @property
    def content(self) -> tuple:
        return (
            self.occurred_at.astimezone(timezone.utc),
            self.warehouse_no,
            self.temperature,
        )


def parse_month(value: str) -> date:
    """Parse a ``YYYY-MM`` string into the first day of that UTC month."""
    match = MONTH_RE.match((value or "").strip())
    if not match:
        raise ValueError("月份格式应为 YYYY-MM")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        raise ValueError("月份必须在 01 到 12 之间")
    return date(year, month, 1)


def month_bounds(month_start: date) -> tuple[date, date]:
    if month_start.month == 12:
        end = date(month_start.year + 1, 1, 1)
    else:
        end = date(month_start.year, month_start.month + 1, 1)
    return month_start, end


def decode_csv(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvError("无法识别 CSV 编码，请使用 UTF-8 或 GB18030")


def _normalise_header(name: str) -> str:
    return (name or "").strip().lower().replace(" ", "").replace("-", "")


def _alias_set(field: str) -> set[str]:
    """Canonical forms of a field's aliases, ignoring underscores."""
    return {
        alias.lower().replace(" ", "").replace("-", "").replace("_", "")
        for alias in HEADER_ALIASES[field]
    }


_ALIASES: dict[str, set[str]] = {
    field: _alias_set(field) for field in HEADER_ALIASES
}


def _map_headers(header: Iterable[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, raw_name in enumerate(header):
        name = _normalise_header(raw_name).replace("_", "")
        if not name:
            continue
        for field, aliases in _ALIASES.items():
            if name in aliases and field not in mapping:
                mapping[field] = index
    missing = [field for field in REQUIRED_FIELDS if field not in mapping]
    if missing:
        raise CsvError(
            "CSV 缺少必需列："
            + ", ".join(missing)
            + "（告警号、发生时间、库房号、温度）"
        )
    return mapping


def _parse_timestamp(value: str) -> datetime:
    text = (value or "").strip()
    if not text:
        raise ValueError("发生时间为空")
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"发生时间格式无法解析：{value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_temperature(value: str) -> Decimal:
    text = (value or "").strip()
    if not text:
        raise ValueError("温度为空")
    if text.endswith("℃") or text.endswith("°C"):
        text = text[:-1].strip()
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"温度不是数字：{value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"温度不是有限数值：{value!r}")
    return number


def parse_csv(raw: bytes, month_start: date) -> tuple[list[AlarmRow], list[dict]]:
    """Parse and validate a CSV.

    Returns ``(rows, errors)``.  Errors are dictionaries with ``line`` and
    ``message``; any error means the whole batch must be rejected.
    """
    text = decode_csv(raw)
    if not text.strip():
        raise CsvError("CSV 文件为空")

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise CsvError("CSV 文件为空") from exc
    mapping = _map_headers(header)

    month_start, month_end = month_bounds(month_start)
    rows: list[AlarmRow] = []
    errors: list[dict] = []

    for line_no, record in enumerate(reader, start=2):
        if not record or all(not cell.strip() for cell in record):
            continue

        def cell(field: str) -> str:
            index = mapping[field]
            return record[index] if index < len(record) else ""

        try:
            alarm_no = cell("alarm_no").strip()
            if not alarm_no:
                raise ValueError("告警号为空")
            occurred_at = _parse_timestamp(cell("occurred_at"))
            warehouse_no = cell("warehouse_no").strip()
            if not warehouse_no:
                raise ValueError("库房号为空")
            temperature = _parse_temperature(cell("temperature"))
        except ValueError as exc:
            errors.append({"line": line_no, "message": str(exc)})
            continue

        row = AlarmRow(
            line_no=line_no,
            alarm_no=alarm_no,
            occurred_at=occurred_at,
            warehouse_no=warehouse_no,
            temperature=temperature,
        )
        if not (month_start <= row.month_start < month_end):
            errors.append({
                "line": line_no,
                "message": (
                    f"发生时间 {occurred_at.isoformat()} 不在所选 UTC 月份 "
                    f"{month_start:%Y-%m} 内"
                ),
            })
            continue
        rows.append(row)

    if not rows and not errors:
        raise CsvError("CSV 中没有数据行")
    return rows, errors
