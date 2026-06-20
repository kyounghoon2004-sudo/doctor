"""Central configuration for the YouTube Shorts processing pipeline.

All tunable parameters live here so the rest of the codebase never hard-codes
paths, model names, or timing values. Paths are resolved relative to this file
so the project can be moved or run from any working directory.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# Base directory = the folder containing this config.py
BASE_DIR: Path = Path(__file__).resolve().parent

# Where clean Shorts screenshots are written (one PNG per video).
SCREENSHOTS_DIR: Path = BASE_DIR / "screenshots"

# Scratch space for downloaded audio. Files here are deleted after processing.
TEMP_AUDIO_DIR: Path = BASE_DIR / "temp_audio"

# The Excel workbook all results are appended to.
EXCEL_FILE: Path = BASE_DIR / "shorts_summary.xlsx"

# Worksheet name inside the workbook.
EXCEL_SHEET_NAME: str = "Shorts"


# ---------------------------------------------------------------------------
# Playwright / browser
# ---------------------------------------------------------------------------
# Run the browser without a visible window. Set to False to watch it work
# (useful for debugging consent dialogs or selector changes).
HEADLESS: bool = True

# A realistic desktop user agent reduces the chance of bot challenges.
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Viewport (browser window) size in pixels.
VIEWPORT: dict[str, int] = {"width": 1280, "height": 900}

# Locale / timezone hints for the browser context.
LOCALE: str = "en-US"
TIMEZONE: str = "UTC"

# Hard navigation timeout (milliseconds) for page.goto / waits.
NAVIGATION_TIMEOUT_MS: int = 45_000

# URL collection keeps scrolling the feed/search results until it reaches the
# requested count, the results genuinely run out, or a safety cap is hit:
#   - give up after this many *consecutive* scrolls that surface no new items:
SCROLL_STALL_LIMIT: int = 5
#   - never scroll more than this many times total (guards against an infinite
#     loop when a query simply doesn't have enough results):
MAX_SCROLLS_HARD_CAP: int = 80

# Default upper bound on how many items to collect per target.
MAX_SHORTS_PER_TARGET: int = 25

# Default content type: "shorts" or "videos" (regular watch?v= videos).
DEFAULT_CONTENT_TYPE: str = "shorts"


# ---------------------------------------------------------------------------
# faster-whisper (transcription)
# ---------------------------------------------------------------------------
# Model size: "tiny", "base", "small", "medium", "large-v3".
# "base" is a good speed/accuracy trade-off on CPU.
WHISPER_MODEL_SIZE: str = "base"

# Transcription device:
#   "auto" -> use a CUDA (NVIDIA) GPU automatically when one is available,
#             otherwise fall back to CPU  (recommended default)
#   "cuda" -> force GPU       "cpu" -> force CPU
#
# IMPORTANT: faster-whisper's backend (CTranslate2) accelerates on NVIDIA/CUDA
# GPUs only. AMD/Intel GPUs are not supported and will transparently run on CPU.
WHISPER_DEVICE: str = "auto"

# Compute type chosen per device (GPU prefers float16; CPU prefers int8).
WHISPER_COMPUTE_TYPE_GPU: str = "float16"
WHISPER_COMPUTE_TYPE_CPU: str = "int8"

# Transcription engine:
#   "faster-whisper" -> CTranslate2. CPU here; uses a CUDA GPU automatically on
#                       NVIDIA machines (see WHISPER_DEVICE). Fast + has VAD.
#   "directml"       -> ONNX Runtime + DirectML. Runs on AMD/Intel/NVIDIA GPUs on
#                       Windows (DirectX 12). Correct, but on this hardware it is
#                       ~5x SLOWER than CPU because DirectML requires the decoder
#                       KV-cache to be disabled, and it has no VAD. Opt-in only.
WHISPER_BACKEND: str = "faster-whisper"

# Pre-exported ONNX Whisper model used by the "directml" backend (HF Hub id).
WHISPER_ONNX_MODEL: str = "onnx-community/whisper-base"

# Beam size for decoding. Higher = slightly better, slower.
WHISPER_BEAM_SIZE: int = 5

# Language hint. None => auto-detect.
WHISPER_LANGUAGE: str | None = None

# Voice-activity-detection filter. Strips non-speech audio before decoding.
# Strongly recommended: without it Whisper hallucinates phantom tokens like
# "You" or "Thank you" on silent/music-only clips, which would then be sent to
# the LLM instead of being correctly reported as "No speech detected".
WHISPER_VAD_FILTER: bool = True


# ---------------------------------------------------------------------------
# Ollama (local LLM summarization)
# ---------------------------------------------------------------------------
OLLAMA_API_URL: str = "http://localhost:11434"
OLLAMA_MODEL: str = "llama3"  # text summary model (see README for multilingual notes)

# Request timeout (seconds) for the generate call. Local LLMs can be slow on
# first load, so keep this generous.
OLLAMA_TIMEOUT_S: int = 120

# Prompt used to instruct the model. {transcript} and {language} are filled in.
OLLAMA_SUMMARY_PROMPT: str = (
    "You are a concise assistant. Summarize the following YouTube Short "
    "transcript in 1-2 short sentences describing what the video is about. "
    "Do not add commentary, disclaimers, or quotation marks. "
    "Write the summary in {language}.\n\n"
    "Transcript:\n{transcript}\n\nSummary:"
)

# Returned when Ollama cannot be reached or fails.
FALLBACK_SUMMARY: str = "Summary unavailable (LLM offline). See transcript."


# ---------------------------------------------------------------------------
# Vision "context reader" (for videos with no speech)
# ---------------------------------------------------------------------------
# A short with no spoken audio often still teaches something visually (on-screen
# text, a demonstration, a caption). When no speech is detected we send the
# screenshot to a local vision model to describe the lesson/takeaway.
#
# Requires a vision-capable Ollama model, e.g.:
#   ollama pull llava           (balanced, ~4.7GB)
#   ollama pull moondream       (small/fast, ~1.7GB)
#   ollama pull llama3.2-vision (highest quality, ~7.9GB)
VISION_MODEL: str = "llava"

VISION_PROMPT: str = (
    "This is a single still frame from a video that has NO spoken audio. In 1-2 "
    "concise sentences, describe what is shown and the main message, lesson, or "
    "takeaway the video conveys. Read and use any on-screen text. Do not mention "
    "that this is a frame or an image. Respond in {language}."
)

# Shown when there is no speech AND the vision model is unavailable/failed.
NO_SPEECH_SUMMARY: str = "No speech detected in this video."
VISION_UNAVAILABLE_SUMMARY: str = (
    "No speech detected; visual context unavailable (vision model offline). "
    'Run `ollama pull ' + VISION_MODEL + "` to enable the context reader."
)


# ---------------------------------------------------------------------------
# Summary language
# ---------------------------------------------------------------------------
# Language for generated summaries / visual context. Use "Auto" to match the
# content's own language (quality depends on OLLAMA_MODEL's multilingual ability).
DEFAULT_SUMMARY_LANGUAGE: str = "English"
# Values the UI may send that mean "match the content's language".
AUTO_LANGUAGE_VALUES: tuple[str, ...] = ("auto", "same as video", "same", "")


# ---------------------------------------------------------------------------
# Human-like delays (seconds)
# ---------------------------------------------------------------------------
# A random delay in [MIN_DELAY, MAX_DELAY] is inserted between page actions to
# look less like a bot and avoid rate limits / CAPTCHAs.
MIN_DELAY: float = 2.0
MAX_DELAY: float = 5.0

# Extra pause after a Shorts video frame is requested, to let it actually render
# before the screenshot is taken.
SCREENSHOT_RENDER_PAUSE_S: float = 2.5


# ---------------------------------------------------------------------------
# Excel image embedding
# ---------------------------------------------------------------------------
# Target on-sheet height for embedded screenshots, in pixels. Width is scaled
# to preserve the image aspect ratio.
EMBED_IMAGE_HEIGHT_PX: int = 110


def ensure_directories() -> None:
    """Create the working directories if they do not already exist."""
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
