"""Audio download, transcription, summarization, and visual context.

Pipeline per Short:
    download_audio()  -> WAV file in temp_audio/
    transcribe()      -> text via faster-whisper
    summarize()       -> 1-2 sentence summary via local Ollama (any language)
    describe_image()  -> when there is NO speech, read the lesson/context from
                         the screenshot via a local Ollama vision model

Every stage degrades gracefully: an offline Ollama or a missing vision model
yields a clearly labelled fallback rather than crashing the pipeline.
"""

from __future__ import annotations

import base64
import glob
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import requests

import config

logger = logging.getLogger(__name__)


def _resolve_language(language: str | None) -> str:
    """Map a UI language choice to a prompt-ready clause."""
    lang = (language or config.DEFAULT_SUMMARY_LANGUAGE).strip()
    if lang.lower() in config.AUTO_LANGUAGE_VALUES:
        return "the same language as the content"
    return lang


@dataclass
class ProcessingResult:
    """Outcome of processing a single Short.

    ``kind`` records how the summary was produced:
        "speech" -> transcribed audio, "vision" -> visual context reader,
        "none"   -> no speech and no visual context available.
    """

    transcript: str
    summary: str
    kind: str = "speech"


# ---------------------------------------------------------------------------
# 1. Audio download (yt-dlp)
# ---------------------------------------------------------------------------
def download_audio(short_url: str, video_id: str | None = None) -> Path | None:
    """Download only the audio stream of a Short as a WAV file.

    Returns the path to the WAV, or None if the download/conversion failed
    (e.g. private/removed video, network error, or missing FFmpeg).
    """
    # Import lazily so the module loads even if yt-dlp isn't installed yet.
    try:
        import yt_dlp
    except ImportError:
        logger.error("yt-dlp is not installed. Run: pip install yt-dlp")
        return None

    config.TEMP_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    out_template = str(config.TEMP_AUDIO_DIR / "%(id)s.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "wav",
                "preferredquality": "192",
            }
        ],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(short_url, download=True)
        resolved_id = (info or {}).get("id") or video_id
        if resolved_id:
            candidate = config.TEMP_AUDIO_DIR / f"{resolved_id}.wav"
            if candidate.exists():
                logger.info("Downloaded audio -> %s", candidate.name)
                return candidate
        # Fallback: find the most recent wav (handles odd id templating).
        wavs = sorted(
            glob.glob(str(config.TEMP_AUDIO_DIR / "*.wav")),
            key=os.path.getmtime,
            reverse=True,
        )
        if wavs:
            logger.info("Downloaded audio -> %s", os.path.basename(wavs[0]))
            return Path(wavs[0])
        logger.error("yt-dlp finished but no WAV file was produced for %s", short_url)
        return None
    except Exception as exc:  # yt-dlp raises a variety of DownloadError types
        logger.error("Audio download failed for %s: %s", short_url, exc)
        return None


# ---------------------------------------------------------------------------
# 2. Transcription (faster-whisper)
# ---------------------------------------------------------------------------
# The Whisper model is expensive to construct, so build it once and reuse it.
_WHISPER_MODEL = None
_WHISPER_LOAD_FAILED = False


def _cuda_available() -> bool:
    """True if CTranslate2 sees a usable CUDA (NVIDIA) GPU."""
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _resolve_device() -> tuple[str, str]:
    """Resolve (device, compute_type) from config, preferring GPU when asked.

    "auto" uses a CUDA GPU when available, else CPU. "cuda" forces GPU. Anything
    else is CPU. Returns CPU settings if no CUDA GPU is present so the caller can
    still attempt (and the load wrapper falls back cleanly).
    """
    want = (config.WHISPER_DEVICE or "auto").lower()
    use_gpu = want == "cuda" or (want == "auto" and _cuda_available())
    if use_gpu:
        return "cuda", config.WHISPER_COMPUTE_TYPE_GPU
    return "cpu", config.WHISPER_COMPUTE_TYPE_CPU


def _get_whisper_model():
    """Lazily load and cache the WhisperModel; return None if unavailable.

    Prefers the GPU (CUDA) when available/requested and falls back to CPU if the
    GPU initialization fails — so a "GPU by default" config never crashes a run.
    """
    global _WHISPER_MODEL, _WHISPER_LOAD_FAILED
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL
    if _WHISPER_LOAD_FAILED:
        return None
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.error("faster-whisper is not installed. Run: pip install faster-whisper")
        _WHISPER_LOAD_FAILED = True
        return None

    device, compute_type = _resolve_device()
    if config.WHISPER_DEVICE.lower() == "auto" and device == "cpu":
        logger.info("No CUDA GPU detected for transcription; using CPU.")

    try:
        logger.info(
            "Loading Whisper model '%s' on %s (%s)...",
            config.WHISPER_MODEL_SIZE,
            device,
            compute_type,
        )
        _WHISPER_MODEL = WhisperModel(
            config.WHISPER_MODEL_SIZE, device=device, compute_type=compute_type
        )
        return _WHISPER_MODEL
    except Exception as exc:
        # GPU init can fail (missing CUDA/cuDNN, unsupported GPU). Fall back to CPU.
        if device != "cpu":
            logger.warning(
                "GPU transcription init failed (%s); falling back to CPU.", exc
            )
            try:
                _WHISPER_MODEL = WhisperModel(
                    config.WHISPER_MODEL_SIZE,
                    device="cpu",
                    compute_type=config.WHISPER_COMPUTE_TYPE_CPU,
                )
                return _WHISPER_MODEL
            except Exception as exc2:
                logger.error("Failed to load Whisper model on CPU too: %s", exc2)
        else:
            logger.error("Failed to load Whisper model: %s", exc)
        _WHISPER_LOAD_FAILED = True
        return None


