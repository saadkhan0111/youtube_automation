# Shorts Publisher: GTA 6 YouTube Shorts automation

Takes finished vertical clips from your clipping engine, generates metadata with Claude, puts every clip through
**human approval and a source-rights check**, then uploads through the official **YouTube Data API v3** (resumable
uploads). Clips go up as private or unlisted, or are scheduled with `publishAt`. Quota is tracked locally and
enforced conservatively.

```
clipper ──► data/incoming/*.mp4 + *.json ──► ingest (validate, hash, move to library)
                                               │
                                     metadata (Claude) ──► pending_review
                                               │  you: edit / approve (rights confirmed) / reject
                                               ▼
                       approved ──► upload queue ──► resumable videos.insert ──► uploaded / scheduled ──► published
                                        ▲ quota gate (fails closed)             ▲ verified with videos.list
```

## 1. Installation

Requirements: Windows/macOS/Linux, **Python 3.11+**, **Node 18+**, **FFmpeg** (validation + clipping), and an **OpenAI API key** for the AI Clipper.

```bash
winget install Gyan.FFmpeg
```
(Open a new terminal afterwards so `ffprobe` is on PATH.)

```bash
cd backend
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt       # add -r requirements-dev.txt for tests
copy .env.example .env
cd ..\frontend
npm install
npm run build
```

`start.ps1` does all of this automatically on the first run.

## 2. Environment variables (`backend/.env`)

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_CLIENT_SECRET_FILE` | `credentials/client_secret.json` | Desktop OAuth client JSON |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | OAuth redirect = `<this>/api/auth/callback` |
| `DASHBOARD_URL` | `http://localhost:8000` | Where the browser lands after OAuth (`http://localhost:5173` with `npm run dev`) |
| `YOUTUBE_ENABLE_MANAGE_SCOPE` | `false` | Adds `youtube.force-ssl` so already-uploaded scheduled videos can be rescheduled |
| `ANTHROPIC_API_KEY` | (empty) | Claude metadata generation. Empty = template fallback |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Model for metadata |
| `METADATA_AI_ENABLED` | `true` | Turn AI metadata off |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address. Non-localhost requires `DASHBOARD_TOKEN` |
| `DASHBOARD_TOKEN` | (empty) | Password for the dashboard/API |
| `AGENT_API_TOKEN` | (empty) | Enables `/api/agent/*` (see §12) |
| `ALLOWED_ORIGINS` | localhost:8000/5173 | CORS + CSRF origin allow-list |
| `DATA_DIR` | `data` | DB, logs, incoming/library folders |
| `CREDENTIALS_DIR` | `credentials` | OAuth tokens (`credentials/tokens/`) |
| `INCOMING_DIR` | `<DATA_DIR>/incoming` | Point at your clipper's output folder |
| `LIBRARY_DIR` | `<DATA_DIR>/library` | Imported clips. Uploads only from here |
| `DATABASE_URL` | `sqlite:///data/app.db` | SQLAlchemy URL |
| `FFPROBE_PATH` | `ffprobe` | Full path if not on PATH |
| `UPLOAD_CHUNK_SIZE_MB` | `8` | Resumable chunk size |
| `UPLOAD_MAX_RETRIES` | `8` | Retries for temporary errors before pausing |
| `SCHEDULER_ENABLED` | `true` | Background jobs |

## 3. Google Cloud OAuth setup

1. Go to <https://console.cloud.google.com/>, create (or select) a project.
2. **APIs & Services → Library →** enable **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen (Google Auth Platform)**: User type *External*. Fill in the app name and
   support email, and add yourself under **Test users**.
   *Scopes:* add `.../auth/youtube.upload` and `.../auth/youtube.readonly` (and `youtube.force-ssl` only if you set
   `YOUTUBE_ENABLE_MANAGE_SCOPE=true`).
4. **Credentials → Create credentials → OAuth client ID → Application type: Desktop app**. Download the JSON.
5. Save it as **`backend/credentials/client_secret.json`** (this folder is git-ignored).

