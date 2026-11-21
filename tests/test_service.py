"""End-to-end service tests against a real PostgreSQL instance."""

from __future__ import annotations

import threading
from datetime import date, datetime
from decimal import Decimal

import psycopg

from app import service
from app.db import connect

OCT = date(2026, 10, 1)
NOV = date(2026, 11, 1)


def csv_bytes(*rows: str) -> bytes:
    header = "alarm_no,occurred_at,warehouse_no,temperature\n"
    return (header + "".join(row + "\n" for row in rows)).encode()


def t(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def storage_of(alarm_no: str, month: date = OCT) -> str:
    with connect() as conn:
        return conn.execute(
            "SELECT tableoid::regclass::text AS s FROM alarm"
            " WHERE occurred_month = %s AND alarm_no = %s",
            (month, alarm_no),
        ).fetchone()["s"]


def rows_for(month: date = OCT) -> dict[str, dict]:
    return {r["alarm_no"]: r for r in service.month_records(month)}


# ---------------------------------------------------------------------------
# Late reports land in the default partition
# ---------------------------------------------------------------------------


def test_late_report_lands_in_default_partition(db):
    result = service.record_late("L-1", t("2026-10-05T03:00:00Z"), "W1", "-18.5")
    assert result.status == "inserted"
    assert storage_of("L-1") == "alarm_default"


def test_late_report_duplicate_ignored_and_conflict_reported(db):
    first = service.record_late("L-1", t("2026-10-05T03:00:00Z"), "W1", "-18.5")
    same = service.record_late("L-1", t("2026-10-05T03:00:00Z"), "W1", "-18.50")
    assert first.status == "inserted"
    assert same.status == "duplicate"
    assert len(rows_for()) == 1

    changed = service.record_late("L-1", t("2026-10-05T03:00:00Z"), "W1", "-15")
    assert changed.status == "conflict"
    assert "L-1" in changed.detail


def test_same_alarm_number_in_different_months_is_allowed(db):
    service.record_late("SAME", t("2026-10-05T03:00:00Z"), "W1", "-18")
    service.record_late("SAME", t("2026-11-05T03:00:00Z"), "W1", "-18")
    assert len(service.month_records(OCT)) == 1
    assert len(service.month_records(NOV)) == 1


# ---------------------------------------------------------------------------
# Attach happy path
# ---------------------------------------------------------------------------


def test_attach_makes_history_and_late_rows_visible_from_parent(db):
    late = service.record_late("L-1", t("2026-10-05T03:00:00Z"), "W1", "-18.5")
    assert late.status == "inserted"

    staged = service.stage_upload(
        OCT,
        "oct.csv",
        csv_bytes(
            "H-1,2026-10-01T01:00:00Z,W1,-20",
            "H-2,2026-10-02T02:00:00Z,W2,-21",
        ),
    )
    assert staged.status == "staged"
    assert staged.row_count == 2

    result = service.attach_batch(staged.batch_id)
    assert result.ok, result.message
    assert result.inserted_history == 2
    assert result.kept_late == 1

    rows = rows_for()
    assert set(rows) == {"L-1", "H-1", "H-2"}
    assert rows["L-1"]["source"] == "late"
    assert rows["H-1"]["source"] == "history"
    assert all(r["storage"] == "alarm_p_202610" for r in rows.values())


def test_late_report_after_attach_goes_to_month_partition(db):
    staged = service.stage_upload(OCT, "oct.csv", csv_bytes("H-1,2026-10-01T01:00:00Z,W1,-20"))
    assert service.attach_batch(staged.batch_id).ok

    after = service.record_late("L-2", t("2026-10-20T03:00:00Z"), "W2", "-19")
    assert after.status == "inserted"
    assert storage_of("L-2") == "alarm_p_202610"
    assert "alarm_p_202610" in after.detail


def test_other_months_keep_using_default_partition(db):
    staged = service.stage_upload(OCT, "oct.csv", csv_bytes("H-1,2026-10-01T01:00:00Z,W1,-20"))
    assert service.attach_batch(staged.batch_id).ok

    service.record_late("NOV-1", t("2026-11-02T00:00:00Z"), "W1", "-18")
    assert storage_of("NOV-1", NOV) == "alarm_default"


def test_duplicate_inside_csv_kept_once(db):
    staged = service.stage_upload(
        OCT,
        "oct.csv",
        csv_bytes(
            "H-1,2026-10-01T01:00:00Z,W1,-20",
            "H-1,2026-10-01T01:00:00Z,W1,-20.0",
        ),
    )
    assert staged.status == "staged"
    result = service.attach_batch(staged.batch_id)
    assert result.ok
    rows = service.month_records(OCT)
    assert len(rows) == 1
    assert rows[0]["temperature"] == Decimal("-20")


def test_late_row_identical_to_history_is_not_duplicated(db):
    service.record_late("H-1", t("2026-10-01T01:00:00Z"), "W1", "-20")
    staged = service.stage_upload(OCT, "oct.csv", csv_bytes("H-1,2026-10-01T01:00:00Z,W1,-20.00"))
    result = service.attach_batch(staged.batch_id)
    assert result.ok
    rows = service.month_records(OCT)
    assert len(rows) == 1
    assert rows[0]["source"] == "late"


# ---------------------------------------------------------------------------
# Attach conflicts
# ---------------------------------------------------------------------------


def test_conflicting_late_report_rejects_attach_and_reports_alarm_number(db):
    service.record_late("CLASH", t("2026-10-05T03:00:00Z"), "W1", "-18.5")
    staged = service.stage_upload(
        OCT,
        "oct.csv",
        csv_bytes(
            "OK-1,2026-10-02T00:00:00Z,W1,-20",
            "CLASH,2026-10-05T03:00:00Z,W1,-15",
        ),
    )

    result = service.attach_batch(staged.batch_id)
    assert not result.ok
    assert result.status == "rejected"
    assert "CLASH" in result.message
    assert [c["alarm_no"] for c in result.conflicts] == ["CLASH"]

    # Historical batch did not take effect at all.
    assert set(rows_for()) == {"CLASH"}
    assert storage_of("CLASH") == "alarm_default"

    batch = service.get_batch(staged.batch_id)
    assert batch["status"] == "rejected"
    assert batch["conflict_json"]["conflicts"][0]["alarm_no"] == "CLASH"


def test_conflict_inside_csv_rejects_whole_batch(db):
    staged = service.stage_upload(
        OCT,
        "oct.csv",
        csv_bytes(
            "DUP,2026-10-02T00:00:00Z,W1,-20",
            "DUP,2026-10-02T00:00:00Z,W1,-21",
        ),
    )
    result = service.attach_batch(staged.batch_id)
    assert not result.ok
    assert "DUP" in result.message
    assert service.month_records(OCT) == []


def test_after_rejected_attach_late_reports_still_accepted(db):
    service.record_late("CLASH", t("2026-10-05T03:00:00Z"), "W1", "-18.5")
    staged = service.stage_upload(OCT, "oct.csv", csv_bytes("CLASH,2026-10-05T03:00:00Z,W1,-15"))
    assert not service.attach_batch(staged.batch_id).ok

    later = service.record_late("NEW-1", t("2026-10-09T00:00:00Z"), "W2", "-19")
    assert later.status == "inserted"
    assert storage_of("NEW-1") == "alarm_default"
    assert set(rows_for()) == {"CLASH", "NEW-1"}


def test_corrected_upload_can_be_attached_after_rejection(db):
    service.record_late("CLASH", t("2026-10-05T03:00:00Z"), "W1", "-18.5")
    bad = service.stage_upload(OCT, "oct.csv", csv_bytes("CLASH,2026-10-05T03:00:00Z,W1,-15"))
    assert not service.attach_batch(bad.batch_id).ok

    good = service.stage_upload(OCT, "oct-fixed.csv", csv_bytes("CLASH,2026-10-05T03:00:00Z,W1,-18.5"))
    assert service.attach_batch(good.batch_id).ok
    assert storage_of("CLASH") == "alarm_p_202610"


def test_second_attach_for_same_month_is_rejected(db):
    first = service.stage_upload(OCT, "a.csv", csv_bytes("A-1,2026-10-01T00:00:00Z,W1,-20"))
    second = service.stage_upload(OCT, "b.csv", csv_bytes("B-1,2026-10-02T00:00:00Z,W1,-20"))
    assert service.attach_batch(first.batch_id).ok
    result = service.attach_batch(second.batch_id)
    assert not result.ok
    assert result.status == "rejected"
    assert "已存在月分区" in result.message
    assert service.get_batch(second.batch_id)["status"] == "rejected"
    assert set(rows_for()) == {"A-1"}


def test_rejected_csv_rows_never_reach_staging(db):
    result = service.stage_upload(
        OCT,
        "bad.csv",
        csv_bytes("A-1,not-a-time,W1,-20", "A-2,2026-10-01T00:00:00Z,W1,-20"),
    )
    assert result.status == "rejected"
    batch = service.get_batch(result.batch_id)
    assert batch["staged_total"] == 0
    assert batch["conflict_json"]["row_errors"][0]["line"] == 2


def test_rows_outside_selected_month_are_rejected(db):
    result = service.stage_upload(OCT, "wrong.csv", csv_bytes("A-1,2026-11-01T00:00:00Z,W1,-20"))
    assert result.status == "rejected"
    assert "不在所选 UTC 月份" in result.errors[0]["message"]


# ---------------------------------------------------------------------------
# Failure handling: attach rolls back cleanly
# ---------------------------------------------------------------------------


def test_attach_failure_rolls_back_and_parent_keeps_working(db, monkeypatch):
    service.record_late("L-1", t("2026-10-05T03:00:00Z"), "W1", "-18.5")
    staged = service.stage_upload(OCT, "oct.csv", csv_bytes("H-1,2026-10-01T00:00:00Z,W1,-20"))

    original = service._copy_rows

    def explode(*args, **kwargs):
        original(*args, **kwargs)
        raise psycopg.errors.LockNotAvailable("simulated failure")

    monkeypatch.setattr(service, "_copy_rows", explode)
    result = service.attach_batch(staged.batch_id)
    assert not result.ok
    assert result.status == "failed"
    assert "回滚" in result.message

    monkeypatch.setattr(service, "_copy_rows", original)

    # No month partition, no history rows, batch still staged and retryable.
    assert service.month_partition_exists(OCT) is False
    assert set(rows_for()) == {"L-1"}
    assert storage_of("L-1") == "alarm_default"
    assert service.get_batch(staged.batch_id)["status"] == "staged"

    # Late reports still work.
    later = service.record_late("L-2", t("2026-10-06T00:00:00Z"), "W2", "-19")
    assert later.status == "inserted"

    # And the same batch can be attached afterwards.
    assert service.attach_batch(staged.batch_id).ok
    assert set(rows_for()) == {"L-1", "L-2", "H-1"}


def test_concurrent_attach_same_month_only_one_succeeds(db):
    batch_ids = []
    for index in range(6):
        staged = service.stage_upload(
            OCT,
            f"b{index}.csv",
            csv_bytes(f"A-{index},2026-10-0{index + 1}T00:00:00Z,W1,-20"),
        )
        batch_ids.append(staged.batch_id)

    barrier = threading.Barrier(len(batch_ids))
    results: list[service.AttachResult] = []
    lock = threading.Lock()

    def worker(batch_id: int) -> None:
        barrier.wait()
        outcome = service.attach_batch(batch_id)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(bid,)) for bid in batch_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [r for r in results if r.ok]
    assert len(winners) == 1
    attached = [
        b for b in (service.get_batch(bid) for bid in batch_ids) if b["status"] == "attached"
    ]
    assert len(attached) == 1
    # Only the winning batch's CSV row is visible.
    assert len(service.month_records(OCT)) == 1


