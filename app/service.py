"""Business logic: staging, late reports and historical month attachment."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, Sequence

import psycopg
from psycopg import sql

from .csv_import import AlarmRow, CsvError, month_bounds, parse_csv
from .db import connect

# Advisory-lock namespaces.  Late reports take a *shared* lock on their month;
# attach takes an *exclusive* lock on the same month for its short final step,
# so writes for that month pause only while the swap happens.
#
# Attach attempts are additionally serialised *globally*.  An attach reads the
# parent table (AccessShare) and later needs an AccessExclusive lock on it for
# ATTACH PARTITION.  Two attaches running concurrently would each try to
# upgrade their own lock and deadlock, even for different months.  Taking one
# global attach lock first — before any parent-table read — means at most one
# attach holds parent locks at a time, so the upgrade can only wait for late
# reports (which never upgrade) rather than for another attach.
_MONTH_LOCK_BASE = 10**12
_ATTACH_LOCK_KEY = 2 * 10**12 + 1


def month_lock_key(month_start: date) -> int:
    return _MONTH_LOCK_BASE + month_start.year * 100 + month_start.month


def attach_lock_key() -> int:
    return _ATTACH_LOCK_KEY


def partition_name(month_start: date) -> str:
    return f"alarm_p_{month_start:%Y%m}"


def next_month(month_start: date) -> date:
    return month_bounds(month_start)[1]


def _existing_partition(conn: psycopg.Connection, month_start: date) -> str | None:
    """Name of an attached partition covering the month, if any.

    Reads the real partition bounds rather than trusting our naming scheme, so
    a manually created partition still blocks a second attach.
    """
    rows = conn.execute(
        """
        SELECT c.relname AS name,
               pg_get_expr(c.relpartbound, c.oid, true) AS bound
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = 'alarm'::regclass
          AND c.relpartbound IS NOT NULL
          AND pg_get_expr(c.relpartbound, c.oid, true) <> 'DEFAULT'
        """
    ).fetchall()
    for row in rows:
        bound = row["bound"] or ""
        match = re.search(r"FROM \('([^']+)'\) TO \('([^']+)'\)", bound)
        if not match:
            continue
        lower, upper = date.fromisoformat(match.group(1)), date.fromisoformat(match.group(2))
        if lower <= month_start < upper:
            return row["name"]
    return None


def _offline_month_table(conn: psycopg.Connection, month_start: date) -> str | None:
    """Name of a detached (offline) table still holding the month's rows.

    After a manual ``ALTER TABLE alarm DETACH PARTITION`` the table keeps its
    standard name ``alarm_p_YYYYMM`` but no longer inherits from ``alarm``.
    Such a table is exactly what a same-month re-attach has to reinstall; it is
    *not* an attached partition (see :func:`_existing_partition`).
    """
    name = partition_name(month_start)
    exists = conn.execute("SELECT to_regclass(%s) AS name", (name,)).fetchone()["name"]
    if not exists:
        return None
    attached = conn.execute(
        """
        SELECT 1
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = 'alarm'::regclass AND c.relname = %s
        """,
        (name,),
    ).fetchone()
    return None if attached else name


def _table_index_names(conn: psycopg.Connection, table: str) -> list[str]:
    """Index names (including constraint-backed ones) of a table."""
    return [
        row["indexname"]
        for row in conn.execute(
            "SELECT indexname FROM pg_indexes"
            " WHERE schemaname = 'public' AND tablename = %s",
            (table,),
        ).fetchall()
    ]


def _offline_rows(conn: psycopg.Connection, table: str) -> list[dict]:
    return conn.execute(
        sql.SQL(
            "SELECT alarm_no, occurred_at, warehouse_no, temperature, source"
            " FROM {}"
        ).format(sql.Identifier(table))
    ).fetchall()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def content_of(occurred_at: datetime, warehouse_no: str, temperature: Decimal) -> tuple:
    return (_utc(occurred_at), warehouse_no, Decimal(temperature))


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


@dataclass
class StageResult:
    batch_id: int
    status: str
    row_count: int
    errors: list[dict]


def stage_upload(month_start: date, filename: str, raw: bytes) -> StageResult:
    """Parse a CSV and stage its rows.  Rejects the whole batch on any row error."""
    month_start = date(month_start.year, month_start.month, 1)
    month_end = next_month(month_start)

    try:
        rows, errors = parse_csv(raw, month_start)
    except CsvError as exc:
        batch_id = _create_batch(
            month_start, month_end, filename, status="rejected", error=str(exc)
        )
        return StageResult(batch_id, "rejected", 0, [{"line": 0, "message": str(exc)}])

    if errors:
        batch_id = _create_batch(
            month_start,
            month_end,
            filename,
            status="rejected",
            error=f"{len(errors)} 行校验失败",
            rejected_count=len(errors),
            conflict_json={"row_errors": errors[:200]},
        )
        return StageResult(batch_id, "rejected", len(rows), errors)

    batch_id = _create_batch(
        month_start, month_end, filename, status="staged", row_count=len(rows)
    )
    _load_staging(batch_id, rows)
    return StageResult(batch_id, "staged", len(rows), [])


def _create_batch(
    month_start: date,
    month_end: date,
    filename: str,
    *,
    status: str,
    error: str | None = None,
    row_count: int = 0,
    rejected_count: int = 0,
    conflict_json: dict | None = None,
) -> int:
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO alarm_batch
                (month_start, month_end, filename, status, error, row_count,
                 rejected_count, conflict_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                month_start,
                month_end,
                filename,
                status,
                error,
                row_count,
                rejected_count,
                json.dumps(conflict_json, ensure_ascii=False) if conflict_json else None,
            ),
        ).fetchone()
        return row["id"]


def _load_staging(batch_id: int, rows: Sequence[AlarmRow]) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            with cur.copy(
                "COPY alarm_staging (batch_id, line_no, alarm_no, occurred_at,"
                " warehouse_no, temperature) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(
                        (
                            batch_id,
                            row.line_no,
                            row.alarm_no,
                            row.occurred_at,
                            row.warehouse_no,
                            row.temperature,
                        )
                    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def get_batch(batch_id: int) -> dict | None:
    with connect() as conn:
        batch = conn.execute(
            "SELECT * FROM alarm_batch WHERE id = %s", (batch_id,)
        ).fetchone()
        if batch is None:
            return None
        batch["staged_rows"] = conn.execute(
            """
            SELECT line_no, alarm_no, occurred_at, warehouse_no, temperature
            FROM alarm_staging WHERE batch_id = %s ORDER BY line_no LIMIT 500
            """,
            (batch_id,),
        ).fetchall()
        batch["staged_total"] = conn.execute(
            "SELECT count(*) AS n FROM alarm_staging WHERE batch_id = %s",
            (batch_id,),
        ).fetchone()["n"]
        return batch


def list_batches(limit: int = 50) -> list[dict]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT id, month_start, filename, status, row_count, rejected_count,
                   error, created_at, attached_at
            FROM alarm_batch ORDER BY id DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()


def month_summary() -> list[dict]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT occurred_month AS month,
                   count(*) AS total,
                   count(*) FILTER (WHERE source = 'late') AS late_count,
                   count(*) FILTER (WHERE source = 'history') AS history_count
            FROM alarm
            GROUP BY occurred_month
            ORDER BY occurred_month DESC
            """
        ).fetchall()


