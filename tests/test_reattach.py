"""Same-month re-attach tests against a real PostgreSQL instance.

Scenarios covered here, on top of the ordinary attach flow:

* a manually DETACHed month table still keeping its standard name no longer
  blocks a new attach — the new batch replaces the old history while the late
  reports from both the offline table and the default partition survive;
* identical alarm numbers collapse to one row, kept as a late report;
* content differences (time / warehouse / temperature) name the clashing
  alarm number and reject the new batch, leaving the offline table, the
  default partition and the staging rows untouched;
* a mid-attach failure rolls everything back, including the offline table
  rename, and the batch stays retryable;
* late reports accepted while the load runs still end up in the new
  partition.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import psycopg

from app import service
from app.db import connect

OCT = date(2026, 10, 1)
PART = "alarm_p_202610"


def csv_bytes(*rows: str) -> bytes:
    header = "alarm_no,occurred_at,warehouse_no,temperature\n"
    return (header + "".join(row + "\n" for row in rows)).encode()


def t(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def rows_for(month: date = OCT) -> dict[str, dict]:
    return {r["alarm_no"]: r for r in service.month_records(month)}


def detach_month_partition(month: date = OCT) -> None:
    """Simulate the operator's manual DETACH of the system-generated partition."""
    with connect() as conn:
        conn.execute(
            "ALTER TABLE alarm DETACH PARTITION {}".format(
                service.partition_name(month)
            )
        )


def offline_count(month: date = OCT) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM {}".format(service.partition_name(month))
        ).fetchone()["n"]


def offline_rows(month: date = OCT) -> dict[str, dict]:
    with connect() as conn:
        return {
            row["alarm_no"]: row
            for row in conn.execute(
                "SELECT * FROM {}".format(service.partition_name(month))
            ).fetchall()
        }


def table_exists(name: str) -> bool:
    with connect() as conn:
        return conn.execute("SELECT to_regclass(%s) AS n", (name,)).fetchone()["n"] is not None


def build_first_month(db) -> int:
    """Attach an initial batch so the month has a real partition, then DETACH it."""
    service.record_late("L-EARLY", t("2026-10-05T03:00:00Z"), "W1", "-18.5")
    first = service.stage_upload(
        OCT,
        "oct-old.csv",
        csv_bytes(
            "H-OLD-1,2026-10-01T01:00:00Z,W1,-20",
            "H-OLD-2,2026-10-02T02:00:00Z,W2,-21",
        ),
    )
    assert service.attach_batch(first.batch_id).ok
    detach_month_partition()
    return first.batch_id


# ---------------------------------------------------------------------------
# Happy path: re-attach with the detached table still holding the old name
# ---------------------------------------------------------------------------


def test_reattach_with_offline_table_present_replaces_history_and_keeps_late(db):
    first_id = build_first_month(db)

    # While the month is offline, new late reports land in the default
    # partition and remain queryable from the parent table.  Rows inside the
    # detached table are not visible through the parent until re-attach.
    after = service.record_late("L-AFTER", t("2026-10-09T08:00:00Z"), "W3", "-17")
    assert after.status == "inserted"

    visible = rows_for()
    assert set(visible) == {"L-AFTER"}
    assert visible["L-AFTER"]["storage"] == "alarm_default"
    # The offline table itself still holds everything from the first attach.
    assert offline_count() == 3
    assert "待重挂" in service.month_storage(OCT)

    second = service.stage_upload(
        OCT,
        "oct-new.csv",
        csv_bytes(
            "H-NEW-1,2026-10-03T03:30:00Z,W1,-22",
            "H-NEW-2,2026-10-04T04:40:00Z,W2,-23",
        ),
    )
    result = service.attach_batch(second.batch_id)
    assert result.ok, result.message
    assert result.inserted_history == 2
    assert result.kept_late == 2

    rows = rows_for()
    # New CSV history is visible; old history was replaced wholesale.
    assert set(rows) == {"H-NEW-1", "H-NEW-2", "L-EARLY", "L-AFTER"}
    assert all(r["storage"] == PART for r in rows.values())
    assert rows["H-NEW-1"]["source"] == "history"
    assert rows["H-NEW-1"]["batch_id"] == second.batch_id
    assert rows["L-EARLY"]["source"] == "late"
    assert rows["L-AFTER"]["source"] == "late"

    # The new partition really is attached again.
    assert service.month_partition_exists(OCT) is True
    assert service.month_storage(OCT) == PART
    # No leftover build/retired tables.
    assert table_exists(f"alarm_build_{second.batch_id}") is False
    assert table_exists(f"alarm_retired_202610_{second.batch_id}") is False

    # Batch states take effect together with the new partition.
    assert service.get_batch(second.batch_id)["status"] == "attached"
    assert service.get_batch(first_id)["status"] == "superseded"


