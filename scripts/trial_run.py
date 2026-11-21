#!/usr/bin/env python3
"""Trial run against a running deployment.

Exercises the acceptance path end to end over HTTP:

1. a late report lands in the default partition;
2. a historical CSV is uploaded and attached, and both kinds of rows become
   visible from the parent table;
3. a later late report for the attached month goes to the new partition;
4. a conflicting upload is refused, names the alarm numbers, leaves the
   historical batch inactive and keeps accepting late reports;
5. concurrent attaches of the same month yield exactly one success.

Usage::

    python scripts/trial_run.py [http://localhost:8000] [YYYY-MM]

The three months used are the given month and the two after it.  Re-run
against a fresh database (``docker compose down -v``) or pass a month that
has not been attached yet.
"""

from __future__ import annotations

import concurrent.futures
import io
import sys

import requests

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8000"
START = sys.argv[2] if len(sys.argv) > 2 else "2026-10"


def _shift_month(month: str, offset: int) -> str:
    year, mon = (int(part) for part in month.split("-"))
    total = year * 12 + (mon - 1) + offset
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


MONTH_A = START
MONTH_B = _shift_month(START, 1)
MONTH_C = _shift_month(START, 2)


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
    for status in ("attached", "rejected", "staged"):
        if f'badge {status}"' in text:
            return status
    return "unknown"


def main() -> None:
    partition_a = "alarm_p_" + MONTH_A.replace("-", "")
    print(f"本次试跑使用月份：{MONTH_A} / {MONTH_B} / {MONTH_C}")

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

    print("\n全部检查通过。")


if __name__ == "__main__":
    main()
