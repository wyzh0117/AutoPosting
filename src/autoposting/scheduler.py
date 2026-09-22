"""In-process scheduled publication runner for the local web service."""

from __future__ import annotations

import json
import logging
import threading
import traceback
from datetime import datetime, timedelta, timezone
from typing import Callable

from .db import Database


_LOG = logging.getLogger(__name__)


def _safe_traceback(exc: BaseException) -> str:
    """Return stack frames without copying an exception's possibly sensitive text."""
    frames = "".join(
        f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n'
        for frame in traceback.extract_tb(exc.__traceback__)
    )
    if not frames:
        frames = "<no traceback>\n"
    return f"Traceback (most recent call last):\n{frames}{type(exc).__name__}: [redacted]"


def _log_scheduler_exception(job_id: str, stage: str, exc: BaseException) -> None:
    # logger.exception would include str(exc), which may contain a token,
    # AppSecret, or an external authorization URL.
    _LOG.error(
        "scheduled job failed job_id=%s stage=%s exception_type=%s traceback=%s",
        job_id,
        stage,
        type(exc).__name__,
        _safe_traceback(exc),
    )


def parse_run_at(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("run_at required")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("run_at must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone(timezone.utc).isoformat()


class PublishScheduler:
    def __init__(
        self,
        database: Database,
        publisher: Callable[[str, str, bool], dict[str, object]],
        interval_seconds: float = 15.0,
        stale_after_seconds: float | None = None,
    ) -> None:
        self.database = database
        self.publisher = publisher
        self.interval_seconds = interval_seconds
        self.stale_after_seconds = (
            max(60.0, 4.0 * interval_seconds)
            if stale_after_seconds is None
            else float(stale_after_seconds)
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # This serializes the local publisher/finish path; it is not a
        # distributed lease or retry mechanism.
        self._lock = threading.Lock()
        self._active_job_lock = threading.Lock()
        self._active_job_ids: set[str] = set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="autoposting-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.tick()
            except Exception as exc:
                _log_scheduler_exception("<tick>", "tick", exc)
                continue

    def tick(self, now: datetime | None = None) -> list[dict[str, object]]:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        else:
            moment = moment.astimezone(timezone.utc)
        cutoff = (moment - timedelta(seconds=self.stale_after_seconds)).isoformat()
        with self._active_job_lock:
            active_job_ids = set(self._active_job_ids)
        try:
            repaired = self.database.reconcile_stale_running_jobs(
                cutoff,
                active_job_ids=active_job_ids,
            )
        except Exception as exc:
            _log_scheduler_exception("<reconcile>", "stale_reconcile", exc)
        else:
            for job_id in repaired:
                _LOG.warning(
                    "scheduled job repaired job_id=%s stage=stale_reconcile "
                    "error_code=scheduler_finish_failed",
                    job_id,
                )
        due = self.database.due_scheduled_jobs(moment.isoformat())
        results: list[dict[str, object]] = []
        for job in due:
            job_id = str(job.get("job_id") or "<unknown>")
            try:
                results.append(self._run_job(job))
            except Exception as exc:
                _log_scheduler_exception(job_id, "job", exc)
                self._finish_failed(job_id, "scheduler_error")
                results.append({"job_id": job_id, "status": "FAILED", "error": "scheduler_error"})
        return results

    def _finish_failed(self, job_id: str, error_code: str) -> bool:
        try:
            finished = self.database.finish_scheduled_job(
                job_id,
                "FAILED",
                error_message=error_code,
            )
        except Exception as exc:
            _log_scheduler_exception(job_id, "finish_failed", exc)
            return False
        if not finished:
            _LOG.warning(
                "scheduled job finish ignored job_id=%s stage=finish "
                "error_code=late_finish_ignored",
                job_id,
            )
        return finished

    def _run_job(self, job: dict[str, object]) -> dict[str, object]:
        job_id = str(job["job_id"])
        with self._lock:
            try:
                claimed = self.database.claim_scheduled_job(job_id)
            except Exception as exc:
                _log_scheduler_exception(job_id, "claim", exc)
                return {"job_id": job_id, "status": "FAILED", "error": "scheduler_claim_failed"}
            if not claimed:
                return {"job_id": job_id, "status": "SKIPPED"}
            with self._active_job_lock:
                self._active_job_ids.add(job_id)
            try:
                try:
                    outcome = self.publisher(
                        str(job["draft_id"]),
                        str(job["platform"]),
                        bool(job["live"]),
                    )
                except Exception as exc:
                    _log_scheduler_exception(job_id, "publisher", exc)
                    self._finish_failed(job_id, "publisher_failed")
                    return {"job_id": job_id, "status": "FAILED", "error": "publisher_failed"}
                if outcome is not None and not isinstance(outcome, dict):
                    exc = TypeError("scheduled result must be a JSON object or null")
                    _log_scheduler_exception(job_id, "validate_result", exc)
                    self._finish_failed(job_id, "result_not_json_object")
                    return {
                        "job_id": job_id,
                        "status": "FAILED",
                        "error": "result_not_json_object",
                    }
                try:
                    result_json = json.dumps(
                        outcome,
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                except (TypeError, ValueError) as exc:
                    _log_scheduler_exception(job_id, "serialize_result", exc)
                    self._finish_failed(job_id, "result_not_json_serializable")
                    return {
                        "job_id": job_id,
                        "status": "FAILED",
                        "error": "result_not_json_serializable",
                    }
                try:
                    finished = self.database.finish_scheduled_job(
                        job_id,
                        "DONE",
                        result_json=result_json,
                    )
                except Exception as exc:
                    _log_scheduler_exception(job_id, "finish", exc)
                    return {
                        "job_id": job_id,
                        "status": "FAILED",
                        "error": "scheduler_finish_failed",
                    }
                if not finished:
                    _LOG.warning(
                        "scheduled job finish ignored job_id=%s stage=finish "
                        "error_code=late_finish_ignored",
                        job_id,
                    )
                    return {"job_id": job_id, "status": "FAILED", "error": "scheduler_finish_failed"}
                return {"job_id": job_id, "status": "DONE", "result": outcome}
            finally:
                with self._active_job_lock:
                    self._active_job_ids.discard(job_id)