def month_partition_exists(month_start: date) -> bool:
    with connect() as conn:
        return _existing_partition(conn, month_start) is not None


def month_records(month_start: date, limit: int = 1000) -> list[dict]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT alarm_no, occurred_at, warehouse_no, temperature, source,
                   batch_id, tableoid::regclass::text AS storage
            FROM alarm
            WHERE occurred_month = %s
            ORDER BY occurred_at, alarm_no
            LIMIT %s
            """,
            (month_start, limit),
        ).fetchall()


def month_storage(month_start: date) -> str:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT tableoid::regclass::text AS storage
            FROM alarm WHERE occurred_month = %s LIMIT 1
            """,
            (month_start,),
        ).fetchone()
        name = partition_name(month_start)
        offline = _offline_month_table(conn, month_start)
        if row:
            if offline:
                # Rows currently come from the default partition while the
                # detached table waits to be reinstalled by a re-attach.
                return row["storage"] + "（另有离线月表 " + name + " 待重挂）"
            return row["storage"]
        if offline:
            return name + "（离线，待重挂）"
        if conn.execute(
            "SELECT to_regclass(%s) AS name", (name,)
        ).fetchone()["name"]:
            return name + "（空）"
        return "alarm_default"


# ---------------------------------------------------------------------------
# Late reports
# ---------------------------------------------------------------------------


