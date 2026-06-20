"""Orchestrator for the YouTube Shorts processing pipeline.

Examples
--------
    # A whole channel's Shorts feed
    python main.py --channel MrBeast --max 10

    # A hashtag
    python main.py --hashtag funny --max 15

    # An explicit list of Short URLs (comma-separated)
    python main.py --urls "https://www.youtube.com/shorts/abc...,https://youtube.com/shorts/def..."

For each Short the pipeline:
    1. Captures a screenshot of the video frame.
    2. Downloads the audio.
    3. Transcribes (faster-whisper) and summarizes (Ollama).
    4. Logs a row to shorts_summary.xlsx with the screenshot embedded.
    5. Deletes the temp audio file.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import config
from excel_writer import ExcelLogger
from processor import process_short
from scraper import ShortsScraper, extract_video_id, human_delay, normalize_url_list


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # yt-dlp and others are noisy at DEBUG; keep them at WARNING.
    for noisy in ("urllib3", "yt_dlp", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape, transcribe, summarize, and log YouTube Shorts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--search",
        help="Natural-language query, e.g. 'recent AI developments'.",
    )
    source.add_argument("--channel", help="Channel handle/name, e.g. 'MrBeast' or '@MrBeast'.")
    source.add_argument("--urls", help="Comma-separated list of Shorts URLs.")

    parser.add_argument(
        "--type",
        choices=["shorts", "videos"],
        default=config.DEFAULT_CONTENT_TYPE,
        help="Content type for --search/--channel: 'shorts' or 'videos'.",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=config.MAX_SHORTS_PER_TARGET,
        help="Max items to process from a search/channel feed.",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Show the browser window (overrides config.HEADLESS).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    return parser.parse_args(argv)


async def gather_target_urls(args: argparse.Namespace) -> list[str]:
    """Resolve CLI arguments into a concrete list of Shorts URLs."""
    if args.urls:
        urls = normalize_url_list(args.urls.split(","))
        logging.getLogger("main").info("Using %d supplied URL(s).", len(urls))
        return urls

    # Search / channel require a browser to crawl the feed.
    async with ShortsScraper() as scraper:
        if args.channel:
            return await scraper.collect_from_channel(args.channel, args.max, args.type)
        return await scraper.collect_from_search(args.search, args.max, args.type)


async def run(args: argparse.Namespace) -> int:
    log = logging.getLogger("main")
    config.ensure_directories()

    urls = await gather_target_urls(args)
    if not urls:
        log.error("No Shorts URLs found. Nothing to do.")
        return 1

    total = len(urls)
    log.info("=" * 60)
    log.info("Processing %d Short(s).", total)
    log.info("=" * 60)

    excel = ExcelLogger()
    succeeded = 0

    # One browser session reused for all screenshots.
    async with ShortsScraper() as scraper:
        for index, url in enumerate(urls, start=1):
            video_id = extract_video_id(url)
            print(f"\n[{index}/{total}] {url}")
            try:
                # 1. Screenshot
                print("  -> capturing screenshot...")
                screenshot_path = await scraper.capture_screenshot(url)

                # 2-4. Audio download + transcription + summary (or, for
                # no-speech videos, the visual context reader on the screenshot).
                # Synchronous/CPU-bound, so run in a thread to free the loop.
                print("  -> downloading audio, transcribing, summarizing...")
                result = await asyncio.to_thread(
                    process_short, url, video_id, screenshot_path
                )

                # 5. Log to Excel (with embedded screenshot)
                excel.append_row(
                    url=url,
                    screenshot_path=screenshot_path,
                    summary=result.summary,
                )
                excel.save()  # save incrementally so a crash keeps progress
                succeeded += 1
                print(f"  -> done. Summary: {result.summary[:100]}")
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # never let one bad video kill the run
                log.error("Failed to process %s: %s", url, exc)
                print(f"  !! error: {exc}")

            # Human-like pause between videos (skip after the last one).
            if index < total:
                await human_delay()

    log.info("=" * 60)
    log.info("Finished: %d/%d succeeded. Results -> %s", succeeded, total, config.EXCEL_FILE)
    log.info("=" * 60)
    return 0 if succeeded else 1


def _force_utf8_console() -> None:
    """Make stdout/stderr UTF-8 so non-Latin summaries (Korean, etc.) don't
    crash on legacy code pages like Windows cp949."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    args = parse_args(argv)
    if args.headful:
        config.HEADLESS = False
    setup_logging(args.verbose)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted by user. Exiting.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
