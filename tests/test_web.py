"""HTTP-level tests for the FastAPI app."""

from __future__ import annotations

import io
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import service
from app.main import app


@pytest.fixture()
def client(db):
    with TestClient(app) as test_client:
        yield test_client


OCT = date(2026, 10, 1)


def upload(client, month: str, content: bytes, filename: str = "oct.csv"):
    return client.post(
        "/upload",
        data={"month": month},
        files={"file": (filename, io.BytesIO(content), "text/csv")},
        follow_redirects=False,
    )


CSV = (
    "告警号,发生时间,库房号,温度\n"
    "H-1,2026-10-01T01:00:00Z,W1,-20\n"
    "H-2,2026-10-02 02:00:00,W2,-21\n"
).encode("utf-8")


def test_index_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "上传历史告警 CSV" in response.text


def test_upload_create_and_attach_flow(client):
    response = upload(client, "2026-10", CSV)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/batches/")

    detail = client.get(location)
    assert detail.status_code == 200
    assert "staged" in detail.text
    assert "H-1" in detail.text
    assert "2026-10" in detail.text

    batch_id = int(location.rsplit("/", 1)[1])
    attach = client.post(f"/batches/{batch_id}/attach", follow_redirects=False)
    assert attach.status_code == 303

    detail = client.get(location)
    assert "attached" in detail.text

    month = client.get("/months/2026-10")
    assert month.status_code == 200
    assert "H-1" in month.text and "H-2" in month.text
    assert "alarm_p_202610" in month.text


def test_upload_rejects_bad_month(client):
    response = upload(client, "2026-13", CSV)
    assert response.status_code == 400
    assert "月份" in response.text


def test_upload_with_bad_row_is_rejected_on_upload(client):
    bad = b"alarm_no,occurred_at,warehouse_no,temperature\nH-1,nope,W1,-20\n"
    response = upload(client, "2026-10", bad)
    assert response.status_code == 303
    detail = client.get(response.headers["location"])
    assert "rejected" in detail.text
    assert "发生时间格式无法解析" in detail.text
    assert service.month_records(OCT) == []


def test_late_report_form_and_duplicate(client):
    response = client.post(
        "/late",
        data={
            "alarm_no": "L-1",
            "occurred_at": "2026-10-05T03:00:00Z",
            "warehouse_no": "W1",
            "temperature": "-18.5",
        },
    )
    assert response.status_code == 201
    assert "补报成功" in response.text
    assert "alarm_default" in response.text

    again = client.post(
        "/late",
        data={
            "alarm_no": "L-1",
            "occurred_at": "2026-10-05T03:00:00Z",
            "warehouse_no": "W1",
            "temperature": "-18.50",
        },
    )
    assert again.status_code == 200
    assert "未重复写入" in again.text
    assert len(service.month_records(OCT)) == 1


def test_late_report_conflict_is_reported_but_kept(client):
    client.post(
        "/late",
        data={
            "alarm_no": "L-1",
            "occurred_at": "2026-10-05T03:00:00Z",
            "warehouse_no": "W1",
            "temperature": "-18.5",
        },
    )
    response = client.post(
        "/late",
        data={
            "alarm_no": "L-1",
            "occurred_at": "2026-10-05T03:00:00Z",
            "warehouse_no": "W1",
            "temperature": "-10",
        },
    )
    assert response.status_code == 200
    assert "内容不一致" in response.text
    assert len(service.month_records(OCT)) == 1


def test_late_report_invalid_temperature(client):
    response = client.post(
        "/late",
        data={
            "alarm_no": "L-1",
            "occurred_at": "2026-10-05T03:00:00Z",
            "warehouse_no": "W1",
            "temperature": "warm",
        },
    )
    assert response.status_code == 400
    assert "温度不是数字" in response.text


def test_conflicting_attach_shows_alarm_numbers_and_keeps_late_rows(client):
    client.post(
        "/late",
        data={
            "alarm_no": "CLASH",
            "occurred_at": "2026-10-05T03:00:00Z",
            "warehouse_no": "W1",
            "temperature": "-18.5",
        },
    )
    csv = b"alarm_no,occurred_at,warehouse_no,temperature\nOK-1,2026-10-02T00:00:00Z,W1,-20\nCLASH,2026-10-05T03:00:00Z,W1,-15\n"
    response = upload(client, "2026-10", csv)
    batch_id = int(response.headers["location"].rsplit("/", 1)[1])

    attach = client.post(f"/batches/{batch_id}/attach", follow_redirects=False)
    assert attach.status_code == 303
    detail = client.get(f"/batches/{batch_id}")
    assert "CLASH" in detail.text
    assert "挂接被拒绝" in detail.text

    month = client.get("/months/2026-10")
    assert "CLASH" in month.text
    assert "OK-1" not in month.text
    # Late reports still accepted after the failed attach.
    later = client.post(
        "/late",
        data={
            "alarm_no": "L-2",
            "occurred_at": "2026-10-08T00:00:00Z",
            "warehouse_no": "W2",
            "temperature": "-19",
        },
    )
    assert later.status_code == 201


def test_unknown_batch_is_404(client):
    assert client.get("/batches/99999").status_code == 404
