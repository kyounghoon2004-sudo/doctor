# YouTube Shorts Automated Processing and Logging System

A local, login-free pipeline that:

1. **Finds** **Shorts _or_ regular videos** by a natural-language **search** ("recent AI developments"), a channel, or explicit URLs (Playwright). It keeps scrolling until it reaches the requested count (or the results run out).
2. **Screenshots** each video's player frame.
3. **Downloads** the audio stream (`yt-dlp`).
4. **Transcribes** the audio locally (`faster-whisper`).
5. **Summarizes** the transcript with a local LLM (Ollama, English).
6. **Context reader:** when a Short has **no speech**, a local **vision model** looks at the screenshot and describes the on-screen content/lesson instead.
7. **Logs** everything to `shorts_summary.xlsx` with the screenshot **embedded** in the sheet (`openpyxl`).

No YouTube account or login is required — Shorts are public, so the scraper reads anonymous pages only.

---

## 1. System dependencies

### FFmpeg (required)
`yt-dlp` and `faster-whisper` use FFmpeg to extract and convert audio. It must be on your `PATH`.

**Windows (recommended — winget):**
```powershell
winget install Gyan.FFmpeg
```
Or with Chocolatey:
```powershell
choco install ffmpeg
```
Verify:
```powershell
ffmpeg -version
```
> If `ffmpeg` isn't recognized after install, close and reopen your terminal so the updated `PATH` takes effect.