def test_late_reports_continue_during_staging(db):
    """Staging a batch does not block late reports for the month."""
    staged = service.stage_upload(OCT, "oct.csv", csv_bytes("H-1,2026-10-01T00:00:00Z,W1,-20"))
    assert staged.status == "staged"
    assert service.record_late("L-1", t("2026-10-02T00:00:00Z"), "W1", "-18").status == "inserted"
    assert set(rows_for()) == {"L-1"}


def test_late_report_during_attach_is_merged_into_new_partition(db, monkeypatch):
    """A late report accepted while the bulk copy runs must survive the attach."""
    staged = service.stage_upload(OCT, "oct.csv", csv_bytes("H-1,2026-10-01T00:00:00Z,W1,-20"))
    original = service._copy_rows

    def copy_then_receive_late(*args, **kwargs):
        original(*args, **kwargs)
        # Simulates a late report committed by another session mid-attach.
        service.record_late("DURING", t("2026-10-07T00:00:00Z"), "W3", "-17")

    monkeypatch.setattr(service, "_copy_rows", copy_then_receive_late)
    result = service.attach_batch(staged.batch_id)
    monkeypatch.setattr(service, "_copy_rows", original)

    assert result.ok, result.message
    rows = rows_for()
    assert set(rows) == {"H-1", "DURING"}
    assert rows["DURING"]["source"] == "late"
    assert rows["DURING"]["storage"] == "alarm_p_202610"
    assert storage_of("DURING") == "alarm_p_202610"