The app uses the loopback redirect `http://localhost:8000/api/auth/callback`. Desktop clients allow loopback
redirects without extra configuration.

> **Important:** while the consent screen is in **Testing** status, Google expires refresh tokens after **7 days**,
> and you'll have to reconnect weekly. Publish the app (*Publishing status → In production*) to get long-lived tokens.
> For personal use with sensitive scopes, Google shows an "unverified app" warning that you can click through.
> Also note: Google may lock videos uploaded by **unverified API projects** (created after 28 Jul 2020) to *private*
> until the project passes a YouTube API compliance audit. Private test uploads work either way. Request the audit at
> <https://support.google.com/youtube/contact/yt_api_form> before relying on scheduled/public publishing.

**Why two scopes?** `youtube.upload` is enough for `videos.insert`, but it can't call `channels.list(mine=true)` to verify
which channel you connected, or `videos.list` to confirm upload status. `youtube.readonly` adds only read access.

## 4. Database setup

Nothing to do. SQLite is created automatically at `backend/data/app.db` on first start (tables: `youtube_accounts`,
`videos`, `uploads`, `quota_usage`, `api_request_log`, `publishing_schedule`, `event_log`). Back it up by copying the
`backend/data` folder while the app is stopped. To use another database, set `DATABASE_URL`.

## 5. Start / stop