def test_reattach_identical_alarm_numbers_kept_once_as_late(db):
    build_first_month(db)
    # The same alarm number exists as a late row in BOTH the offline table
    # (L-EARLY) and, after detach, the default partition with identical
    # content.  The replacement CSV repeats it too.
    again = service.record_late("L-EARLY", t("2026-10-05T03:00:00Z"), "W1", "-18.50")
    assert again.status == "inserted"

    second = service.stage_upload(
        OCT,
        "oct-new.csv",
        csv_bytes(
            "L-EARLY,2026-10-05T03:00:00Z,W1,-18.5",
            "H-NEW-1,2026-10-03T03:30:00Z,W1,-22",
        ),
    )
    result = service.attach_batch(second.batch_id)
    assert result.ok, result.message

    rows = rows_for()
    assert len([n for n in rows if n == "L-EARLY"]) == 1
    assert rows["L-EARLY"]["source"] == "late"
    assert rows["L-EARLY"]["temperature"] == Decimal("-18.5")


def test_late_report_during_reattach_is_merged(db, monkeypatch):
    build_first_month(db)
    second = service.stage_upload(
        OCT, "oct-new.csv", csv_bytes("H-NEW-1,2026-10-03T03:30:00Z,W1,-22")
    )
    original = service._copy_rows

    def copy_then_receive_late(*args, **kwargs):
        original(*args, **kwargs)
        service.record_late("DURING", t("2026-10-07T00:00:00Z"), "W3", "-17")

    monkeypatch.setattr(service, "_copy_rows", copy_then_receive_late)
    result = service.attach_batch(second.batch_id)
    monkeypatch.setattr(service, "_copy_rows", original)

    assert result.ok, result.message
    rows = rows_for()
    assert "DURING" in rows
    assert rows["DURING"]["source"] == "late"
    assert rows["DURING"]["storage"] == PART


def test_late_reports_keep_landing_in_default_while_offline(db):
    build_first_month(db)
    one = service.record_late("L-ONE", t("2026-10-11T00:00:00Z"), "W1", "-18")
    two = service.record_late("L-TWO", t("2026-10-12T00:00:00Z"), "W1", "-18")
    assert one.status == "inserted" and two.status == "inserted"
    assert rows_for()["L-ONE"]["storage"] == "alarm_default"


# ---------------------------------------------------------------------------
# Conflict rejection: alarm numbers are reported, everything is preserved
# ---------------------------------------------------------------------------