@dataclass
class LateResult:
    status: str  # inserted | duplicate | conflict
    alarm_no: str
    detail: str


def record_late(
    alarm_no: str,
    occurred_at: datetime,
    warehouse_no: str,
    temperature: Decimal | str,
) -> LateResult:
    """Insert a late alarm into the parent table.

    Late reports landing before the month is attached go to the default
    partition; afterwards they go to the month partition.  A shared advisory
    lock keeps this consistent with a concurrent attach of the same month.
    """
    alarm_no = (alarm_no or "").strip()
    if not alarm_no:
        raise ValueError("告警号为空")
    warehouse_no = (warehouse_no or "").strip()
    if not warehouse_no:
        raise ValueError("库房号为空")
    occurred_at = _utc(occurred_at)
    try:
        temperature = Decimal(str(temperature).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"温度不是数字：{temperature!r}") from exc
    if not temperature.is_finite():
        raise ValueError("温度不是有限数值")
    month_start = date(occurred_at.year, occurred_at.month, 1)
    content = content_of(occurred_at, warehouse_no, temperature)

    with connect() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock_shared(%s)", (month_lock_key(month_start),)
        )
        inserted = conn.execute(
            """
            INSERT INTO alarm
                (alarm_no, occurred_at, warehouse_no, temperature, occurred_month)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (occurred_month, alarm_no) DO NOTHING
            RETURNING alarm_no
            """,
            (alarm_no, occurred_at, warehouse_no, temperature, month_start),
        ).fetchone()
        if inserted:
            storage = conn.execute(
                "SELECT tableoid::regclass::text AS s FROM alarm"
                " WHERE occurred_month = %s AND alarm_no = %s",
                (month_start, alarm_no),
            ).fetchone()["s"]
            return LateResult("inserted", alarm_no, f"已写入 {storage}")

        existing = conn.execute(
            """
            SELECT occurred_at, warehouse_no, temperature
            FROM alarm WHERE occurred_month = %s AND alarm_no = %s
            """,
            (month_start, alarm_no),
        ).fetchone()
        existing_content = content_of(
            existing["occurred_at"],
            existing["warehouse_no"],
            existing["temperature"],
        )
        if existing_content == content:
            return LateResult("duplicate", alarm_no, "同月同号同内容已存在，未重复写入")
        return LateResult(
            "conflict",
            alarm_no,
            f"告警 {alarm_no} 同月同号内容不一致：已有 "
            f"{_utc(existing['occurred_at']).isoformat()} / "
            f"{existing['warehouse_no']} / {existing['temperature']}",
        )


# ---------------------------------------------------------------------------
# Attach
# ---------------------------------------------------------------------------


@dataclass
class AttachResult:
    ok: bool
    status: str
    message: str
    conflicts: list[dict]
    inserted_history: int = 0
    kept_late: int = 0


class AttachError(Exception):
    """Transient/database failure while installing the partition."""