```bash
powershell -ExecutionPolicy Bypass -File .\start.ps1
```
Open <http://localhost:8000>. Stop with **Ctrl+C**, or from another terminal:
```bash
powershell -ExecutionPolicy Bypass -File .\stop.ps1
```
After changing frontend code, run `start.ps1 -Rebuild`. For frontend development, run the backend, then
`cd frontend && npm run dev` (http://localhost:5173, set `DASHBOARD_URL=http://localhost:5173`).

To run it daily without a terminal: Windows **Task Scheduler → Create Task → Trigger "At log on" → Action**
`powershell.exe -ExecutionPolicy Bypass -File "C:\Users\<you>\Desktop\youtube  automation\start.ps1"`.
The PC must be on for queued uploads to run. Already-scheduled videos publish on YouTube's side even if the PC is off.

## 6. Connect your YouTube channel

1. Put `client_secret.json` in place (§3) and start the app.
2. Dashboard → **Settings → Connect YouTube channel**. Google's consent screen opens; pick the Google account/brand
   account that owns the channel and allow access.
3. You're redirected back; the channel name and avatar appear (verified with `channels.list mine=true`, 1 quota unit).
   **Nothing is uploaded during connection.**
4. **Test connection** refreshes the access token if needed and re-checks the channel.

## 7. Connecting your clipper (watch folder)

For each clip, the clipper writes two files into the incoming folder (`INCOMING_DIR`):

* `my_clip.mp4`: the finished render (1080x1920, H.264, AAC, ≤ 3 min, constant frame rate)
* `my_clip.json`: the sidecar, written **last**, after the MP4 is complete. Its presence means "render complete".

```json
{
  "source_url": "https://www.twitch.tv/videos/123",
  "source_creator": "StreamerName",
  "permission_status": "authorized",
  "permission_notes": "Written permission via email 2026-11-10",
  "footage_type": "authorized",
  "clip_start": 812.5,
  "clip_end": 851.0,
  "topic": "GTA 6 vs GTA 5",
  "context_notes": "Streamer compares the GTA 6 police AI with GTA 5's",
  "original_commentary": "My voiceover explains the wanted-level differences",
  "transcript": "optional transcript text..."
}
```
`permission_status` is one of `unknown | original | licensed | authorized | fair_use_claimed | not_authorized`.
You can include `title`, `description`, `tags` and `hashtags` to skip AI generation.

Every 30 s the app imports files that haven't changed for 10 s. It validates them with ffprobe, skips duplicates
(SHA-256), moves them into the library and creates a `pending_review` record. Files still being written are left
alone. Broken sidecars go to `incoming/rejected/` and duplicates go to `incoming/duplicates/`.

## 8. Upload a private test video

```bash
cd backend
.venv\Scripts\python -m app.cli make-test-clip
```
1. This renders an 8-second 1080x1920 test pattern into the incoming folder. Within about 40 s it shows up under
   **Videos → Pending review** (or click *Scan incoming folder*).
2. Open it, tick the rights confirmation and **Approve**.
3. Click **Upload privately**. The upload history shows progress, then the YouTube ID and a Studio link.
4. Check it in YouTube Studio: it's **Private**. Delete it there when you're done.

Each test upload uses 1 upload call plus 1 unit (verification). The automated tests (`pytest`) use a fake YouTube API
and never touch real quota.

## 9. Schedule a Short

Public publishing is **off by default**. Until you enable it, only private and unlisted uploads are possible.

1. **Settings → Allow public publishing** (confirm). Optionally set publishing times and the daily limit
   (default 13:00 / 18:00 / 21:00 PKT, max 3/day).
2. Open an approved clip. The schedule box is pre-filled with the next free slot. Times are **Pakistan time
   (Asia/Karachi)** and are converted to UTC ISO 8601 for the API.
3. Click **Schedule**. The video is uploaded right away as `private` with `status.publishAt`, and YouTube makes it
   public at that time. Check it in **YouTube Studio → Content** (it shows *Scheduled*).
4. Times in the past, less than the minimum lead time ahead (default 30 min), or over the daily limit are refused.
   YouTube publishes a past `publishAt` immediately, which is why those times are blocked.
5. **Reschedule** from the Calendar: before upload, just the time changes. After upload, rescheduling needs
   `YOUTUBE_ENABLE_MANAGE_SCOPE=true` plus a reconnect (`videos.update`, 50 units), or you can change it in Studio.

**Optional automation:** Settings → *Auto-schedule approved clips* fills free slots with clips **you already
approved** (every 10 min). Nothing unreviewed or rejected is ever published. Publishing times don't guarantee views.

## 10. How quota tracking works

* Every YouTube API request is written to `api_request_log` (method, time, bucket, estimated cost, result, HTTP
  status, reason, retry flag) **before** it is sent, so a crash over-counts rather than under-counts.
* Buckets (Google defaults, which can change): **upload** (`videos.insert`, 100 calls/day), **search** (`search.list`,
  100/day, unused by this app), **other** (10,000 units/day: `channels.list`=1, `videos.list`=1, `videos.update`=50).
* **App safety limits** (Settings): 80 upload calls, 80 search calls, 8,000 other units. A request that would exceed a
  limit is never sent, and new uploads stop.
* Counters are keyed by the **Pacific-time date**, so they reset at midnight PT (13:00 PKT in winter, 12:00 PKT in summer).
* If Google returns `quotaExceeded` or `uploadLimitExceeded`, the bucket is marked **exhausted** until the reset.
* If the app stopped mid-request, usage is marked **uncertain** and automated calls pause. Check Google Cloud
  Console → APIs & Services → YouTube Data API v3 → Quotas, then clear the warning on the **Quota** page.
* Resuming an interrupted upload doesn't call `videos.insert` again, so it costs no extra upload quota.
* Local video processing (ffprobe, ingest) never counts as quota.

## 11. Reliability and recovering from errors

| Situation | What happens | What you do |
|---|---|---|
| Network drop mid-upload | Session URI saved after every chunk; retried with exponential backoff, resuming the **same** session | Nothing. After 8 failures it's *paused* and auto-resumes on the next queue tick |
| App closed mid-upload | On start, upload → *paused*, resumed from the saved session | Nothing |
| Resumable session expired (≈1 week) | New `videos.insert` (counted) | Nothing |
| 429 / 500 / 502 / 503 / 504 | Backoff and retry | Nothing |
| 400 invalid metadata, etc. | **Not retried**; upload *failed* with the reason | Fix metadata → **Retry** |
| Quota exhausted / limit reached | Upload stays queued; continues after the reset | Nothing (or raise limits) |
| Bad/partial render | Blocked by validation; never uploaded | Re-render |
| Duplicate | Blocked (same video or identical file hash already has a YouTube ID) | n/a |

### Recover from OAuth expiration / revocation
Symptoms: Settings shows **expired** or **revoked**, uploads pause with `[auth]`.
1. Settings → **Reconnect Google account** and approve again (`prompt=consent` forces a fresh refresh token).
2. Paused uploads resume automatically on the next queue tick (≤ 60 s).
3. If Google doesn't return a refresh token: remove the app at <https://myaccount.google.com/permissions>, then reconnect.
4. Weekly expiry = consent screen in *Testing* mode (see §3).

## 12. Agent integration (instead of OpenClaw)

You don't need OpenClaw. The backend already runs ingest, the upload queue, schedule sync and quota resets on its own.
If you want an agent (Claude Code, a script, or OpenClaw later) to orchestrate, set `AGENT_API_TOKEN` and call:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/agent/pending-clips` | Clips awaiting review |
| GET | `/api/agent/approved-clips` | Approved clips not yet queued |
| GET | `/api/agent/videos/{id}` / `/api/agent/uploads/{id}` | Status |
| GET | `/api/agent/quota` | Quota status |
| POST | `/api/agent/videos/{id}/generate-metadata` | Regenerate metadata (pending clips only) |
| POST | `/api/agent/videos/{id}/request-approval` | Flag for human review (logged) |
| POST | `/api/agent/videos/{id}/upload` `{"mode":"private"\|"unlisted"\|"scheduled","publish_at":"2026-11-20T18:00"}` | Queue an **already approved** clip |

Header: `Authorization: Bearer <AGENT_API_TOKEN>`. Agents **cannot** approve, clear rights, delete, change
settings or bypass quota. Those rules are enforced in `backend/app/services/workflow.py` for every caller.

## AI Clipper: long video → edited Shorts

Dashboard → **AI Clipper → + New project**. Paste the full path of a long video (stream VOD, walkthrough; the file is
read in place) or upload it. Then:

1. **Extract audio** (FFmpeg) → **transcribe** with OpenAI Whisper (word timestamps) → **measure loud spikes**.
2. **Find moments**: the OpenAI model (`OPENAI_MODEL`, default `gpt-5.5`) reads the transcript and loudness markers and
   proposes 20–60 s moments, each with a score, category, on-screen title, avatar hook and outro line.
3. **You choose**: ▶ Preview plays the moment in the source player. **Edit** lets you change start/end (or take them
   from the player position), the title, hook and outro. **Discard** removes bad picks, and **+ Moment at player time**
   adds your own. Tick moments and click **Render selected**.
4. **Render** (about 1–2 min per Short on this laptop), fully automatic:
   * **Smart framing:** full-screen 9:16 (no bands) when the action is centred and the source is at least 720p;
     otherwise a tall gameplay panel (about 70% of the frame) on a dark vignetted background, or a wider crop when
     the motion is dominated by the sides. The crop follows localized action and holds steady during camera motion.
   * **Opening hook card:** the headline on an animated yellow card while the avatar speaks the hook, then it leaves.
   * **Captions:** big Segoe UI Black, 1–3 word groups that bounce in, the spoken word pops, and important words are
     highlighted (yellow for game audio, cyan for the avatar). They sit in the lower-middle third; in third-person
     chase/driving footage (the player is in the lower half) they move to the upper-middle. Profanity is masked.
   * **Voice:** OpenAI `gpt-4o-mini-tts` with an energetic YouTuber direction, then processing (compression,
     presence EQ, a 6% speed-up). The default is `cedar`; `marin` is the natural female voice. Every voice has a
     ▶ Preview in the dashboard, and you can change the voice per project.
   * **Avatar facecam:** shadow, rim and glow, breathing, sway, blinking, mouth shapes driven by the voice, and a pop-in.
   * **Context-aware effects:** loud jumps in the clip are classified with the transcript and scene-cut detection.
     A real impact gets a punch zoom, camera shake, flash, boom and avatar reaction. A shouted line gets a small
     punch-in. A loud scene cut gets a whoosh. Ordinary dialogue gets nothing. The strongest real impact gets a
     build-up push-in and can get slow motion (once, about 0.8 s at half speed, never during the avatar's voice;
     disable with `CLIP_SLOWMO=false`). There's always a whip-zoom intro with a whoosh and pop, and an outro
     push-in with a fade.
   * The game audio is ducked under the AI voice, and sound effects are synthesized locally (`backend/data/sfx`).
5. The Short lands in **Videos → Pending review** with its source info and AI-written YouTube title/description.
   Approve and upload as usual.

**Costs:** transcription about $0.006/min of source (a 3-hour stream ≈ $1.10). Moment detection, metadata and voice
cost a few cents. Transcripts are cached, so **Analyze again** only re-runs moment detection.

**Your avatar:** `backend/assets/avatar.png` (square, transparent background). If you change it, set `AVATAR_MOUTH` to
the mouth position in the 600×600 image, or set `AVATAR_MOUTH_ENABLED=false` (it will still bounce and glow).

**Real lip-sync later:** the avatar is a local animation (free). For a lip-synced talking head, a D-ID or HeyGen API
key can replace `app/clipper/avatar.py`; everything else stays the same.

## 13. Security notes

* `client_secret.json`, tokens, `.env`, the database and clips are git-ignored. Tokens live in
  `backend/credentials/tokens/` with permissions restricted to your Windows user, and are never sent to the frontend
  or written to logs (log lines that look like tokens are redacted).
* The server binds to `127.0.0.1` and refuses to bind elsewhere without `DASHBOARD_TOKEN`. Put it behind HTTPS if you expose it.
* Mutating API calls require an `X-Requested-With: ytshorts` header and a trusted `Origin` (CSRF protection).
* Uploads are only possible for approved, rights-cleared, validated files inside the library folder (paths are
  resolved and checked, so no directory traversal).

## 14. Content and copyright

Every clip carries a source-rights record (source URL, creator, permission status, evidence, footage type, review
status), and approval requires you to confirm it. Keep in mind:
* Copyright permission and YouTube **monetization** eligibility are separate questions.
* Subtitles, cropping, zooming, transitions or AI voiceovers do not by themselves make third-party footage safe.
* YouTube's [reused/inauthentic content policy](https://support.google.com/youtube/answer/1311392) expects meaningful
  original commentary, analysis or entertainment. Avoid mass-producing near-identical clips.
* Adding `#Shorts` does not make a video a Short. Shorts are classified by format: vertical/square and ≤ 3 minutes.
* This app never automates views, likes, comments or subscribers.

## 15. Tests

```bash
cd backend
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```
Covers quota limits/reset/fail-closed, Pacific-day boundaries, PKT→UTC conversion, validation, resumable upload
recovery (network drops, restarts, expired sessions, 5xx, quota, permanent errors, cancel), duplicate protection,
approval rules, scheduling, CSRF/auth, agent restrictions and ingest. All against a fake YouTube API.

## Project layout
```
backend/app/
  main.py            FastAPI app, logging, static dashboard
  config.py          env settings, scopes, defaults
  models.py          SQLAlchemy models
  jobs.py            APScheduler: ingest, upload queue, auto-schedule, status sync, quota reset
  api/routes.py      dashboard API          api/agent.py   agent API
  services/
    oauth.py         Google OAuth, token storage/refresh, channel verification
    quota.py         quota tracking & enforcement
    uploader.py      resumable upload engine
    validation.py    ffprobe checks (Shorts format, render complete)
    metadata.py      Claude metadata generation (+ template fallback)
    workflow.py      approval / rights / publishing rules
    scheduling.py    PKT slots & publish-time validation
    ingest.py        watch-folder import
  clipper/           AI clipper: analysis.py (Whisper + moments), render.py (FFmpeg edit), avatar.py, captions.py, worker.py
frontend/src/        React dashboard (Overview, AI Clipper, Videos, Calendar, Quota, Settings)
```