def test_reattach_conflict_with_offline_late_names_alarm_and_keeps_data(db):
    build_first_month(db)  # offline table contains late row L-EARLY

    second = service.stage_upload(
        OCT,
        "oct-bad.csv",
        # Same alarm number as the offline late row, different temperature.
        csv_bytes(
            "L-EARLY,2026-10-05T03:00:00Z,W1,-10",
            "H-NEW-1,2026-10-03T03:30:00Z,W1,-22",
        ),
    )
    result = service.attach_batch(second.batch_id)
    assert not result.ok
    assert result.status == "rejected"
    assert "L-EARLY" in result.message
    assert [c["alarm_no"] for c in result.conflicts] == ["L-EARLY"]

    # Offline table untouched, default rows queryable, staging preserved.
    assert offline_count() == 3
    assert set(offline_rows()) == {"L-EARLY", "H-OLD-1", "H-OLD-2"}
    assert service.get_batch(second.batch_id)["staged_total"] == 2
    assert service.month_partition_exists(OCT) is False
    # The detached table's rows are invisible to the parent until re-attach.
    assert rows_for() == {}
    assert "待重挂" in service.month_storage(OCT)


def test_reattach_conflict_with_default_late_names_alarm_and_keeps_data(db):
    build_first_month(db)
    service.record_late("CLASH", t("2026-10-06T00:00:00Z"), "W1", "-18.5")
    second = service.stage_upload(
        OCT,
        "oct-bad.csv",
        csv_bytes("CLASH,2026-10-06T00:00:00Z,W2,-18.5"),  # different warehouse
    )
    result = service.attach_batch(second.batch_id)
    assert not result.ok
    assert result.status == "rejected"
    assert "CLASH" in result.message

    assert offline_count() == 3
    assert rows_for()["CLASH"]["warehouse_no"] == "W1"
    assert service.get_batch(second.batch_id)["staged_total"] == 1


def test_reattach_conflict_on_timestamp_difference(db):
    build_first_month(db)
    service.record_late("TIME-CLASH", t("2026-10-06T00:00:00Z"), "W1", "-18.5")
    second = service.stage_upload(
        OCT,
        "oct-bad.csv",
        csv_bytes("TIME-CLASH,2026-10-06T01:00:00Z,W1,-18.5"),  # different time
    )
    result = service.attach_batch(second.batch_id)
    assert not result.ok
    assert "TIME-CLASH" in result.message
    assert offline_count() == 3


def test_rejected_reattach_can_be_retried_with_fixed_csv(db):
    build_first_month(db)
    service.record_late("CLASH", t("2026-10-06T00:00:00Z"), "W1", "-18.5")
    bad = service.stage_upload(
        OCT, "oct-bad.csv", csv_bytes("CLASH,2026-10-06T00:00:00Z,W1,-10")
    )
    assert not service.attach_batch(bad.batch_id).ok

    good = service.stage_upload(
        OCT,
        "oct-fixed.csv",
        csv_bytes(
            "CLASH,2026-10-06T00:00:00Z,W1,-18.5",
            "H-NEW-1,2026-10-03T03:30:00Z,W1,-22",
        ),
    )
    result = service.attach_batch(good.batch_id)
    assert result.ok, result.message
    rows = rows_for()
    assert set(rows) == {"L-EARLY", "CLASH", "H-NEW-1"}
    assert rows["CLASH"]["source"] == "late"
    assert service.month_partition_exists(OCT) is True


# ---------------------------------------------------------------------------
# Failure handling: rollback restores the offline table and all data
# ---------------------------------------------------------------------------