def _reconcile(
    staged: Iterable[dict], existing: Iterable[dict]
) -> tuple[list[dict], list[dict]]:
    """Return (rows to insert as history, conflicts).

    ``existing`` is the de-duplicated set of already accepted *late* rows
    (default partition plus any late rows carried by an offline month table).
    A pre-existing late row wins when its content is identical; differing
    content for the same alarm number is a conflict and rejects the batch.
    """
    existing_map = {
        row["alarm_no"]: content_of(
            row["occurred_at"], row["warehouse_no"], row["temperature"]
        )
        for row in existing
    }
    chosen: dict[str, dict] = {}
    conflicts: dict[tuple[str, str], dict] = {}

    for row in staged:
        alarm_no = row["alarm_no"]
        content = content_of(row["occurred_at"], row["warehouse_no"], row["temperature"])
        if alarm_no in existing_map:
            if content != existing_map[alarm_no]:
                conflicts.setdefault(
                    (alarm_no, "与已接收补报内容不一致"),
                    {
                        "alarm_no": alarm_no,
                        "reason": "与已接收补报内容不一致",
                        "csv_line": row["line_no"],
                    },
                )
            # Identical: keep the already accepted late row, drop the duplicate.
            continue
        if alarm_no in chosen:
            previous = chosen[alarm_no]
            previous_content = content_of(
                previous["occurred_at"],
                previous["warehouse_no"],
                previous["temperature"],
            )
            if previous_content != content:
                conflicts.setdefault(
                    (alarm_no, "同批次内同号内容不一致"),
                    {
                        "alarm_no": alarm_no,
                        "reason": "同批次内同号内容不一致",
                        "csv_line": row["line_no"],
                    },
                )
            continue
        chosen[alarm_no] = row

    return list(chosen.values()), list(conflicts.values())


def _collect_late_rows(
    default_rows: Iterable[dict], offline_rows: Iterable[dict]
) -> tuple[list[dict], list[dict]]:
    """Merge late reports from the default partition and an offline month table.

    Returns ``(late_rows, conflicts)``.  The same alarm number in both places
    with identical content yields one late row (a late report stays a late
    report); differing content is a conflict that rejects the new batch.
    Offline rows originally loaded as history are excluded here — a re-attach
    replaces that history wholesale with the new CSV.
    """
    merged: dict[str, dict] = {}
    conflicts: list[dict] = []
    seen_conflict: set[str] = set()

    def consider(row: dict, reason: str) -> None:
        alarm_no = row["alarm_no"]
        content = content_of(row["occurred_at"], row["warehouse_no"], row["temperature"])
        if alarm_no in merged:
            if content_of(
                merged[alarm_no]["occurred_at"],
                merged[alarm_no]["warehouse_no"],
                merged[alarm_no]["temperature"],
            ) != content and alarm_no not in seen_conflict:
                conflicts.append(
                    {
                        "alarm_no": alarm_no,
                        "reason": reason,
                        "csv_line": None,
                    }
                )
                seen_conflict.add(alarm_no)
            return
        merged[alarm_no] = row

    # Default-partition rows are the most recently accepted late reports and
    # take the first slot; offline late rows are then folded in.
    for row in default_rows:
        consider(row, "默认分区与离线月表中同号补报内容不一致")
    for row in offline_rows:
        if row.get("source", "late") == "late":
            consider(row, "默认分区与离线月表中同号补报内容不一致")
    return list(merged.values()), conflicts