# -- Optional DirectML (GPU) backend ---------------------------------------
_DML_ASR = None
_DML_FAILED = False


def _get_directml_pipeline():
    """Lazily build a Whisper ASR pipeline on ONNX Runtime + DirectML (GPU).

    Uses use_cache=False because the cached (decoder-with-past) path produces
    garbage on DirectML. Returns None if the stack isn't installed / init fails.
    """
    global _DML_ASR, _DML_FAILED
    if _DML_ASR is not None:
        return _DML_ASR
    if _DML_FAILED:
        return None
    try:
        from optimum.onnxruntime import ORTModelForSpeechSeq2Seq
        from transformers import AutoProcessor, pipeline
    except ImportError:
        logger.error(
            "DirectML backend needs: pip install onnxruntime-directml optimum-onnx transformers"
        )
        _DML_FAILED = True
        return None
    try:
        logger.info(
            "Loading Whisper ONNX '%s' on DirectML (GPU)...", config.WHISPER_ONNX_MODEL
        )
        model = ORTModelForSpeechSeq2Seq.from_pretrained(
            config.WHISPER_ONNX_MODEL,
            provider="DmlExecutionProvider",
            use_cache=False,
        )
        proc = AutoProcessor.from_pretrained(config.WHISPER_ONNX_MODEL)
        _DML_ASR = pipeline(
            "automatic-speech-recognition",
            model=model,
            tokenizer=proc.tokenizer,
            feature_extractor=proc.feature_extractor,
        )
        return _DML_ASR
    except Exception as exc:
        logger.error("Failed to initialize DirectML Whisper: %s", exc)
        _DML_FAILED = True
        return None


def _transcribe_directml(audio_path: Path) -> str | None:
    """Transcribe via the DirectML pipeline. None => unavailable (caller falls back)."""
    asr = _get_directml_pipeline()
    if asr is None:
        return None
    try:
        result = asr(str(audio_path), return_timestamps=True)
        text = (result.get("text") if isinstance(result, dict) else str(result)).strip()
        logger.info("Transcribed %s on DirectML (%d chars)", Path(audio_path).name, len(text))
        return text
    except Exception as exc:
        logger.error("DirectML transcription failed for %s: %s", audio_path, exc)
        return None


