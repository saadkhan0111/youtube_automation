import React, { useEffect, useRef, useState } from "react";
import { api, fmtDuration } from "../api.js";

const PERMISSIONS = ["unknown", "original", "licensed", "authorized", "fair_use_claimed", "not_authorized"];
const BUSY = ["downloading", "extracting_audio", "transcribing", "finding_moments"];

function fmtT(s) {
  if (s == null) return "—";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = (s % 60).toFixed(1).padStart(4, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

// Voice dropdown with a play button that fetches a cached sample of that voice.
const VOICE_NOTES = { cedar: "natural, male (recommended)", marin: "natural, female", ash: "male", verse: "male, expressive",
  ballad: "male, warm", onyx: "deep male", echo: "male", coral: "female", nova: "female", shimmer: "female", sage: "calm", fable: "storyteller", alloy: "neutral" };
let currentAudio = null;

function VoicePicker({ value, voices, onChange, disabled }) {
  const [state, setState] = useState("");
  const play = () => {
    if (currentAudio) { currentAudio.pause(); currentAudio = null; }
    setState("loading");
    const audio = new Audio(`/api/clipper/voices/${value}/preview`);
    currentAudio = audio;
    audio.oncanplay = () => setState("playing");
    audio.onended = () => setState("");
    audio.onerror = () => setState("error");
    audio.play().catch(() => setState("error"));
  };
  return (
    <div className="row voice-picker">
      <select value={value} onChange={(e) => { onChange(e.target.value); setState(""); }} disabled={disabled}>
        {voices.map((v) => <option key={v} value={v}>{v}{VOICE_NOTES[v] ? ` — ${VOICE_NOTES[v]}` : ""}</option>)}
      </select>
      <button type="button" onClick={play} disabled={disabled || state === "loading"} title="Hear a sample of this voice">
        {state === "loading" ? "…" : state === "playing" ? "♪ Playing" : "▶ Preview"}
      </button>
      {state === "error" && <span className="error small">Preview failed</span>}
    </div>
  );
}

export default function Clipper({ params }) {
  const id = params.get("project");
  return id ? <ProjectView id={Number(id)} /> : <ProjectList />;
}

// ------------------------------------------------------------------ list + new project
function ProjectList() {
  const [projects, setProjects] = useState([]);
  const [cfg, setCfg] = useState(null);
  const [open, setOpen] = useState(false);
  const [liveOpen, setLiveOpen] = useState(false);
  const [lives, setLives] = useState([]);
  const load = () => { api.get("/api/clipper/projects").then(setProjects); api.get("/api/clipper/live").then(setLives); };
  useEffect(() => {
    load();
    api.get("/api/clipper/config").then(setCfg);
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, []);
  return (
    <div>
      <div className="row between">
        <h1>AI Clipper</h1>
        <div className="row">
          <button className="live-btn" onClick={() => setLiveOpen(true)}>● Record live stream</button>
          <button className="primary" onClick={() => setOpen(true)}>+ New project</button>
        </div>
      </div>
      {lives.length > 0 && <LiveList lives={lives} onChanged={load} />}
      {cfg && !cfg.openai_key_present && <div className="banner warn">Add OPENAI_API_KEY to backend/.env and restart the app to use the clipper.</div>}
      <p className="muted">
        Add a long video (stream, walkthrough). The AI transcribes it, finds the best moments, and you choose which ones become
        edited Shorts (blurred-background layout, title, captions, avatar voice). Finished Shorts land in <a href="#/videos">Videos → Pending review</a>.
      </p>
      <div className="project-grid">
        {projects.map((p) => (
          <a key={p.id} className="card project-card" href={`#/clipper?project=${p.id}`}>
            <div className="li-title">{p.name}</div>
            <div className="li-meta">
              <span className={`pill s-${p.status === "ready" ? "uploaded" : p.status === "failed" ? "failed" : "queued"}`}>{p.status.replaceAll("_", " ")}</span>
              <span>{fmtDuration(p.duration)}</span>
              <span>{p.candidate_count} moments</span>
              <span>{p.rendered_count} rendered</span>
              {p.plain && <span className="pill">quick</span>}
            </div>
            {BUSY.includes(p.status) && <div className="bar" style={{ marginTop: 8 }}><div className="fill" style={{ width: `${Math.round(p.progress * 100)}%` }} /></div>}
            {p.error && <div className="error small">{p.error}</div>}
          </a>
        ))}
        {projects.length === 0 && <div className="muted card">No projects yet.</div>}
      </div>
      {liveOpen && <NewLive onClose={() => setLiveOpen(false)} onStarted={() => { setLiveOpen(false); load(); }} />}
      {open && cfg && <NewProject cfg={cfg} onClose={() => setOpen(false)} onCreated={(pid) => { window.location.hash = `#/clipper?project=${pid}`; }} />}
    </div>
  );
}

function NewProject({ cfg, onClose, onCreated }) {
  const [mode, setMode] = useState("path");
  const [path, setPath] = useState("");
  const [file, setFile] = useState(null);
  const [f, setF] = useState({
    name: "", topic_hint: "", source_url: "", source_creator: "", permission_status: "original", permission_notes: "",
    max_candidates: 8, min_clip_seconds: 20, max_clip_seconds: 60, voice: cfg.default_voice, plain: !cfg.openai_key_present,
  });
  const [progress, setProgress] = useState(null);
  const [err, setErr] = useState("");
  const [yt, setYt] = useState({ url: "", start: "", end: "", info: null, checking: false });
  const fmtT = (s) => (s == null ? "" : `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`);
  const checkLink = async () => {
    setErr(""); setYt({ ...yt, checking: true, info: null });
    try {
      const info = await api.post("/api/clipper/youtube/probe", { url: yt.url.trim() });
      setYt((y) => ({ ...y, checking: false, info, start: y.start || "0:00:00", end: y.end || fmtT(Math.min(info.duration || 0, 3600)) }));
      setF((x) => ({ ...x, source_creator: x.source_creator || info.channel || "", permission_status: "authorized" }));
    } catch (e) { setYt((y) => ({ ...y, checking: false })); setErr(e.message); }
  };
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const submit = async () => {
    setErr("");
    const body = { ...f, max_candidates: Number(f.max_candidates), min_clip_seconds: Number(f.min_clip_seconds), max_clip_seconds: Number(f.max_clip_seconds) };
    if (mode === "youtube") {
      if (!yt.info) return setErr("Paste the link and press Check first.");
      try {
        setProgress(100);
        const p = await api.post("/api/clipper/projects/youtube", { ...body, url: yt.url.trim(), start: yt.start, end: yt.end, source_url: null });
        onCreated(p.id);
      } catch (e) { setProgress(null); setErr(e.message); }
      return;
    }
    if (mode === "path") {
      if (!path.trim()) return setErr("Paste the full path of the video file.");
      try {
        setProgress(100);
        const p = await api.post("/api/clipper/projects", { ...body, source_path: path.trim() });
        onCreated(p.id);
      } catch (e) { setProgress(null); setErr(e.message); }
      return;
    }
    if (!file) return setErr("Choose a video file.");
    const fd = new FormData();
    fd.append("file", file);
    Object.entries(body).forEach(([k, v]) => fd.append(k, v ?? ""));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/clipper/projects/upload");
    xhr.setRequestHeader("X-Requested-With", "ytshorts");
    xhr.upload.onprogress = (e) => e.lengthComputable && setProgress(Math.round((e.loaded / e.total) * 100));
    xhr.onload = () => {
      let d = {};
      try { d = JSON.parse(xhr.responseText); } catch { /* */ }
      if (xhr.status < 300) onCreated(d.id);
      else { setProgress(null); setErr(d.detail || `HTTP ${xhr.status}`); }
    };
    xhr.onerror = () => { setProgress(null); setErr("Network error"); };
    setProgress(0);
    xhr.send(fd);
  };

  const busy = progress !== null;
  return (
    <div className="modal-bg" onClick={busy ? undefined : onClose}>
      <div className="modal card wide" onClick={(e) => e.stopPropagation()}>
        <h3>New clip project</h3>
        <div className="tabs">
          <button className={mode === "path" ? "tab active" : "tab"} onClick={() => setMode("path")} disabled={busy}>File on this PC (recommended for big files)</button>
          <button className={mode === "upload" ? "tab active" : "tab"} onClick={() => setMode("upload")} disabled={busy}>Upload</button>
          <button className={mode === "youtube" ? "tab active" : "tab"} onClick={() => { setMode("youtube"); setF((x) => ({ ...x, permission_status: "authorized" })); }} disabled={busy}>From YouTube link</button>
        </div>
        {mode === "youtube" ? (
          <>
            <label>YouTube link (video or finished live stream)</label>
            <div className="row">
              <input className="grow" value={yt.url} onChange={(e) => setYt({ ...yt, url: e.target.value, info: null })} placeholder="https://www.youtube.com/live/... or https://youtu.be/..." disabled={busy} />
              <button onClick={checkLink} disabled={busy || yt.checking || !yt.url.trim()}>{yt.checking ? "Checking…" : "Check"}</button>
            </div>
            {yt.info && (
              <div className="yt-info">
                {yt.info.thumbnail && <img src={yt.info.thumbnail} alt="" referrerPolicy="no-referrer" />}
                <div className="small">
                  <b>{yt.info.title}</b><br />
                  {yt.info.channel} · {fmtT(yt.info.duration)} {yt.info.was_live && <span className="pill">live stream replay</span>}
                  {yt.info.chapters.length > 0 && <div className="muted tiny-text">{yt.info.chapters.length} chapters: {yt.info.chapters.slice(0, 4).map((c) => `${fmtT(c.start)} ${c.title}`).join(" · ")}…</div>}
                </div>
              </div>
            )}
            <div className="row">
              <div className="grow"><label>From</label><input value={yt.start} onChange={(e) => setYt({ ...yt, start: e.target.value })} placeholder="1:20:00" disabled={busy} /></div>
              <div className="grow"><label>To</label><input value={yt.end} onChange={(e) => setYt({ ...yt, end: e.target.value })} placeholder="1:50:00" disabled={busy} /></div>
            </div>
            <div className="muted small">
              Only this part is downloaded (up to 1080p, max 4 hours per import), then analysed like any other video. Short parts of long
              streams download at about real-time speed; bigger parts are fetched faster in full and cut. Only import streams that are
              yours or that the creator allowed you to clip, and note the proof below. YouTube's Terms don't allow downloading without
              YouTube's own permission, even with the creator's OK; you decided to accept that.
            </div>
          </>
        ) : mode === "path" ? (
          <>
            <label>Full path to the video</label>
            <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="C:\Users\you\Videos\stream.mp4" disabled={busy} />
            <div className="muted small">The file is read in place, never copied or modified. Tip: in Explorer, Shift + right-click the file → “Copy as path”.</div>
          </>
        ) : (
          <>
            <label>Video file</label>
            <input type="file" accept="video/*" onChange={(e) => setFile(e.target.files[0] || null)} disabled={busy} />
            <div className="muted small">The file is copied into backend/data/sources.</div>
          </>
        )}
        <div className="row">
          <div className="grow"><label>Project name</label><input value={f.name} onChange={set("name")} placeholder="(file name)" disabled={busy} /></div>
          <div className="grow"><label>What is this video?</label><input value={f.topic_hint} onChange={set("topic_hint")} placeholder="GTA 5 heist walkthrough" disabled={busy} /></div>
        </div>
        <div className="row">
          <div className="grow"><label>Moments to find</label><input type="number" min={1} max={100} value={f.max_candidates} onChange={set("max_candidates")} disabled={busy} /></div>
          <div className="grow"><label>Min length (s)</label><input type="number" min={8} max={120} value={f.min_clip_seconds} onChange={set("min_clip_seconds")} disabled={busy} /></div>
          <div className="grow"><label>Max length (s)</label><input type="number" min={10} max={180} value={f.max_clip_seconds} onChange={set("max_clip_seconds")} disabled={busy} /></div>

        </div>
        <label className="check">
          <input type="checkbox" checked={f.plain} disabled={busy || !cfg.openai_key_present} onChange={(e) => setF({ ...f, plain: e.target.checked })} />
          <span><b>Quick mode</b>: no transcript, no AI voice or captions. Moments are found from the loudest parts (free, fast);
            clips are rendered vertical with your optional title card.</span>
        </label>
        {!f.plain && <>
          <label>Avatar voice</label>
          <VoicePicker value={f.voice} voices={cfg.voices} onChange={(v) => setF({ ...f, voice: v })} disabled={busy} />
          <div className="muted small">The first preview of each voice takes a few seconds (it's generated once, then cached).</div>
        </>}
        <h3>Source</h3>
        <div className="row">
          {mode !== "youtube" && <div className="grow"><label>Source URL</label><input value={f.source_url} onChange={set("source_url")} disabled={busy} /></div>}
          <div className="grow"><label>Creator</label><input value={f.source_creator} onChange={set("source_creator")} placeholder="Me" disabled={busy} /></div>
          <div className="grow">
            <label>Permission</label>
            <select value={f.permission_status} onChange={set("permission_status")} disabled={busy}>
              {PERMISSIONS.filter((p) => mode !== "youtube" || ["authorized", "original", "licensed"].includes(p)).map((p) => <option key={p} value={p}>{p.replaceAll("_", " ")}</option>)}
            </select>
          </div>
        </div>
        {(mode === "youtube" || f.permission_status === "authorized") && (
          <><label>Proof of permission {mode === "youtube" && f.permission_status !== "original" && <span className="muted">(required)</span>}</label>
            <input value={f.permission_notes} onChange={set("permission_notes")} placeholder="e.g. Discord DM from the streamer on 18 Nov 2026: OK to make Shorts from my streams" disabled={busy} /></>
        )}
        <p className="muted small">
          Cost: transcription ≈ ${cfg.whisper_cost_per_min}/min of video (a 3-hour stream ≈ $1.10) plus a few cents for moment detection and voice.
        </p>
        {busy && mode === "upload" && <><div className="bar"><div className="fill" style={{ width: `${progress}%` }} /></div><div className="muted small">Uploading {progress}%…</div></>}
        {err && <div className="error">{err}</div>}
        <div className="row" style={{ marginTop: 10 }}>
          <button className="primary" onClick={submit} disabled={busy}>{mode === "youtube" ? "Import & analyze" : "Create & analyze"}</button>
          <button onClick={onClose} disabled={busy}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ project detail
function ProjectView({ id }) {
  const [p, setP] = useState(null);
  const [err, setErr] = useState("");
  const [selected, setSelected] = useState({});
  const [msg, setMsg] = useState("");
  const player = useRef(null);
  const stopAt = useRef(null);
  const [voices, setVoices] = useState(null);
  useEffect(() => { api.get("/api/clipper/config").then(setVoices); }, []);

  const load = () => api.get(`/api/clipper/projects/${id}`).then(setP).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [id]);
  useEffect(() => {
    if (!p) return;
    const active = BUSY.includes(p.status) || p.candidates.some((c) => ["queued", "rendering"].includes(c.status));
    if (!active) return;
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [p]);

  const preview = (start, end) => {
    const v = player.current;
    if (!v) return;
    v.currentTime = start;
    stopAt.current = end;
    v.play();
    v.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };
  const onTime = () => {
    const v = player.current;
    if (stopAt.current != null && v.currentTime >= stopAt.current) { v.pause(); stopAt.current = null; }
  };

  if (err) return <div className="error">{err}</div>;
  if (!p) return <div className="muted">Loading…</div>;
  const chosen = Object.keys(selected).filter((k) => selected[k]).map(Number);
  const renderSel = async () => {
    try {
      const r = await api.post(`/api/clipper/projects/${id}/render`, { candidate_ids: chosen });
      setMsg(`Rendering ${r.queued.length} Short(s)… they appear in Videos → Pending review when done.`);
      setSelected({});
      load();
    } catch (e) { setMsg(e.message); }
  };
  const addManual = async () => {
    const t = player.current ? Math.floor(player.current.currentTime) : 0;
    try {
      await api.post(`/api/clipper/projects/${id}/candidates`, { start_time: t, end_time: Math.min(t + 30, p.duration || t + 30) });
      load();
    } catch (e) { setMsg(e.message); }
  };

  return (
    <div>
      <div className="row between">
        <h1><a href="#/clipper">AI Clipper</a> / {p.name}</h1>
        <div className="row">
          {p.status === "downloading" && <button onClick={async () => { await api.post(`/api/clipper/projects/${id}/cancel-download`); load(); }}>Cancel download</button>}
          {!BUSY.includes(p.status) && (
            <button onClick={async () => { if (confirm("Run moment detection again? Proposed (not rendered) moments are replaced. Transcription is reused.")) { await api.post(`/api/clipper/projects/${id}/analyze`); load(); } }}>Analyze again</button>
          )}
          <button className="danger ghost" onClick={async () => {
            if (!confirm("Delete this project? Rendered Shorts stay in Videos. Linked source files are not touched.")) return;
            try { await api.del(`/api/clipper/projects/${id}`); window.location.hash = "#/clipper"; } catch (e) { setMsg(e.message); }
          }}>Delete project</button>
        </div>
      </div>

      <div className="clip-top">
        <div className="card">
          <video ref={player} className="source-player" src={p.source_url_preview} controls preload="metadata" onTimeUpdate={onTime} />
          <div className="muted small">
            {p.source_file} · {fmtDuration(p.duration)} · {p.resolution || "—"} · permission: {p.permission_status.replaceAll("_", " ")}
            {p.est_cost_usd ? ` · transcription cost ≈ $${p.est_cost_usd.toFixed(2)}` : ""}
          </div>
        </div>
        <div className="card">
          <h3>Status</h3>
          <div><span className={`pill s-${p.status === "ready" ? "uploaded" : p.status === "failed" ? "failed" : "queued"}`}>{p.status.replaceAll("_", " ")}</span> {p.stage_message}</div>
          {BUSY.includes(p.status) && <div className="bar" style={{ marginTop: 10 }}><div className="fill" style={{ width: `${Math.round(p.progress * 100)}%` }} /></div>}
          {p.error && <div className="error small">{p.error}</div>}
          <p className="muted small">Settings: {p.max_candidates} moments, {p.min_clip_seconds}–{p.max_clip_seconds}s.</p>
          {p.plain && <div className="small"><span className="pill">quick mode</span> no transcript, AI voice or captions. The title you type
            on a moment becomes its on-screen title card and the video title.</div>}
          {!p.plain && <label>Avatar voice (used for the next renders)</label>}
          {voices && !p.plain && <VoicePicker value={p.voice || voices.default_voice} voices={voices.voices}
            onChange={async (v) => { await api.patch(`/api/clipper/projects/${id}`, { voice: v }); load(); }} />}
          <div className="row">
            <button className="primary" disabled={!chosen.length} onClick={renderSel}>Render {chosen.length || ""} selected</button>
            <button onClick={addManual} title="Adds a 30 s moment starting at the player's current position">+ Moment at player time</button>
          </div>
          {msg && <div className="okmsg small">{msg}</div>}
        </div>
      </div>

      {p.status === "ready" && p.candidates.length === 0 && <div className="muted card">No moments found. Try “Analyze again” or add one manually.</div>}
      {p.candidates.map((c) => (
        <MomentCard key={c.id} c={c} checked={!!selected[c.id]} onCheck={(v) => setSelected({ ...selected, [c.id]: v })}
          onPreview={() => preview(c.start_time, c.end_time)} player={player} onChanged={load} plain={p.plain} />
      ))}
    </div>
  );
}

function MomentCard({ c, checked, onCheck, onPreview, player, onChanged, plain }) {
  const [edit, setEdit] = useState(null);
  const [err, setErr] = useState("");
  const busy = ["queued", "rendering"].includes(c.status);
  const save = async () => {
    try {
      await api.patch(`/api/clipper/candidates/${c.id}`, {
        start_time: Number(edit.start_time), end_time: Number(edit.end_time),
        headline: edit.headline, hook_line: edit.hook_line, outro_line: edit.outro_line,
      });
      setEdit(null); setErr(""); onChanged();
    } catch (e) { setErr(e.message); }
  };
  const fromPlayer = (k) => player.current && setEdit({ ...edit, [k]: player.current.currentTime.toFixed(1) });

  return (
    <div className={`card moment ${checked ? "moment-sel" : ""}`}>
      <div className="row between">
        <label className="check" style={{ margin: 0 }}>
          <input type="checkbox" checked={checked} disabled={busy} onChange={(e) => onCheck(e.target.checked)} />
          <b>{c.headline || (plain ? "(add a title: Edit)" : "(no headline)")}</b>
        </label>
        <div className="row">
          <span className="score">{c.score ? `${c.score.toFixed(0)}/10` : "manual"}</span>
          <span className="pill">{c.category}</span>
          <span className="muted">{fmtT(c.start_time)} → {fmtT(c.end_time)} ({c.duration.toFixed(0)}s)</span>
        </div>
      </div>
      {!edit ? (
        <>
          {!plain && <div className="small"><span className="muted">Avatar hook:</span> “{c.hook_line}” · <span className="muted">Outro:</span> “{c.outro_line}”</div>}
          <div className="small muted">{c.reason}</div>
          {c.excerpt && <details className="small"><summary className="muted">Transcript</summary>{c.excerpt}</details>}
        </>
      ) : (
        <div className="edit-grid">
          <label>Start (s)</label><div className="row"><input value={edit.start_time} onChange={(e) => setEdit({ ...edit, start_time: e.target.value })} /><button onClick={() => fromPlayer("start_time")}>= player</button></div>
          <label>End (s)</label><div className="row"><input value={edit.end_time} onChange={(e) => setEdit({ ...edit, end_time: e.target.value })} /><button onClick={() => fromPlayer("end_time")}>= player</button></div>
          <label>{plain ? "Title (card + video title)" : "On-screen title"}</label><input value={edit.headline} maxLength={plain ? 95 : 120} onChange={(e) => setEdit({ ...edit, headline: e.target.value })} />
          {!plain && <><label>Avatar hook</label><input value={edit.hook_line} onChange={(e) => setEdit({ ...edit, hook_line: e.target.value })} />
          <label>Avatar outro</label><input value={edit.outro_line} onChange={(e) => setEdit({ ...edit, outro_line: e.target.value })} /></>}
        </div>
      )}
      <div className="row" style={{ marginTop: 8 }}>
        <button onClick={onPreview}>▶ Preview</button>
        {!edit && !busy && <button onClick={() => setEdit({ start_time: c.start_time, end_time: c.end_time, headline: c.headline || "", hook_line: c.hook_line || "", outro_line: c.outro_line || "" })}>Edit</button>}
        {edit && <><button className="primary" onClick={save}>Save</button><button onClick={() => { setEdit(null); setErr(""); }}>Cancel</button></>}
        {!busy && c.status !== "rendered" && <button className="danger ghost" onClick={async () => { await api.post(`/api/clipper/candidates/${c.id}/discard`); onChanged(); }}>Discard</button>}
        <span className={`pill s-${c.status === "rendered" ? "uploaded" : c.status === "failed" ? "failed" : c.status === "proposed" ? "" : "queued"}`}>{c.status}</span>
        {c.status === "rendered" && c.video_id && <a href={`#/videos?status=pending_review&id=${c.video_id}`}>Open Short #{c.video_id} →</a>}
      </div>
      {busy && <div className="bar" style={{ marginTop: 6 }}><div className="fill" style={{ width: `${Math.round(c.render_progress * 100)}%` }} /></div>}
      {(err || c.error) && <div className="error small">{err || c.error}</div>}
    </div>
  );
}


// ------------------------------------------------------------------ live recording
const hms = (s) => `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

function LiveList({ lives, onChanged }) {
  const [busy, setBusy] = useState(null);
  const stop = async (id) => {
    if (!confirm("Stop recording? The current part is finished and saved as a clip project.")) return;
    setBusy(id);
    try { await api.post(`/api/clipper/live/${id}/stop`); } catch (e) { alert(e.message); }
    setBusy(null); onChanged();
  };
  return (
    <div className="card live-list">
      <h3>Live recordings</h3>
      {lives.slice(0, 6).map((r) => {
        const active = ["starting", "recording", "stopping"].includes(r.status);
        return (
          <div key={r.id} className={`live-row ${active ? "on" : ""}`}>
            <div className="row between">
              <div>
                {active && <span className="rec-dot" />} <b>{r.title || r.url}</b> <span className="muted small">{r.channel}</span>
                <div className="small muted">
                  {r.status === "recording" ? "Recording" : r.status} · {hms(r.recorded_seconds)} recorded · {r.size_mb} MB · {r.parts_done} part{r.parts_done === 1 ? "" : "s"} ready
                  {" "}· parts of {r.part_minutes} min, stops after {Math.round(r.max_minutes / 60 * 10) / 10} h
                  {r.message && !active ? ` · ${r.message}` : ""}
                </div>
                {r.error && <div className="error small">{r.error}</div>}
              </div>
              {active && r.status !== "stopping" && <button className="danger" disabled={busy === r.id} onClick={() => stop(r.id)}>■ Stop</button>}
            </div>
            {r.projects.length > 0 && (
              <div className="row wrap" style={{ marginTop: 6 }}>
                {r.projects.map((p) => (
                  <a key={p.id} className="pill part-pill" href={`#/clipper?project=${p.id}`}>
                    Part {p.part}: {p.status === "ready" ? `${p.candidates} moments` : p.status.replaceAll("_", " ")}
                  </a>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function NewLive({ onClose, onStarted }) {
  const [f, setF] = useState({ url: "", part_minutes: 30, max_minutes: 360, source_creator: "", permission_status: "authorized",
    permission_notes: "", max_candidates: 5, min_clip_seconds: 20, max_clip_seconds: 45 });
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const start = async () => {
    setErr(""); setBusy(true);
    try {
      await api.post("/api/clipper/live", { ...f, part_minutes: Number(f.part_minutes), max_minutes: Number(f.max_minutes),
        max_candidates: Number(f.max_candidates), min_clip_seconds: Number(f.min_clip_seconds), max_clip_seconds: Number(f.max_clip_seconds) });
      onStarted();
    } catch (e) { setErr(e.message); setBusy(false); }
  };
  return (
    <div className="modal-bg" onClick={busy ? undefined : onClose}>
      <div className="modal card wide" onClick={(e) => e.stopPropagation()}>
        <h3>● Record a live stream</h3>
        <p className="muted small">Records from now until the stream ends (or you stop it). Every part becomes a quick-mode clip project as soon as
          it's finished: loud moments are proposed, you trim, add a title and render. No transcript, no AI voice. Keep the app and PC running
          while it records (about 1–3 GB per hour at 1080p).</p>
        <label>Live stream link</label>
        <input value={f.url} onChange={set("url")} placeholder="https://www.youtube.com/live/... or https://www.youtube.com/watch?v=..." disabled={busy} />
        <div className="row">
          <div className="grow"><label>New part every (minutes)</label><input type="number" min={5} max={120} value={f.part_minutes} onChange={set("part_minutes")} disabled={busy} /></div>
          <div className="grow"><label>Stop after (minutes)</label><input type="number" min={10} max={720} value={f.max_minutes} onChange={set("max_minutes")} disabled={busy} /></div>
        </div>
        <div className="row">
          <div className="grow"><label>Moments per part</label><input type="number" min={1} max={20} value={f.max_candidates} onChange={set("max_candidates")} disabled={busy} /></div>
          <div className="grow"><label>Clip length min (s)</label><input type="number" min={8} max={120} value={f.min_clip_seconds} onChange={set("min_clip_seconds")} disabled={busy} /></div>
          <div className="grow"><label>max (s)</label><input type="number" min={10} max={180} value={f.max_clip_seconds} onChange={set("max_clip_seconds")} disabled={busy} /></div>
        </div>
        <div className="row">
          <div className="grow"><label>Streamer</label><input value={f.source_creator} onChange={set("source_creator")} placeholder="(channel name)" disabled={busy} /></div>
          <div className="grow"><label>Permission</label>
            <select value={f.permission_status} onChange={set("permission_status")} disabled={busy}>
              {["authorized", "original", "licensed"].map((x) => <option key={x} value={x}>{x}</option>)}
            </select></div>
        </div>
        <label>Proof of permission {f.permission_status !== "original" && <span className="muted">(required)</span>}</label>
        <input value={f.permission_notes} onChange={set("permission_notes")} placeholder="e.g. Discord DM from the streamer on 18 Nov 2026: OK to clip my streams" disabled={busy} />
        <div className="muted small">Only record streams that are yours or that the creator allowed you to clip. YouTube's Terms don't allow
          downloading without YouTube's own permission, even with the creator's OK.</div>
        {err && <div className="error">{err}</div>}
        <div className="row" style={{ marginTop: 10 }}>
          <button className="primary" onClick={start} disabled={busy || !f.url.trim()}>{busy ? "Connecting…" : "● Start recording"}</button>
          <button onClick={onClose} disabled={busy}>Cancel</button>
        </div>
      </div>
    </div>
  );
}