def attach_batch(batch_id: int) -> AttachResult:
    try:
        with connect() as conn:
            batch = conn.execute(
                "SELECT * FROM alarm_batch WHERE id = %s FOR UPDATE", (batch_id,)
            ).fetchone()
            if batch is None:
                return AttachResult(False, "missing", "批次不存在", [])
            if batch["status"] == "attached":
                return AttachResult(False, "attached", "该批次已挂接，未重复执行", [])
            if batch["status"] == "superseded":
                return AttachResult(
                    False, "superseded", "该批次已被同月新批替代，不能再次挂接", []
                )
            if batch["status"] == "rejected":
                conflicts = (batch["conflict_json"] or {}).get("conflicts", [])
                return AttachResult(
                    False,
                    "rejected",
                    "该批次已拒绝，请修正后重新上传",
                    conflicts,
                )

            month_start = batch["month_start"]
            month_end = batch["month_end"]
            part_name = partition_name(month_start)

            # Serialise every attach (any month) before reading the parent
            # table, so parent-table lock upgrades cannot deadlock with one
            # another.  Late reports do not take this lock.
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (attach_lock_key(),))

            staged = conn.execute(
                """
                SELECT line_no, alarm_no, occurred_at, warehouse_no, temperature
                FROM alarm_staging WHERE batch_id = %s ORDER BY line_no
                """,
                (batch_id,),
            ).fetchall()
            if not staged:
                message = "批次没有可挂接的数据行"
                _mark_rejected(conn, batch_id, message, [])
                return AttachResult(False, "rejected", message, [])

            # An *attached* partition for the month still blocks a second
            # attach.  A *detached* table keeping the standard name is the
            # re-attach case and is reinstalled below instead.
            if _existing_partition(conn, month_start):
                message = f"{month_start:%Y-%m} 已存在月分区，拒绝重复挂接"
                _mark_rejected(conn, batch_id, message, [])
                return AttachResult(False, "rejected", message, [])

            offline_table = _offline_month_table(conn, month_start)

            # Accepted late rows live in the default partition; rows received
            # before the earlier DETACH may still sit in the offline table.
            # Old history rows in the offline table are intentionally ignored:
            # the new CSV replaces that history.
            default_rows = _default_rows(conn, month_start)
            offline_rows = (
                _offline_rows(conn, offline_table) if offline_table else []
            )
            late_rows, late_conflicts = _collect_late_rows(default_rows, offline_rows)
            if late_conflicts:
                return _reject_conflicts(conn, batch_id, late_conflicts)

            history_rows, conflicts = _reconcile(staged, late_rows)
            if conflicts:
                return _reject_conflicts(conn, batch_id, conflicts)

            # Build the new partition table and bulk-copy it *before* taking
            # the month write lock, so late reports keep flowing during the
            # heavy part of the load.
            build = f"alarm_build_{batch_id}"
            conn.execute(
                sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(build))
            )
            conn.execute(
                sql.SQL("CREATE TABLE {} (LIKE alarm INCLUDING ALL)").format(
                    sql.Identifier(build)
                )
            )
            conn.execute(
                sql.SQL(
                    "ALTER TABLE {part} ADD CONSTRAINT {constraint} CHECK ("
                    "occurred_month >= {lower}::date "
                    "AND occurred_month < {upper}::date)"
                ).format(
                    part=sql.Identifier(build),
                    constraint=sql.Identifier(f"{build}_month_check"),
                    lower=sql.Literal(month_start.isoformat()),
                    upper=sql.Literal(month_end.isoformat()),
                )
            )
            _copy_rows(conn, build, month_start, batch["id"], late_rows, history_rows)

            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (month_lock_key(month_start),),
            )

            # Late reports committed while the bulk copy ran are not in the
            # build table yet.  Merge them in (and check for conflicts) now
            # that no further writes for this month can slip in.
            late_now = _default_rows(conn, month_start)
            merge_conflicts = _merge_new_late_rows(conn, build, late_now)
            if merge_conflicts:
                conn.execute(
                    sql.SQL("DROP TABLE {}").format(sql.Identifier(build))
                )
                return _reject_conflicts(conn, batch_id, merge_conflicts)

            kept_late = conn.execute(
                sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(build))
            ).fetchone()["n"] - len(history_rows)

            if offline_table:
                # Free the standard table/index names held by the detached
                # table.  Everything runs in this transaction: any later
                # failure rolls back and the offline table is back in place.
                retired = f"alarm_retired_{month_start:%Y%m}_{batch_id}"
                conn.execute(
                    sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(retired))
                )
                # Rename constraints while the table still has its old name;
                # renaming the table afterwards does not rename them.
                for suffix, target in (
                    ("_pkey", f"{retired}_pkey_constraint"),
                    ("_month_check", f"{retired}_month_check"),
                ):
                    conn.execute(
                        sql.SQL(
                            "ALTER TABLE {part} RENAME CONSTRAINT {old} TO {new}"
                        ).format(
                            part=sql.Identifier(offline_table),
                            old=sql.Identifier(offline_table + suffix),
                            new=sql.Identifier(target),
                        )
                    )
                for index, ordinal in zip(
                    _table_index_names(conn, offline_table), range(1, 100)
                ):
                    conn.execute(
                        sql.SQL("ALTER INDEX {old} RENAME TO {new}").format(
                            old=sql.Identifier(index),
                            new=sql.Identifier(f"{retired}_idx{ordinal}"),
                        )
                    )
                conn.execute(
                    sql.SQL("ALTER TABLE {old} RENAME TO {new}").format(
                        old=sql.Identifier(offline_table),
                        new=sql.Identifier(retired),
                    )
                )

            # Remove the month's rows from the default partition before
            # attaching, otherwise ATTACH PARTITION refuses overlapping rows.
            conn.execute("DELETE FROM alarm WHERE occurred_month = %s", (month_start,))
            conn.execute(
                sql.SQL("ALTER TABLE {build} RENAME TO {part}").format(
                    build=sql.Identifier(build), part=sql.Identifier(part_name)
                )
            )
            # Tidy the auto-generated constraint/index names to match the
            # partition, so repeated rebuilds do not leave confusing names.
            for suffix, target in (
                ("_pkey", f"{part_name}_pkey"),
                ("_month_check", f"{part_name}_month_check"),
            ):
                conn.execute(
                    sql.SQL(
                        "ALTER TABLE {part} RENAME CONSTRAINT {old} TO {new}"
                    ).format(
                        part=sql.Identifier(part_name),
                        old=sql.Identifier(build + suffix),
                        new=sql.Identifier(target),
                    )
                )
            conn.execute(
                sql.SQL("ALTER TABLE alarm ATTACH PARTITION {part} "
                        "FOR VALUES FROM ({lower}) TO ({upper})").format(
                    part=sql.Identifier(part_name),
                    lower=sql.Literal(month_start.isoformat()),
                    upper=sql.Literal(month_end.isoformat()),
                )
            )
            if offline_table:
                # The new partition now carries everything worth keeping; the
                # retired table is dropped only here, inside the same
                # transaction, so a failure before this point leaves it whole.
                conn.execute(
                    sql.SQL("DROP TABLE {}").format(sql.Identifier(retired))
                )

            # A successful re-attach supersedes any previously attached batch
            # of the same month; the new batch's success takes effect together
            # with the new partition.
            conn.execute(
                """
                UPDATE alarm_batch
                SET status = 'superseded',
                    error = COALESCE(error, '已被同月新批 #' || %s || ' 替代')
                WHERE month_start = %s AND status = 'attached' AND id <> %s
                """,
                (batch_id, month_start, batch_id),
            )
            conn.execute(
                """
                UPDATE alarm_batch
                SET status = 'attached', attached_at = now(), error = NULL,
                    rejected_count = 0
                WHERE id = %s
                """,
                (batch_id,),
            )
            return AttachResult(
                True,
                "attached",
                (
                    f"{month_start:%Y-%m} 重挂成功，离线月表已由新批替换"
                    if offline_table
                    else f"{month_start:%Y-%m} 挂接成功"
                ),
                [],
                inserted_history=len(history_rows),
                kept_late=kept_late,
            )
    except (AttachError, psycopg.Error) as exc:
        # The transaction rolled back: the parent table is untouched and any
        # late rows accepted before the failure remain queryable.
        message = f"挂接失败，已回滚：{exc}"
        _record_failure(batch_id, message)
        return AttachResult(False, "failed", message, [])


