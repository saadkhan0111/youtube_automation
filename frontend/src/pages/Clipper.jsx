import React, { useEffect, useRef, useState } from "react";
import { api, fmtDuration } from "../api.js";

const PERMISSIONS = ["unknown", "original", "licensed", "authorized", "fair_use_claimed", "not_authorized"];
const BUSY = ["extracting_audio", "transcribing", "finding_moments"];

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
  const load = () => api.get("/api/clipper/projects").then(setProjects);
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
        <button className="primary" onClick={() => setOpen(true)} disabled={cfg && !cfg.openai_key_present}>+ New project</button>
      </div>
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
            </div>
            {BUSY.includes(p.status) && <div className="bar" style={{ marginTop: 8 }}><div className="fill" style={{ width: `${Math.round(p.progress * 100)}%` }} /></div>}
            {p.error && <div className="error small">{p.error}</div>}
          </a>
        ))}
        {projects.length === 0 && <div className="muted card">No projects yet.</div>}
      </div>
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
    max_candidates: 8, min_clip_seconds: 20, max_clip_seconds: 60, voice: cfg.default_voice,
  });
  const [progress, setProgress] = useState(null);
  const [err, setErr] = useState("");
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const submit = async () => {
    setErr("");
    const body = { ...f, max_candidates: Number(f.max_candidates), min_clip_seconds: Number(f.min_clip_seconds), max_clip_seconds: Number(f.max_clip_seconds) };
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
        </div>
        {mode === "path" ? (
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
          <div className="grow"><label>Moments to find</label><input type="number" min={1} max={30} value={f.max_candidates} onChange={set("max_candidates")} disabled={busy} /></div>
          <div className="grow"><label>Min length (s)</label><input type="number" min={8} max={120} value={f.min_clip_seconds} onChange={set("min_clip_seconds")} disabled={busy} /></div>
          <div className="grow"><label>Max length (s)</label><input type="number" min={10} max={180} value={f.max_clip_seconds} onChange={set("max_clip_seconds")} disabled={busy} /></div>

        </div>
        <label>Avatar voice</label>
        <VoicePicker value={f.voice} voices={cfg.voices} onChange={(v) => setF({ ...f, voice: v })} disabled={busy} />
        <div className="muted small">The first preview of each voice takes a few seconds (it's generated once, then cached).</div>
        <h3>Source</h3>
        <div className="row">
          <div className="grow"><label>Source URL</label><input value={f.source_url} onChange={set("source_url")} disabled={busy} /></div>
          <div className="grow"><label>Creator</label><input value={f.source_creator} onChange={set("source_creator")} placeholder="Me" disabled={busy} /></div>
          <div className="grow">
            <label>Permission</label>
            <select value={f.permission_status} onChange={set("permission_status")} disabled={busy}>
              {PERMISSIONS.map((p) => <option key={p} value={p}>{p.replaceAll("_", " ")}</option>)}
            </select>
          </div>
        </div>
        <p className="muted small">
          Cost: transcription ≈ ${cfg.whisper_cost_per_min}/min of video (a 3-hour stream ≈ $1.10) plus a few cents for moment detection and voice.
        </p>
        {busy && mode === "upload" && <><div className="bar"><div className="fill" style={{ width: `${progress}%` }} /></div><div className="muted small">Uploading {progress}%…</div></>}
        {err && <div className="error">{err}</div>}
        <div className="row" style={{ marginTop: 10 }}>
          <button className="primary" onClick={submit} disabled={busy}>Create &amp; analyze</button>
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
          <label>Avatar voice (used for the next renders)</label>
          {voices && <VoicePicker value={p.voice || voices.default_voice} voices={voices.voices}
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
          onPreview={() => preview(c.start_time, c.end_time)} player={player} onChanged={load} />
      ))}
    </div>
  );
}

function MomentCard({ c, checked, onCheck, onPreview, player, onChanged }) {
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
          <b>{c.headline || "(no headline)"}</b>
        </label>
        <div className="row">
          <span className="score">{c.score ? `${c.score.toFixed(0)}/10` : "manual"}</span>
          <span className="pill">{c.category}</span>
          <span className="muted">{fmtT(c.start_time)} → {fmtT(c.end_time)} ({c.duration.toFixed(0)}s)</span>
        </div>
      </div>
      {!edit ? (
        <>
          <div className="small"><span className="muted">Avatar hook:</span> “{c.hook_line}” · <span className="muted">Outro:</span> “{c.outro_line}”</div>
          <div className="small muted">{c.reason}</div>
          {c.excerpt && <details className="small"><summary className="muted">Transcript</summary>{c.excerpt}</details>}
        </>
      ) : (
        <div className="edit-grid">
          <label>Start (s)</label><div className="row"><input value={edit.start_time} onChange={(e) => setEdit({ ...edit, start_time: e.target.value })} /><button onClick={() => fromPlayer("start_time")}>= player</button></div>
          <label>End (s)</label><div className="row"><input value={edit.end_time} onChange={(e) => setEdit({ ...edit, end_time: e.target.value })} /><button onClick={() => fromPlayer("end_time")}>= player</button></div>
          <label>On-screen title</label><input value={edit.headline} maxLength={120} onChange={(e) => setEdit({ ...edit, headline: e.target.value })} />
          <label>Avatar hook</label><input value={edit.hook_line} onChange={(e) => setEdit({ ...edit, hook_line: e.target.value })} />
          <label>Avatar outro</label><input value={edit.outro_line} onChange={(e) => setEdit({ ...edit, outro_line: e.target.value })} />
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
