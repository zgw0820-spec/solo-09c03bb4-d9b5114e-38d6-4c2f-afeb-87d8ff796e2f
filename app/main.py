"""FastAPI web application for the alarm archive rebuild."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import service
from .csv_import import month_bounds, parse_month
from .db import init_schema

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

MAX_UPLOAD_BYTES = 32 * 1024 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_schema()
    yield


app = FastAPI(title="冷库超温告警历史库重建", lifespan=lifespan)


def _default_month() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "batches": service.list_batches(),
            "months": service.month_summary(),
            "default_month": _default_month(),
        },
    )


@app.post("/upload")
async def upload(
    request: Request,
    month: str = Form(...),
    file: UploadFile = Form(...),
):
    try:
        month_start = parse_month(month)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": "CSV 文件超过 32MB 限制"},
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    # Parsing and the staging COPY are blocking (CPU + psycopg).  Run them in
    # the threadpool so late reports and month queries are not held up on the
    # event loop while a large CSV is being loaded.
    result = await run_in_threadpool(
        service.stage_upload, month_start, file.filename or "upload.csv", raw
    )
    return RedirectResponse(
        url=f"/batches/{result.batch_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@app.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail(request: Request, batch_id: int):
    batch = service.get_batch(batch_id)
    if batch is None:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": f"批次 {batch_id} 不存在"},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    conflicts = (batch.get("conflict_json") or {}).get("conflicts", [])
    row_errors = (batch.get("conflict_json") or {}).get("row_errors", [])
    return templates.TemplateResponse(
        request,
        "batch.html",
        {
            "batch": batch,
            "conflicts": conflicts,
            "row_errors": row_errors,
        },
    )


@app.post("/batches/{batch_id}/attach")
async def attach_batch(batch_id: int):
    # Attaching a large month partition is a blocking DB operation.
    await run_in_threadpool(service.attach_batch, batch_id)
    return RedirectResponse(
        url=f"/batches/{batch_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@app.get("/months/{month}", response_class=HTMLResponse)
def month_view(request: Request, month: str):
    try:
        month_start = parse_month(month)
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "error.html", {"message": str(exc)}, status_code=400
        )
    _, month_end = month_bounds(month_start)
    return templates.TemplateResponse(
        request,
        "month.html",
        {
            "month": month_start,
            "month_end": month_end,
            "records": service.month_records(month_start),
            "storage": service.month_storage(month_start),
        },
    )


@app.get("/late", response_class=HTMLResponse)
def late_form(request: Request):
    return templates.TemplateResponse(
        request,
        "late.html",
        {
            "now": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
            "result": None,
        },
    )


@app.post("/late", response_class=HTMLResponse)
def late_submit(
    request: Request,
    alarm_no: str = Form(...),
    occurred_at: str = Form(...),
    warehouse_no: str = Form(...),
    temperature: str = Form(...),
):
    context = {"now": occurred_at, "result": None, "form": {
        "alarm_no": alarm_no,
        "occurred_at": occurred_at,
        "warehouse_no": warehouse_no,
        "temperature": temperature,
    }}
    try:
        parsed = _parse_form_time(occurred_at)
        result = service.record_late(alarm_no, parsed, warehouse_no, temperature)
    except (ValueError, ArithmeticError) as exc:
        context["result"] = {"status": "error", "detail": str(exc), "alarm_no": alarm_no}
        return templates.TemplateResponse(
            request, "late.html", context, status_code=status.HTTP_400_BAD_REQUEST
        )
    context["result"] = {
        "status": result.status,
        "detail": result.detail,
        "alarm_no": result.alarm_no,
    }
    code = status.HTTP_201_CREATED if result.status == "inserted" else status.HTTP_200_OK
    return templates.TemplateResponse(request, "late.html", context, status_code=code)


def _parse_form_time(value: str) -> datetime:
    text = (value or "").strip()
    if not text:
        raise ValueError("发生时间为空")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