def transcribe_audio(audio_path: Path) -> str:
    """Transcribe an audio file. Returns text, or "" when no speech is found."""
    if audio_path is None or not Path(audio_path).exists():
        logger.warning("Transcription skipped: audio file missing.")
        return ""

    # Opt-in GPU path: ONNX Runtime + DirectML. Falls back to faster-whisper
    # (CPU) if the DirectML stack is unavailable.
    if (config.WHISPER_BACKEND or "").lower() == "directml":
        text = _transcribe_directml(audio_path)
        if text is not None:
            return text
        logger.warning("DirectML unavailable; falling back to faster-whisper (CPU).")

    model = _get_whisper_model()
    if model is None:
        return ""

    try:
        segments, info = model.transcribe(
            str(audio_path),
            beam_size=config.WHISPER_BEAM_SIZE,
            language=config.WHISPER_LANGUAGE,
            vad_filter=config.WHISPER_VAD_FILTER,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        if not text:
            logger.info("No speech detected in %s", Path(audio_path).name)
            return ""
        logger.info(
            "Transcribed %s (lang=%s, %d chars)",
            Path(audio_path).name,
            getattr(info, "language", "?"),
            len(text),
        )
        return text
    except Exception as exc:
        logger.error("Transcription failed for %s: %s", audio_path, exc)
        return ""


# ---------------------------------------------------------------------------
# 3. Summarization (Ollama)
# ---------------------------------------------------------------------------
def summarize_text(transcript: str, language: str | None = None) -> str:
    """Summarize a transcript via the local Ollama API, in ``language``.

    Returns a 1-2 sentence summary. Falls back to a labelled placeholder if
    the transcript is empty or Ollama is unreachable.
    """
    if not transcript or not transcript.strip():
        return config.NO_SPEECH_SUMMARY

    url = f"{config.OLLAMA_API_URL.rstrip('/')}/api/generate"
    payload = {
        "model": config.OLLAMA_MODEL,
        "prompt": config.OLLAMA_SUMMARY_PROMPT.format(
            transcript=transcript, language=_resolve_language(language)
        ),
        "stream": False,
    }

    try:
        response = requests.post(url, json=payload, timeout=config.OLLAMA_TIMEOUT_S)
        response.raise_for_status()
        data = response.json()
        summary = (data.get("response") or "").strip()
        if not summary:
            logger.warning("Ollama returned an empty summary; using fallback.")
            return config.FALLBACK_SUMMARY
        logger.info("Generated summary via Ollama (%s).", config.OLLAMA_MODEL)
        return summary
    except requests.exceptions.ConnectionError:
        logger.error(
            "Could not reach Ollama at %s. Is it running? (`ollama serve`)",
            config.OLLAMA_API_URL,
        )
        return config.FALLBACK_SUMMARY
    except requests.exceptions.Timeout:
        logger.error("Ollama request timed out after %ss.", config.OLLAMA_TIMEOUT_S)
        return config.FALLBACK_SUMMARY
    except requests.exceptions.HTTPError as exc:
        # A 404 usually means the model name isn't pulled yet.
        logger.error(
            "Ollama HTTP error: %s. Did you run `ollama pull %s`?",
            exc,
            config.OLLAMA_MODEL,
        )
        return config.FALLBACK_SUMMARY
    except Exception as exc:
        logger.error("Unexpected error talking to Ollama: %s", exc)
        return config.FALLBACK_SUMMARY


# ---------------------------------------------------------------------------
# 4. Visual context reader (for videos with no speech)
# ---------------------------------------------------------------------------
def describe_image(image_path: Path | str | None, language: str | None = None) -> str:
    """Describe a screenshot's content/lesson via a local Ollama vision model.

    Returns a 1-2 sentence description, or "" if no image is available or the
    vision model is unreachable / not installed.
    """
    if not image_path:
        return ""
    path = Path(image_path)
    if not path.exists():
        logger.warning("Vision skipped: screenshot missing (%s).", image_path)
        return ""

    try:
        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
    except OSError as exc:
        logger.error("Could not read screenshot for vision: %s", exc)
        return ""

    url = f"{config.OLLAMA_API_URL.rstrip('/')}/api/generate"
    payload = {
        "model": config.VISION_MODEL,
        "prompt": config.VISION_PROMPT.format(language=_resolve_language(language)),
        "images": [b64],
        "stream": False,
    }
    try:
        response = requests.post(url, json=payload, timeout=config.OLLAMA_TIMEOUT_S)
        response.raise_for_status()
        text = (response.json().get("response") or "").strip()
        if text:
            logger.info("Generated visual context via %s.", config.VISION_MODEL)
        return text
    except requests.exceptions.ConnectionError:
        logger.error("Vision: could not reach Ollama at %s.", config.OLLAMA_API_URL)
        return ""
    except requests.exceptions.Timeout:
        logger.error("Vision: Ollama request timed out.")
        return ""
    except requests.exceptions.HTTPError as exc:
        logger.error(
            "Vision: Ollama HTTP error: %s. Did you run `ollama pull %s`?",
            exc,
            config.VISION_MODEL,
        )
        return ""
    except Exception as exc:
        logger.error("Vision: unexpected error: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Orchestration + cleanup
# ---------------------------------------------------------------------------
def process_short(
    short_url: str,
    video_id: str | None = None,
    screenshot_path: Path | str | None = None,
    summary_language: str | None = None,
) -> ProcessingResult:
    """Run the full pipeline for a single Short.

    Speech path: download audio -> transcribe -> summarize.
    No-speech path: fall back to the visual "context reader" on the screenshot.

    Always returns a ProcessingResult (never raises), so the caller's loop can
    continue even when an individual video fails.
    """
    audio_path = download_audio(short_url, video_id=video_id)
    try:
        # Download failed (unavailable/private/network). Don't run the vision
        # reader on what is probably an error-page screenshot — report clearly.
        if audio_path is None:
            return ProcessingResult(
                transcript="",
                summary="Audio could not be downloaded (video may be unavailable or private).",
                kind="none",
            )

        transcript = transcribe_audio(audio_path)
        if transcript:
            summary = summarize_text(transcript, summary_language)
            return ProcessingResult(transcript=transcript, summary=summary, kind="speech")

        # Downloaded but genuinely no speech -> read the lesson from the screenshot.
        context = describe_image(screenshot_path, summary_language)
        if context:
            return ProcessingResult(
                transcript="(no speech — visual context below)",
                summary=context,
                kind="vision",
            )

        # No speech and no usable visual context (e.g. vision model offline).
        fallback = (
            config.VISION_UNAVAILABLE_SUMMARY
            if screenshot_path
            else config.NO_SPEECH_SUMMARY
        )
        return ProcessingResult(
            transcript="(no speech detected)", summary=fallback, kind="none"
        )
    finally:
        cleanup_audio(audio_path)


def cleanup_audio(audio_path: Path | None) -> None:
    """Delete a temporary audio file if it exists."""
    if audio_path is None:
        return
    try:
        path = Path(audio_path)
        if path.exists():
            path.unlink()
            logger.debug("Removed temp audio: %s", path.name)
    except OSError as exc:
        logger.warning("Could not delete temp audio %s: %s", audio_path, exc)