### Ollama (required for summaries + context reader)
A local LLM runner. Download from <https://ollama.com/download>, then pull the models and make sure the server is running:
```powershell
ollama pull llama3   # English text summaries          -> config.OLLAMA_MODEL
ollama pull llava    # vision context reader (no-speech) -> config.VISION_MODEL
ollama serve         # usually auto-starts; runs on http://localhost:11434
```
> Model notes:
> - **Summaries** are English, via `llama3` (set `config.OLLAMA_MODEL`).
> - **Context reader** uses any Ollama **vision** model (`llava`, `moondream`, `llama3.2-vision`); set `config.VISION_MODEL`.
> - If Ollama is offline (or a model isn't pulled), the pipeline still runs and writes a clearly labelled fallback instead of crashing.

---

## 2. Python environment

Requires **Python 3.10+**.

```powershell
cd youtube_shorts_processor   # the cloned/downloaded project folder

python -m venv .venv
.\.venv\Scripts\Activate.ps1        # PowerShell
# .\.venv\Scripts\activate.bat      # cmd.exe

pip install --upgrade pip
pip install -r requirements.txt
```

> **Windows: "스크립트를 실행할 수 없으므로… / cannot be loaded because running scripts is disabled"**
> PowerShell's default execution policy blocks `Activate.ps1`. Two options:
> 1. **Skip activation** and call the venv's Python directly (no system change):
>    ```powershell
>    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
>    .\.venv\Scripts\python.exe dashboard.py
>    ```
> 2. **Allow activation** for your user once (safe, no admin needed), then `Activate.ps1` works:
>    ```powershell
>    Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
>    ```

## 3. Install the browser binary

Playwright needs a browser engine installed once:
```powershell
playwright install chromium
```

---

## 4. Running the pipeline

```powershell
# Search Shorts by a natural-language query
python main.py --search "recent AI developments" --max 10

# Search regular VIDEOS instead of Shorts
python main.py --search "recent AI developments" --type videos --max 10

# Process a channel's Shorts (or --type videos for its Videos tab)
python main.py --channel MrBeast --type videos --max 10

# Process specific URLs (Shorts or watch?v=, mixed is fine)
python main.py --urls "https://www.youtube.com/shorts/ID1,https://www.youtube.com/watch?v=ID2"
```

### Interactive dashboard (recommended)

Prefer a UI? Launch the web dashboard instead of the CLI:

```powershell
python dashboard.py
# opens http://127.0.0.1:5000 automatically
# options: python dashboard.py --port 8000 --no-open
# if activation was blocked: .\.venv\Scripts\python.exe dashboard.py
```

From the dashboard you can:
- Pick a **source** — **Search**, **Channel**, or **URLs** — and a **Type** (**Shorts** or **Videos**), set **Max results**, and click **▶ Run**. Collection keeps scrolling until it reaches your **Max results** (or the results run out).
- Click **■ Stop** to halt gracefully (it finishes the current video, then stops).
- Watch the **live log** and **progress bar**, and see each finished Short in the **results grid** (screenshot + summary + link), badged 🎤 transcribed / 👁 visual context / 🔇 no speech.
- Use **⬇ Download .xlsx** or **📂 Open in Excel** for a direct link to the spreadsheet.
- Click **↻ New session (reset)** to archive the current spreadsheet (moved to the `archive/` folder with a timestamp) and clear the log + results. Your next run starts a fresh `shorts_summary.xlsx`. (Screenshots are kept; embedded copies already live inside each archived workbook.)

> The dashboard runs the exact same pipeline as the CLI below.

**Context reader:** for videos with no spoken audio, the pipeline sends the screenshot to the local vision model (`config.VISION_MODEL`) and writes a description of the on-screen lesson/message — so even silent/text-only videos get a meaningful summary. (It only runs when audio actually downloaded but had no speech — a failed/unavailable download is reported as such, not guessed at.)

**Regular videos vs Shorts:** regular videos use the same pipeline but can be much longer than Shorts, so transcription takes proportionally longer. For long videos, use a smaller `WHISPER_MODEL_SIZE` (`tiny`) — see [GPU acceleration](#gpu-acceleration) below for the device/backend options.

### CLI flags
| Flag | Description |
|------|-------------|
| `--search <query>` | Natural-language query, e.g. `"recent AI developments"`. |
| `--channel <name>` | Channel handle/name (`MrBeast` or `@MrBeast`). |
| `--urls <list>` | Comma-separated explicit URLs (Shorts or `watch?v=`). |
| `--type <shorts\|videos>` | Content type for `--search`/`--channel` (default `shorts`). |
| `--max <n>` | Max items to pull from a search/channel feed (default `25`). |
| `--headful` | Show the browser window (helpful for debugging). |
| `-v`, `--verbose` | Verbose / debug logging. |

`--search`, `--channel`, and `--urls` are mutually exclusive — pick one per run.

---

## 5. Output

- **`shorts_summary.xlsx`** — one row per Short:
  `Timestamp | URL | Screenshot Path | Summary | Screenshot (embedded image)`.
  New runs **append** to the existing workbook. The file saves after every video, so progress survives an interruption.
  > Close the workbook in Excel before running, otherwise the save will fail with a permission error.
- **`screenshots/`** — saved PNGs, named by video id.
- **`temp_audio/`** — scratch audio; each file is deleted automatically after it is transcribed.

---

## 6. Configuration

All tunables live in [`config.py`](config.py):

- **Paths** — `SCREENSHOTS_DIR`, `TEMP_AUDIO_DIR`, `EXCEL_FILE`.
- **Browser / collection** — `HEADLESS`, `USER_AGENT`, `VIEWPORT`, `MAX_SHORTS_PER_TARGET`, `SCROLL_STALL_LIMIT`, `MAX_SCROLLS_HARD_CAP`, `DEFAULT_CONTENT_TYPE`.
- **Transcription** — `WHISPER_MODEL_SIZE` (`tiny`→`large-v3`), `WHISPER_DEVICE` (`auto`/`cuda`/`cpu`), `WHISPER_BACKEND` (`faster-whisper`/`directml`), `WHISPER_VAD_FILTER` (keep `True` so silent clips report "No speech detected" instead of hallucinated text).
- **Vision / Ollama** — `VISION_MODEL`, `OLLAMA_API_URL`, `OLLAMA_MODEL`, `OLLAMA_SUMMARY_PROMPT`.
- **Delays** — `MIN_DELAY` / `MAX_DELAY` (human-like pacing between page actions).

### GPU acceleration
- **NVIDIA:** leave `WHISPER_DEVICE = "auto"` — faster-whisper automatically uses the CUDA GPU (`float16`) when one is present, else CPU. (Requires the CUDA/cuDNN runtime.)
- **AMD / Intel (Windows):** faster-whisper's CTranslate2 backend is **CUDA-only**, so it can't use these GPUs. Set `WHISPER_BACKEND = "directml"` to transcribe on the GPU via ONNX Runtime + DirectML (install the optional packages noted in `requirements.txt`).
  > Heads-up: benchmarked on an AMD RX 6700S, DirectML is **correct but ~5× slower than CPU** (it must disable Whisper's KV-cache, which is what otherwise produces garbage). It's **off by default** for that reason. CPU faster-whisper (`whisper base`) transcribes a ~50s clip in ~7s.

---

## 7. Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `ffmpeg not found` / audio download fails | FFmpeg not installed or not on `PATH`. See §1. |
| Summaries say "Summary unavailable (LLM offline)" | Ollama isn't running, or the model isn't pulled. Run `ollama serve` and `ollama pull llama3`. |
| Summaries/transcripts say "No speech detected" | The Short genuinely has no spoken audio (music-only). Expected behavior. |
| `playwright._impl... Executable doesn't exist` | Run `playwright install chromium`. |
| Few/zero URLs collected from a feed | YouTube layout/rate-limit changes. Try `--headful` to watch, raise `MAX_SCROLLS_HARD_CAP` in `config.py`, or pass URLs directly with `--urls`. |
| `PermissionError` on save | `shorts_summary.xlsx` is open in Excel. Close it and rerun. |

---

## 8. Responsible use

This tool is for personal, local, and educational use. Scrape responsibly: keep volumes modest, respect the human-like delays (don't set them to zero), and follow YouTube's Terms of Service and the content owners' rights. The randomized delays exist to avoid hammering YouTube — please leave them in place.
