#!/usr/bin/env python3
"""Trial run against a running deployment.

Exercises the acceptance path end to end over HTTP:

1. a late report lands in the default partition;
2. a historical CSV is uploaded and attached, and both kinds of rows become
   visible from the parent table;
3. a later late report for the attached month goes to the new partition;
4. a conflicting upload is refused, names the alarm numbers, leaves the
   historical batch inactive and keeps accepting late reports;
5. concurrent attaches of the same month yield exactly one success;
6. same-month re-attach: after the month partition is manually DETACHed (an
   operator SQL step, mirroring the runbook), a replacement CSV is uploaded
   and attached from the batch page while late reports keep flowing; the new
   history replaces the old one and every late report — from the offline
   table, the default partition and one received mid-load — survives, with
   same-number same-content rows kept once as late reports;
7. a re-attach whose CSV clashes with an accepted late report is rejected,
   names the alarm number and leaves the offline table, default partition
   and staging data intact.

Usage::

    python scripts/trial_run.py [http://localhost:8000] [YYYY-MM]

The four months used are the given month and the three after it.  Re-run
against a fresh database (``docker compose down -v``) or pass a month that
has not been attached yet.
"""

from __future__ import annotations

import concurrent.futures
import io
import os
import sys

import psycopg
import requests

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8000"
START = sys.argv[2] if len(sys.argv) > 2 else "2026-10"
# Only used for the manual DETACH step in the re-attach scenario; falls back
# to the compose default when unset.
DATABASE_URL = os.environ.get(
    "TRIAL_DATABASE_URL", "postgresql://alarm:alarm@localhost:5432/alarms"
)


def _shift_month(month: str, offset: int) -> str:
    year, mon = (int(part) for part in month.split("-"))
    total = year * 12 + (mon - 1) + offset
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


MONTH_A = START
MONTH_B = _shift_month(START, 1)
MONTH_C = _shift_month(START, 2)
MONTH_D = _shift_month(START, 3)


def _day(month: str, day: str) -> str:
    return f"{month}-{day}"


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        raise SystemExit(1)


def upload(month: str, text: str, filename: str) -> int:
    response = requests.post(
        f"{BASE}/upload",
        data={"month": month},
        files={"file": (filename, io.BytesIO(text.encode("utf-8")), "text/csv")},
        allow_redirects=False,
        timeout=30,
    )
    response.raise_for_status()
    return int(response.headers["location"].rsplit("/", 1)[1])


def attach(batch_id: int) -> None:
    requests.post(f"{BASE}/batches/{batch_id}/attach", allow_redirects=False, timeout=60)


def late(alarm_no: str, occurred_at: str, warehouse: str, temperature: str):
    return requests.post(
        f"{BASE}/late",
        data={
            "alarm_no": alarm_no,
            "occurred_at": occurred_at,
            "warehouse_no": warehouse,
            "temperature": temperature,
        },
        timeout=30,
    )


def batch_status(batch_id: int) -> str:
    text = requests.get(f"{BASE}/batches/{batch_id}", timeout=30).text
    for status in ("attached", "rejected", "staged", "superseded"):
        if f'badge {status}"' in text:
            return status
    return "unknown"


def month_page(month: str) -> str:
    return requests.get(f"{BASE}/months/{month}", timeout=30).text


def detach_partition(month: str) -> None:
    """Mirror the operator runbook: manually DETACH the system partition."""
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(f'ALTER TABLE alarm DETACH PARTITION alarm_p_{month.replace("-", "")}')


def table_exists(name: str) -> bool:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        return conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0] is not None


