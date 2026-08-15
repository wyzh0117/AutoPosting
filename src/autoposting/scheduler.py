"""In-process scheduled publication runner for the local web service."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from .db import Database


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
    ) -> None:
        self.database = database
        self.publisher = publisher
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

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
            except Exception:
                continue

    def tick(self, now: datetime | None = None) -> list[dict[str, object]]:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        due = self.database.due_scheduled_jobs(moment.isoformat())
        results: list[dict[str, object]] = []
        for job in due:
            results.append(self._run_job(job))
        return results

    def _run_job(self, job: dict[str, object]) -> dict[str, object]:
        job_id = str(job["job_id"])
        with self._lock:
            claimed = self.database.claim_scheduled_job(job_id)
            if not claimed:
                return {"job_id": job_id, "status": "SKIPPED"}
            try:
                outcome = self.publisher(
                    str(job["draft_id"]),
                    str(job["platform"]),
                    bool(job["live"]),
                )
            except Exception as exc:
                self.database.finish_scheduled_job(job_id, "FAILED", error_message=str(exc))
                return {"job_id": job_id, "status": "FAILED", "error": str(exc)}
            self.database.finish_scheduled_job(
                job_id,
                "DONE",
                result_json=json.dumps(outcome, ensure_ascii=False, default=str),
            )
            return {"job_id": job_id, "status": "DONE", "result": outcome}
