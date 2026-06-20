"""Threaded pipeline runner that backs the web dashboard.

Wraps the same scrape -> screenshot -> download -> transcribe -> summarize ->
log flow used by main.py, but exposes it as a controllable, observable object:

  * start()  launches the pipeline on a background thread (own asyncio loop).
  * stop()   requests a cooperative stop (finishes the current video, then halts).
  * snapshot() returns a thread-safe view of state/progress/logs/results for the UI.

The runner never raises into the caller: per-video failures are logged and the
loop continues, matching the CLI behavior.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from pathlib import Path

import config
from excel_writer import ExcelLogger
from processor import process_short
from scraper import (
    ShortsScraper,
    extract_video_id,
    human_delay,
    normalize_url_list,
)

logger = logging.getLogger(__name__)

_MAX_LOG_LINES = 500


class PipelineRunner:
    """Owns a single pipeline run at a time and tracks its live state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        # All fields below are guarded by self._lock.
        self.state: str = "idle"  # idle | running | stopping | done | error
        self.total: int = 0
        self.processed: int = 0
        self.results: list[dict] = []
        self.logs: list[dict] = []
        self.error: str | None = None
        self.source_label: str = ""
        self.started_at: str | None = None
        self.finished_at: str | None = None

    # -- introspection ------------------------------------------------------
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self) -> dict:
        """Thread-safe copy of current state for the dashboard."""
        with self._lock:
            return {
                "state": self.state,
                "running": self.is_running(),
                "stop_requested": self._stop.is_set(),
                "total": self.total,
                "processed": self.processed,
                "source_label": self.source_label,
                "results": list(self.results),
                "logs": self.logs[-_MAX_LOG_LINES:],
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }

    def _log(self, msg: str, level: str = "info") -> None:
        with self._lock:
            self.logs.append(
                {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "level": level,
                    "msg": msg,
                }
            )
            if len(self.logs) > _MAX_LOG_LINES * 2:
                self.logs = self.logs[-_MAX_LOG_LINES:]

    # -- control ------------------------------------------------------------
    def start(
        self,
        source_type: str,
        source_value: str,
        max_count: int,
        headless: bool = True,
        summary_language: str | None = None,
        content_type: str = "shorts",
    ) -> bool:
        """Begin a run. Returns False if one is already in progress."""
        if self.is_running():
            return False

        ctype = "videos" if content_type == "videos" else "shorts"
        with self._lock:
            self.state = "running"
            self.total = 0
            self.processed = 0
            self.results = []
            self.logs = []
            self.error = None
            label_kind = "videos" if ctype == "videos" else "shorts"
            self.source_label = f"{source_type} ({label_kind}): {source_value}"
            self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.finished_at = None

        self._stop.clear()
        config.HEADLESS = headless
        language = summary_language or config.DEFAULT_SUMMARY_LANGUAGE

        self._thread = threading.Thread(
            target=self._run_thread,
            args=(source_type, source_value, max_count, language, ctype),
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> bool:
        """Request a cooperative stop. Returns False if nothing is running."""
        if not self.is_running():
            return False
        self._stop.set()
        with self._lock:
            if self.state == "running":
                self.state = "stopping"
        self._log("Stop requested — halting after the current video.", "warn")
        return True

    def reset(self) -> bool:
        """Clear all in-memory state (logs, results, counters) back to idle.

        Returns False if a run is in progress (reset is refused while running).
        """
        if self.is_running():
            return False
        with self._lock:
            self.state = "idle"
            self.total = 0
            self.processed = 0
            self.results = []
            self.logs = []
            self.error = None
            self.source_label = ""
            self.started_at = None
            self.finished_at = None
        self._stop.clear()
        return True

    # -- worker -------------------------------------------------------------
    def _run_thread(
        self,
        source_type: str,
        source_value: str,
        max_count: int,
        language: str,
        content_type: str,
    ) -> None:
        try:
            asyncio.run(
                self._run_async(
                    source_type, source_value, max_count, language, content_type
                )
            )
        except Exception as exc:  # pragma: no cover - safety net
            with self._lock:
                self.state = "error"
                self.error = str(exc)
            self._log(f"Fatal error: {exc}", "error")
        finally:
            with self._lock:
                if self.state != "error":
                    # Only "stopped" if a stop actually truncated the run; if
                    # every item finished, it's "done" regardless of a late stop.
                    truncated = self._stop.is_set() and self.processed < self.total
                    self.state = "stopped" if truncated else "done"
                self.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def _gather_urls(
        self,
        source_type: str,
        source_value: str,
        max_count: int,
        content_type: str,
    ) -> list[str]:
        if source_type == "urls":
            return normalize_url_list(source_value.split(","))
        async with ShortsScraper() as scraper:
            if source_type == "channel":
                return await scraper.collect_from_channel(
                    source_value, max_count, content_type
                )
            if source_type == "search":
                return await scraper.collect_from_search(
                    source_value, max_count, content_type
                )
        raise ValueError(f"Unknown source type: {source_type}")

    async def _run_async(
        self,
        source_type: str,
        source_value: str,
        max_count: int,
        language: str,
        content_type: str,
    ) -> None:
        config.ensure_directories()
        kind = "videos" if content_type == "videos" else "Shorts"
        self._log(f"Gathering {kind} for {source_type} = {source_value!r} ...")

        urls = await self._gather_urls(source_type, source_value, max_count, content_type)
        if not urls:
            self._log("No Shorts URLs found. Nothing to do.", "warn")
            return

        with self._lock:
            self.total = len(urls)
        self._log(f"Found {len(urls)} {kind}. Processing...")

        excel = ExcelLogger()
        async with ShortsScraper() as scraper:
            for index, url in enumerate(urls, start=1):
                if self._stop.is_set():
                    self._log("Stopped — remaining videos skipped.", "warn")
                    break

                video_id = extract_video_id(url)
                self._log(f"[{index}/{len(urls)}] {url}")
                try:
                    screenshot = await scraper.capture_screenshot(url)
                    result = await asyncio.to_thread(
                        process_short, url, video_id, screenshot, language
                    )
                    excel.append_row(
                        url=url, screenshot_path=screenshot, summary=result.summary
                    )
                    excel.save()  # incremental save so the link is always fresh

                    with self._lock:
                        self.processed += 1
                        self.results.append(
                            {
                                "index": index,
                                "url": url,
                                "video_id": video_id,
                                "screenshot": (
                                    Path(screenshot).name if screenshot else None
                                ),
                                "summary": result.summary,
                                "transcript": result.transcript,
                                "kind": result.kind,
                                "time": datetime.now().strftime("%H:%M:%S"),
                            }
                        )
                    tag = {"vision": " [visual]", "none": " [no speech]"}.get(result.kind, "")
                    self._log(f"  done{tag}: {result.summary[:90]}")
                except Exception as exc:  # keep going on a single bad video
                    self._log(f"  error processing {url}: {exc}", "error")
                    logger.exception("Error processing %s", url)

                if index < len(urls) and not self._stop.is_set():
                    await human_delay()

        self._log("Run finished.")


# A single shared runner instance for the dashboard process.
runner = PipelineRunner()