def _default_rows(conn: psycopg.Connection, month_start: date) -> list[dict]:
    return conn.execute(
        """
        SELECT alarm_no, occurred_at, warehouse_no, temperature
        FROM alarm WHERE occurred_month = %s
        """,
        (month_start,),
    ).fetchall()


def _reject_conflicts(
    conn: psycopg.Connection, batch_id: int, conflicts: list[dict]
) -> AttachResult:
    alarm_numbers = sorted({c["alarm_no"] for c in conflicts})
    message = "挂接被拒绝：以下告警号内容存在出入 " + ", ".join(alarm_numbers)
    _mark_rejected(conn, batch_id, message, conflicts)
    return AttachResult(False, "rejected", message, conflicts)


def _copy_rows(
    conn: psycopg.Connection,
    table: str,
    month_start: date,
    batch_id: int,
    existing: list[dict],
    history_rows: list[dict],
) -> None:
    """Bulk-copy late rows and history rows into the new partition table."""
    with conn.cursor() as cur:
        with cur.copy(
            sql.SQL(
                "COPY {} (alarm_no, occurred_at, warehouse_no, temperature,"
                " occurred_month, source, batch_id) FROM STDIN"
            ).format(sql.Identifier(table))
        ) as copy:
            for row in existing:
                copy.write_row(
                    (
                        row["alarm_no"],
                        row["occurred_at"],
                        row["warehouse_no"],
                        row["temperature"],
                        month_start,
                        "late",
                        None,
                    )
                )
            for row in history_rows:
                copy.write_row(
                    (
                        row["alarm_no"],
                        row["occurred_at"],
                        row["warehouse_no"],
                        row["temperature"],
                        month_start,
                        "history",
                        batch_id,
                    )
                )