def test_reattach_failure_restores_offline_table_and_all_data(db, monkeypatch):
    first_id = build_first_month(db)
    service.record_late("L-AFTER", t("2026-10-09T08:00:00Z"), "W3", "-17")
    second = service.stage_upload(
        OCT, "oct-new.csv", csv_bytes("H-NEW-1,2026-10-03T03:30:00Z,W1,-22")
    )

    original = service._copy_rows

    def explode(*args, **kwargs):
        original(*args, **kwargs)
        raise psycopg.errors.LockNotAvailable("simulated failure")

    monkeypatch.setattr(service, "_copy_rows", explode)
    result = service.attach_batch(second.batch_id)
    monkeypatch.setattr(service, "_copy_rows", original)

    assert not result.ok
    assert result.status == "failed"
    assert "回滚" in result.message

    # Offline table back under its standard name with all rows.
    assert table_exists(PART) is True
    assert service.month_partition_exists(OCT) is False
    assert offline_count() == 3
    assert set(offline_rows()) == {"L-EARLY", "H-OLD-1", "H-OLD-2"}
    # Default partition rows and staging rows preserved.  The detached
    # table's own rows stay invisible through the parent until retry.
    assert set(rows_for()) == {"L-AFTER"}
    assert service.get_batch(second.batch_id)["status"] == "staged"
    assert service.get_batch(second.batch_id)["staged_total"] == 1
    # Old batch was not touched.
    assert service.get_batch(first_id)["status"] == "attached"

    # The same batch can be attached afterwards.
    assert service.attach_batch(second.batch_id).ok
    rows = rows_for()
    assert set(rows) == {"L-EARLY", "L-AFTER", "H-NEW-1"}
    assert service.get_batch(first_id)["status"] == "superseded"


def test_reattach_without_offline_table_still_works(db):
    """First-time attach behaviour is unchanged when no offline table exists."""
    service.record_late("L-1", t("2026-10-05T03:00:00Z"), "W1", "-18.5")
    staged = service.stage_upload(
        OCT, "oct.csv", csv_bytes("H-1,2026-10-01T01:00:00Z,W1,-20")
    )
    result = service.attach_batch(staged.batch_id)
    assert result.ok
    assert result.kept_late == 1
    assert set(rows_for()) == {"L-1", "H-1"}


def test_superseded_batch_cannot_be_attached_again(db):
    first_id = build_first_month(db)
    second = service.stage_upload(
        OCT, "oct-new.csv", csv_bytes("H-NEW-1,2026-10-03T03:30:00Z,W1,-22")
    )
    assert service.attach_batch(second.batch_id).ok
    assert service.get_batch(first_id)["status"] == "superseded"

    again = service.attach_batch(first_id)
    assert not again.ok
    assert again.status == "superseded"
    # The new partition and its rows are untouched.
    assert service.month_partition_exists(OCT) is True
    assert set(rows_for()) == {"L-EARLY", "H-NEW-1"}


def test_offline_and_default_late_same_number_conflict_rejects(db):
    build_first_month(db)  # offline table holds late row L-EARLY W1/-18.5
    # A clashing late report with the same number but different content lands
    # in the default partition while the month is offline.
    service.record_late("L-EARLY", t("2026-10-05T03:00:00Z"), "W9", "-18.5")
    second = service.stage_upload(
        OCT, "oct-new.csv", csv_bytes("H-NEW-1,2026-10-03T03:30:00Z,W1,-22")
    )
    result = service.attach_batch(second.batch_id)
    assert not result.ok
    assert result.status == "rejected"
    assert "L-EARLY" in result.message
    assert offline_count() == 3
    assert service.get_batch(second.batch_id)["status"] == "rejected"
    assert service.month_partition_exists(OCT) is False


def test_offline_storage_label_without_default_rows(db):
    build_first_month(db)
    # No default-partition rows: label still points at the offline table.
    assert service.month_storage(OCT) == f"{PART}（离线，待重挂）"


def test_reattach_creates_standard_index_and_constraint_names(db):
    build_first_month(db)
    second = service.stage_upload(
        OCT, "oct-new.csv", csv_bytes("H-NEW-1,2026-10-03T03:30:00Z,W1,-22")
    )
    assert service.attach_batch(second.batch_id).ok
    with connect() as conn:
        indexes = {
            r["indexname"]
            for r in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = %s", (PART,)
            ).fetchall()
        }
        constraints = {
            r["conname"]
            for r in conn.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass",
                (PART,),
            ).fetchall()
        }
    assert f"{PART}_pkey" in indexes
    assert f"{PART}_month_check" in constraints
    # No index/constraint keeps a build-table name.
    assert not {n for n in indexes | constraints if "build" in n or "retired" in n}
