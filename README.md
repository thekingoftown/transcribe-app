# Video Transcriber

Paste any video link (YouTube, Vimeo, X/Twitter, and 1000+ other sites) and get a transcript with timestamps. Runs 100% locally on your PC — no accounts, no API keys, nothing leaves your machine.

## Setup (one time)

1. Install Python from https://www.python.org/downloads/ — **check "Add Python to PATH"** during install.
2. Double-click `run.bat`. The first run installs everything automatically (a few minutes).

## Use

1. Double-click `run.bat` — your browser opens at http://localhost:5005.
2. Paste a video link, pick a model, click **Transcribe**.
3. Read the transcript on the page, or download it as `.txt` (timestamped text) or `.srt` (subtitles). Copies are also saved to the `transcripts` folder.

## Models

| Model  | Speed on CPU | Accuracy |
|--------|--------------|----------|
| tiny   | fastest      | okay     |
| base   | fast         | good (default) |
| small  | slower       | better   |
| medium | slow         | best     |

The first time you use a model it downloads automatically (base ≈ 150 MB). After that it's cached and works offline.

## Notes

- Transcription runs on CPU. A 10-minute video takes roughly 1–3 minutes with `base`.
- If a YouTube link fails, updating yt-dlp usually fixes it:
  open a terminal in this folder and run `venv\Scripts\pip install -U yt-dlp`
- To stop the app, close the black console window.