def _merge_new_late_rows(
    conn: psycopg.Connection, table: str, late_rows: list[dict]
) -> list[dict]:
    """Copy late rows that are missing from the build table; report conflicts.

    Rows already copied by the bulk load are recognised by alarm number and
    skipped.  A late row whose content differs from the copy is a conflict.
    """
    known = {
        row["alarm_no"]: (
            _utc(row["occurred_at"]),
            row["warehouse_no"],
            Decimal(row["temperature"]),
        )
        for row in conn.execute(
            sql.SQL(
                "SELECT alarm_no, occurred_at, warehouse_no, temperature FROM {}"
            ).format(sql.Identifier(table))
        ).fetchall()
    }

    conflicts: list[dict] = []
    new_rows: list[dict] = []
    for row in late_rows:
        alarm_no = row["alarm_no"]
        content = content_of(row["occurred_at"], row["warehouse_no"], row["temperature"])
        if alarm_no in known:
            if known[alarm_no] != content:
                conflicts.append(
                    {
                        "alarm_no": alarm_no,
                        "reason": "挂接期间收到的补报与此前内容不一致",
                        "csv_line": None,
                    }
                )
            continue
        new_rows.append(row)

    if not conflicts and new_rows:
        with conn.cursor() as cur:
            with cur.copy(
                sql.SQL(
                    "COPY {} (alarm_no, occurred_at, warehouse_no, temperature,"
                    " occurred_month, source) FROM STDIN"
                ).format(sql.Identifier(table))
            ) as copy:
                for row in new_rows:
                    copy.write_row(
                        (
                            row["alarm_no"],
                            row["occurred_at"],
                            row["warehouse_no"],
                            row["temperature"],
                            _month_of(row["occurred_at"]),
                            "late",
                        )
                    )
    return conflicts


def _month_of(value: datetime) -> date:
    utc = _utc(value)
    return date(utc.year, utc.month, 1)


def _mark_rejected(
    conn: psycopg.Connection, batch_id: int, message: str, conflicts: list[dict]
) -> None:
    payload = json.dumps({"conflicts": conflicts}, ensure_ascii=False)
    conn.execute(
        """
        UPDATE alarm_batch
        SET status = 'rejected', error = %s, conflict_json = %s,
            rejected_count = %s
        WHERE id = %s
        """,
        (message, payload, len(conflicts), batch_id),
    )


def _record_failure(batch_id: int, message: str) -> None:
    """Record a rolled-back attach attempt without changing the batch status."""
    try:
        with connect() as conn:
            conn.execute(
                """
                UPDATE alarm_batch
                SET error = %s
                WHERE id = %s AND status = 'staged'
                """,
                (message, batch_id),
            )
    except psycopg.Error:
        pass