def main() -> None:
    partition_a = "alarm_p_" + MONTH_A.replace("-", "")
    print(f"本次试跑使用月份：{MONTH_A} / {MONTH_B} / {MONTH_C} / {MONTH_D}")

    print(f"1. 装载前的补报落入默认分区（{MONTH_A}）")
    response = late("TRIAL-LATE", _day(MONTH_A, "03") + "T08:15:00Z", "K1", "-16.5")
    check("补报被接收", response.status_code == 201, f"HTTP {response.status_code}")
    check("写入 alarm_default", "alarm_default" in response.text)

    print("2. 上传并挂接历史批次")
    csv = (
        "告警号,发生时间,库房号,温度\n"
        f"TRIAL-HIST-1,{_day(MONTH_A, '04')}T09:00:00Z,K2,-20.5\n"
        f"TRIAL-HIST-2,{_day(MONTH_A, '05')} 10:00:00,K1,-19\n"
    )
    batch = upload(MONTH_A, csv, "trial-history.csv")
    check("批次暂存成功", batch_status(batch) == "staged")
    attach(batch)
    check("批次挂接成功", batch_status(batch) == "attached")

    page = requests.get(f"{BASE}/months/{MONTH_A}", timeout=30).text
    check("历史告警可从父表查询", "TRIAL-HIST-1" in page and "TRIAL-HIST-2" in page)
    check("已接收补报仍在父表", "TRIAL-LATE" in page)
    check("记录落在月分区", partition_a in page, partition_a)

    print("3. 挂接后的补报写入新分区")
    response = late("TRIAL-AFTER", _day(MONTH_A, "20") + "T08:15:00Z", "K1", "-17")
    check("补报被接收", response.status_code == 201)
    check(f"写入 {partition_a}", partition_a in response.text)

    print("4. 同月同号但内容有出入 -> 拒绝挂接并指出告警号")
    clash = "TRIAL-CLASH"
    late(clash, _day(MONTH_B, "02") + "T00:00:00Z", "K1", "-10")
    csv = (
        "alarm_no,occurred_at,warehouse_no,temperature\n"
        f"TRIAL-NEXT-OK,{_day(MONTH_B, '03')}T00:00:00Z,K1,-20\n"
        f"{clash},{_day(MONTH_B, '02')}T00:00:00Z,K1,-18\n"
    )
    batch = upload(MONTH_B, csv, "trial-conflict.csv")
    attach(batch)
    detail = requests.get(f"{BASE}/batches/{batch}", timeout=30).text
    check("批次被拒绝", batch_status(batch) == "rejected")
    check("报出冲突告警号", clash in detail, clash)
    month_b = requests.get(f"{BASE}/months/{MONTH_B}", timeout=30).text
    check("历史批次未生效", "TRIAL-NEXT-OK" not in month_b)
    check("已接收补报仍可查", clash in month_b)
    response = late("TRIAL-NEXT-AFTER", _day(MONTH_B, "06") + "T00:00:00Z", "K2", "-18")
    check("父表继续接收补报", response.status_code == 201)

    print("5. 同月并发挂接最多成功一次")
    ids = []
    for index in range(5):
        csv = (
            "alarm_no,occurred_at,warehouse_no,temperature\n"
            f"TRIAL-CONCURRENT-{index},{_day(MONTH_C, f'0{index + 1}')}T00:00:00Z,K1,-20\n"
        )
        ids.append(upload(MONTH_C, csv, f"trial-concurrent-{index}.csv"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ids)) as pool:
        list(pool.map(attach, ids))
    statuses = [batch_status(batch) for batch in ids]
    check("恰好一次成功", statuses.count("attached") == 1, str(statuses))
    check("其余全部退回", statuses.count("rejected") == len(ids) - 1, str(statuses))

    print("6. 同月重挂：手工 DETACH 后从批次页挂接替换 CSV")
    partition_d = "alarm_p_" + MONTH_D.replace("-", "")
    # 6.1 first attach of month D, with a late report already accepted.
    response = late("RE-LATE-OLD", _day(MONTH_D, "02") + "T01:00:00Z", "K1", "-18.5")
    check("重挂月份补报被接收", response.status_code == 201)
    first_csv = (
        "告警号,发生时间,库房号,温度\n"
        f"RE-OLD-1,{_day(MONTH_D, '03')}T09:00:00Z,K2,-20.5\n"
        f"RE-OLD-2,{_day(MONTH_D, '04')} 10:00:00,K1,-19\n"
    )
    first_batch = upload(MONTH_D, first_csv, "re-old.csv")
    attach(first_batch)
    check("首次挂接成功", batch_status(first_batch) == "attached")
    check("首挂分区存在", table_exists(partition_d))

    # 6.2 operator manually detaches the system-generated partition.
    detach_partition(MONTH_D)
    check("DETACH 后旧月表仍在", table_exists(partition_d))
    page = month_page(MONTH_D)
    check("月份页提示离线待重挂", "待重挂" in page)
    check("离线表历史已不可从父表查询", "RE-OLD-1" not in page)

    # 6.3 late reports keep landing in the default partition while offline.
    response = late("RE-LATE-NEW", _day(MONTH_D, "08") + "T08:15:00Z", "K3", "-17")
    check("离线期间补报照常接收", response.status_code == 201)
    check("离线补报写入默认分区", "alarm_default" in response.text)

    # 6.4 upload the replacement CSV and attach from the batch page.
    second_csv = (
        "alarm_no,occurred_at,warehouse_no,temperature\n"
        # Same alarm number/content as the old late row: kept once, as late.
        f"RE-LATE-OLD,{_day(MONTH_D, '02')}T01:00:00Z,K1,-18.50\n"
        f"RE-NEW-1,{_day(MONTH_D, '05')}T03:30:00Z,K1,-22\n"
        f"RE-NEW-2,{_day(MONTH_D, '06')}T04:40:00Z,K2,-23\n"
    )
    second_batch = upload(MONTH_D, second_csv, "re-new.csv")
    check("替换批次暂存成功", batch_status(second_batch) == "staged")
    # One more late report during the load window.
    response = late("RE-LATE-LOAD", _day(MONTH_D, "09") + "T00:00:00Z", "K4", "-16")
    check("装载期间补报照常接收", response.status_code == 201)
    attach(second_batch)
    check("重挂批次成功", batch_status(second_batch) == "attached")
    check("旧批次被标记为 superseded", batch_status(first_batch) == "superseded")

    page = month_page(MONTH_D)
    check("新 CSV 历史告警可查", "RE-NEW-1" in page and "RE-NEW-2" in page)
    check("旧历史已由新批替换", "RE-OLD-1" not in page and "RE-OLD-2" not in page)
    check("离线月表中的补报恢复", "RE-LATE-OLD" in page)
    check("默认分区补报保留", "RE-LATE-NEW" in page)
    check("装载期间补报保留", "RE-LATE-LOAD" in page)
    check("同月同号同内容只留一条", page.count("RE-LATE-OLD") == 1)
    check("记录全部落在新月分区", partition_d in page)
    check("重挂后无残留构建表", table_exists(f"alarm_build_{second_batch}") is False)

    # 6.5 late reports after re-attach go straight to the new partition.
    response = late("RE-LATE-AFTER", _day(MONTH_D, "20") + "T08:15:00Z", "K1", "-17")
    check("重挂后补报写入新分区", response.status_code == 201 and partition_d in response.text)

    print("7. 重挂撞号退回：保留离线月表、默认分区与批次暂存")
    # Month B's earlier attach was rejected (scenario 4), so give it a clean
    # first attach now, then detach it to create the offline table.
    partition_b = "alarm_p_" + MONTH_B.replace("-", "")
    setup_csv = (
        "alarm_no,occurred_at,warehouse_no,temperature\n"
        f"REB-OLD-1,{_day(MONTH_B, '11')}T00:00:00Z,K1,-20\n"
    )
    setup_batch = upload(MONTH_B, setup_csv, "re-b-setup.csv")
    attach(setup_batch)
    check("月 B 首次挂接成功", batch_status(setup_batch) == "attached")
    detach_partition(MONTH_B)
    check("月 B 旧月表离线待重挂", table_exists(partition_b))
    clash = "RE-CLASH"
    late(clash, _day(MONTH_B, "21") + "T00:00:00Z", "K1", "-18.5")
    bad_csv = (
        "alarm_no,occurred_at,warehouse_no,temperature\n"
        f"{clash},{_day(MONTH_B, '21')}T00:00:00Z,K1,-10\n"
    )
    bad_batch = upload(MONTH_B, bad_csv, "re-clash.csv")
    attach(bad_batch)
    detail = requests.get(f"{BASE}/batches/{bad_batch}", timeout=30).text
    check("撞号批次被退回", batch_status(bad_batch) == "rejected")
    check("报出冲突告警号", clash in detail, clash)
    check("离线月表未被破坏", table_exists(partition_b) is True)
    page = month_page(MONTH_B)
    check("默认分区补报仍可查", clash in page)
    check("月份页仍提示离线待重挂", "待重挂" in page)
    check("冲突 CSV 未生效", "re-clash" not in page)

    print("\n全部检查通过。")


if __name__ == "__main__":
    main()