def test_late_report_conflicting_during_attach_rejects_batch(db, monkeypatch):
    """A differing late report for a CSV alarm, arriving mid-attach, rejects it."""
    staged = service.stage_upload(OCT, "oct.csv", csv_bytes("CLASH,2026-10-05T00:00:00Z,W1,-18.5"))
    original = service._copy_rows

    def copy_then_conflict(*args, **kwargs):
        original(*args, **kwargs)
        # Another session accepts a late report for the same alarm while the
        # batch is being copied, with different content than the CSV.
        service.record_late("CLASH", t("2026-10-05T00:00:00Z"), "W1", "-10")

    monkeypatch.setattr(service, "_copy_rows", copy_then_conflict)
    result = service.attach_batch(staged.batch_id)
    monkeypatch.setattr(service, "_copy_rows", original)

    assert not result.ok
    assert result.status == "rejected"
    assert "CLASH" in result.message
    assert service.month_partition_exists(OCT) is False
    # The mid-attach late report is the only row and stays queryable.
    rows = rows_for()
    assert set(rows) == {"CLASH"}
    assert rows["CLASH"]["temperature"] == Decimal("-10")
    assert storage_of("CLASH") == "alarm_default"


def test_merge_helper_reports_content_conflict(db):
    with connect() as conn:
        conn.execute("DROP TABLE IF EXISTS merge_probe")
        conn.execute("CREATE TABLE merge_probe (LIKE alarm INCLUDING ALL)")
        conn.execute(
            "INSERT INTO merge_probe (alarm_no, occurred_at, warehouse_no, temperature,"
            " occurred_month) VALUES ('M-1', %s, 'W1', -20, %s)",
            (t("2026-10-01T00:00:00Z"), OCT),
        )
        conflicts = service._merge_new_late_rows(
            conn,
            "merge_probe",
            [
                {"alarm_no": "M-1", "occurred_at": t("2026-10-01T00:00:00Z"),
                 "warehouse_no": "W1", "temperature": Decimal("-20")},
                {"alarm_no": "M-1", "occurred_at": t("2026-10-01T00:00:00Z"),
                 "warehouse_no": "W1", "temperature": Decimal("-5")},
                {"alarm_no": "M-2", "occurred_at": t("2026-10-02T00:00:00Z"),
                 "warehouse_no": "W2", "temperature": Decimal("-21")},
            ],
        )
    assert [c["alarm_no"] for c in conflicts] == ["M-1"]


def test_month_summary_counts(db):
    service.record_late("L-1", t("2026-10-05T03:00:00Z"), "W1", "-18.5")
    staged = service.stage_upload(
        OCT, "oct.csv", csv_bytes("H-1,2026-10-01T00:00:00Z,W1,-20", "H-2,2026-10-02T00:00:00Z,W1,-21")
    )
    assert service.attach_batch(staged.batch_id).ok
    summary = {row["month"]: row for row in service.month_summary()}
    assert summary[OCT]["total"] == 3
    assert summary[OCT]["late_count"] == 1
    assert summary[OCT]["history_count"] == 2
