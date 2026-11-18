import React, { useEffect, useState } from "react";
import { api, fmtBytes, fmtDuration, fmtLocal, toLocalInput } from "../api.js";

const TABS = [
  ["pending_review", "Pending review"],
  ["approved", "Approved"],
  ["queued,uploading", "Uploading"],
  ["uploaded", "Uploaded"],
  ["scheduled", "Scheduled"],
  ["published", "Published"],
  ["rejected", "Rejected"],
  ["failed", "Failed"],
  ["", "All"],
];
const PERMISSIONS = ["unknown", "original", "licensed", "authorized", "fair_use_claimed", "not_authorized"];
const EDITABLE = ["pending_review", "approved", "rejected", "failed"];
const TZ = "Asia/Karachi";

export default function Videos({ params }) {
  const [tab, setTab] = useState(params.get("status") ?? "pending_review");
  const [videos, setVideos] = useState([]);
  const [selected, setSelected] = useState(params.get("id") ? Number(params.get("id")) : null);
  const [err, setErr] = useState("");
  const [scanMsg, setScanMsg] = useState("");
  const [addOpen, setAddOpen] = useState(false);

  const load = () =>
    api.get(`/api/videos${tab ? `?status=${tab}` : ""}`).then((v) => { setVideos(v); setErr(""); }).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [tab]);
  useEffect(() => {
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, [tab]);

  return (
    <div>
      <div className="row between">
        <h1>Videos</h1>
        <div className="row">
          <button className="primary" onClick={() => setAddOpen(true)}>+ Add video</button>
          <button onClick={async () => {
            setScanMsg("Scanning…");
            try {
              const r = await api.post("/api/ingest/scan");
              setScanMsg(r.imported.length ? `Imported ${r.imported.length} clip(s)` : `No new clips in ${r.incoming_dir}`);
              load();
            } catch (e) { setScanMsg(e.message); }
          }}>Scan incoming folder</button>
          <span className="muted small">{scanMsg}</span>
        </div>
      </div>
      <div className="tabs">
        {TABS.map(([k, label]) => (
          <button key={label} className={tab === k ? "tab active" : "tab"} onClick={() => { setTab(k); }}>{label}</button>
        ))}
      </div>
      {err && <div className="error">{err}</div>}
      <div className="split">
        <div className="list card">
          {videos.length === 0 && <div className="muted">No videos here.</div>}
          {videos.map((v) => (
            <div key={v.id} className={`list-item ${selected === v.id ? "sel" : ""}`} onClick={() => setSelected(v.id)}>
              <div className="li-title">{v.title || <i className="muted">(no title)</i>}</div>
              <div className="li-meta">
                <span className={`pill s-${v.status}`}>{v.status.replace("_", " ")}</span>
                <span>#{v.id}</span>
                <span>{fmtDuration(v.duration)}</span>
                <span>{v.resolution}</span>
                {!v.validation_ok && <span className="pill bad">invalid</span>}
              </div>
            </div>
          ))}
        </div>
        <div className="detail">
          {selected ? <VideoDetail id={selected} onChange={load} onDeleted={() => { setSelected(null); load(); }} /> : <div className="muted card">Select a video.</div>}
        </div>
      </div>
      {addOpen && (
        <AddVideoDialog onClose={() => setAddOpen(false)}
          onAdded={(id) => { setAddOpen(false); setTab("pending_review"); setSelected(id); load(); }} />
      )}
    </div>
  );
}

function AddVideoDialog({ onClose, onAdded }) {
  const [file, setFile] = useState(null);
  const [f, setF] = useState({
    title: "", topic: "", context_notes: "", original_commentary: "", source_url: "", source_creator: "",
    permission_status: "original", footage_type: "original", permission_notes: "", clip_start: "", clip_end: "",
  });
  const [progress, setProgress] = useState(null);
  const [err, setErr] = useState("");
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const submit = () => {
    if (!file) return setErr("Choose an .mp4 file.");
    setErr("");
    const fd = new FormData();
    fd.append("file", file);
    Object.entries(f).forEach(([k, v]) => fd.append(k, v));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/videos/import");
    xhr.setRequestHeader("X-Requested-With", "ytshorts");
    xhr.upload.onprogress = (e) => e.lengthComputable && setProgress(Math.round((e.loaded / e.total) * 100));
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300) onAdded(data.id);
      else { setProgress(null); setErr(data.detail || `Upload failed (HTTP ${xhr.status})`); }
    };
    xhr.onerror = () => { setProgress(null); setErr("Network error while sending the file."); };
    setProgress(0);
    xhr.send(fd);
  };

  const busy = progress !== null;
  return (
    <div className="modal-bg" onClick={busy ? undefined : onClose}>
      <div className="modal card wide" onClick={(e) => e.stopPropagation()}>
        <h3>Add video</h3>
        <p className="muted small">
          The clip is checked (vertical, ≤ 3 min, H.264/AAC) and added to <b>Pending review</b>. Nothing is sent to YouTube
          until you approve it and choose an upload option.
        </p>
        <label>MP4 file</label>
        <input type="file" accept="video/mp4,.mp4" onChange={(e) => setFile(e.target.files[0] || null)} disabled={busy} />
        <label>Title <span className="muted small">(leave empty to auto-generate)</span></label>
        <input value={f.title} maxLength={100} onChange={set("title")} disabled={busy} />
        <div className="row">
          <div className="grow"><label>Topic</label><input value={f.topic} onChange={set("topic")} placeholder="GTA 6 vs GTA 5" disabled={busy} /></div>
          <div className="grow"><label>Clip start (s)</label><input value={f.clip_start} onChange={set("clip_start")} disabled={busy} /></div>
          <div className="grow"><label>Clip end (s)</label><input value={f.clip_end} onChange={set("clip_end")} disabled={busy} /></div>
        </div>
        <label>What happens in the clip <span className="muted small">(used for the AI title/description)</span></label>
        <textarea rows={2} value={f.context_notes} onChange={set("context_notes")} disabled={busy} />
        <label>Your original commentary / value added</label>
        <textarea rows={2} value={f.original_commentary} onChange={set("original_commentary")} disabled={busy} />
        <div className="row">
          <div className="grow"><label>Source URL</label><input value={f.source_url} onChange={set("source_url")} disabled={busy} /></div>
          <div className="grow"><label>Source creator</label><input value={f.source_creator} onChange={set("source_creator")} disabled={busy} /></div>
        </div>
        <div className="row">
          <div className="grow">
            <label>Permission status</label>
            <select value={f.permission_status} onChange={set("permission_status")} disabled={busy}>
              {PERMISSIONS.map((p) => <option key={p} value={p}>{p.replaceAll("_", " ")}</option>)}
            </select>
          </div>
          <div className="grow">
            <label>Footage type</label>
            <select value={f.footage_type} onChange={set("footage_type")} disabled={busy}>
              <option value="original">original</option><option value="licensed">licensed</option>
              <option value="authorized">authorized</option><option value="third_party">third party</option>
            </select>
          </div>
        </div>
        <label>Permission evidence / notes</label>
        <input value={f.permission_notes} onChange={set("permission_notes")} disabled={busy} />
        {busy && (
          <>
            <div className="bar" style={{ marginTop: 12 }}><div className="fill" style={{ width: `${progress}%` }} /></div>
            <div className="muted small">{progress < 100 ? `Sending ${progress}%…` : "Checking video and generating metadata…"}</div>
          </>
        )}
        {err && <div className="error">{err}</div>}
        <div className="row" style={{ marginTop: 12 }}>
          <button className="primary" disabled={busy} onClick={submit}>Add video</button>
          <button disabled={busy} onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

function VideoDetail({ id, onChange, onDeleted }) {
  const [v, setV] = useState(null);
  const [form, setForm] = useState(null);
  const [msg, setMsg] = useState({ text: "", kind: "" });
  const [busy, setBusy] = useState(false);
  const [settings, setSettings] = useState(null);
  const [scheduleAt, setScheduleAt] = useState("");
  const [approveOpen, setApproveOpen] = useState(false);

  const load = () =>
    api.get(`/api/videos/${id}`).then((d) => {
      setV(d);
      setForm({
        title: d.title || "", description: d.description || "", hashtags: d.hashtags.join(" "), tags: d.tags.join(", "),
        category_id: d.category_id, ...d.source,
      });
      setScheduleAt((prev) => prev || toLocalInput(d.suggested_publish_at, TZ));
    }).catch((e) => setMsg({ text: e.message, kind: "error" }));

  useEffect(() => { setScheduleAt(""); setMsg({ text: "", kind: "" }); load(); api.get("/api/settings").then(setSettings); }, [id]);
  useEffect(() => {
    if (!v) return;
    const active = v.uploads.some((u) => ["queued", "uploading", "paused"].includes(u.upload_status));
    if (!active) return;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [v]);

  if (!v || !form) return <div className="muted card">Loading…</div>;
  const editable = EDITABLE.includes(v.status);

  const run = async (fn, ok) => {
    setBusy(true);
    setMsg({ text: "", kind: "" });
    try {
      await fn();
      setMsg({ text: ok, kind: "ok" });
      await load();
      onChange();
    } catch (e) {
      setMsg({ text: e.message, kind: "error" });
    } finally {
      setBusy(false);
    }
  };

  const save = () => run(() => api.patch(`/api/videos/${id}`, {
    title: form.title, description: form.description,
    hashtags: form.hashtags.split(/[\s,]+/).filter(Boolean), tags: form.tags.split(",").map((t) => t.trim()).filter(Boolean),
    category_id: form.category_id, original_source_url: form.original_source_url || "", source_creator: form.source_creator || "",
    permission_status: form.permission_status, permission_notes: form.permission_notes || "", footage_type: form.footage_type || "",
    original_commentary: form.original_commentary || "",
  }), "Saved.");

  const upload = (mode, publish_at) => run(() => api.post(`/api/videos/${id}/upload`, { mode, publish_at }), `Queued ${mode} upload.`);
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const publicOk = settings?.allow_public_publishing;

  return (
    <div className="card">
      <div className="row between">
        <h2>#{v.id} <span className={`pill s-${v.status}`}>{v.status.replace("_", " ")}</span></h2>
        <span className="muted small">metadata: {v.metadata_source || "—"}</span>
      </div>
      {msg.text && <div className={msg.kind === "error" ? "error" : "okmsg"}>{msg.text}</div>}

      <div className="detail-grid">
        <div>
          <video className="preview" src={v.preview_url} controls preload="metadata" />
          <dl className="facts">
            <dt>Duration</dt><dd>{fmtDuration(v.duration)}</dd>
            <dt>Resolution</dt><dd>{v.resolution || "—"} {v.frame_rate ? `@ ${v.frame_rate} fps` : ""}</dd>
            <dt>Codecs</dt><dd>{v.video_codec || "—"} / {v.audio_codec || "no audio"}</dd>
            <dt>Size</dt><dd>{fmtBytes(v.file_size)}</dd>
            <dt>Clip</dt><dd>{v.clip_start_time ?? "—"}s → {v.clip_end_time ?? "—"}s</dd>
            <dt>Suggested</dt><dd>{fmtLocal(v.suggested_publish_at, TZ)}</dd>
            <dt>File</dt><dd className="small">{v.file_name}</dd>
          </dl>
          {v.validation_errors.map((e) => <div key={e} className="error small">✖ {e}</div>)}
          {v.validation_warnings.map((e) => <div key={e} className="warnmsg small">⚠ {e}</div>)}
          {v.validation_ok && v.validation_errors.length === 0 && <div className="okmsg small">✔ Passed Shorts validation</div>}
        </div>

        <div>
          <label>Title <span className="muted small">{form.title.length}/100</span></label>
          <input value={form.title} maxLength={100} onChange={set("title")} disabled={!editable} />
          <label>Description</label>
          <textarea rows={6} value={form.description} onChange={set("description")} disabled={!editable} />
          <label>Hashtags <span className="muted small">(max 5)</span></label>
          <input value={form.hashtags} onChange={set("hashtags")} disabled={!editable} placeholder="#GTA6 #Gaming" />
          <label>Tags <span className="muted small">(comma separated)</span></label>
          <input value={form.tags} onChange={set("tags")} disabled={!editable} />
          <label>Category</label>
          <select value={form.category_id} onChange={set("category_id")} disabled={!editable}>
            <option value="20">Gaming</option><option value="24">Entertainment</option><option value="22">People &amp; Blogs</option>
          </select>
          {v.review_notes && <div className="note small">{v.review_notes}</div>}

          <h3>Source &amp; rights</h3>
          <label>Original source URL</label>
          <input value={form.original_source_url || ""} onChange={set("original_source_url")} disabled={!editable} />
          <label>Source creator</label>
          <input value={form.source_creator || ""} onChange={set("source_creator")} disabled={!editable} />
          <div className="row">
            <div className="grow">
              <label>Permission status</label>
              <select value={form.permission_status} onChange={set("permission_status")} disabled={!editable}>
                {PERMISSIONS.map((p) => <option key={p} value={p}>{p.replaceAll("_", " ")}</option>)}
              </select>
            </div>
            <div className="grow">
              <label>Footage type</label>
              <select value={form.footage_type || ""} onChange={set("footage_type")} disabled={!editable}>
                <option value="">—</option><option value="original">original</option><option value="licensed">licensed</option>
                <option value="authorized">authorized</option><option value="third_party">third party</option>
              </select>
            </div>
          </div>
          <label>Permission evidence / notes</label>
          <textarea rows={2} value={form.permission_notes || ""} onChange={set("permission_notes")} disabled={!editable} />
          <label>Original commentary / value added</label>
          <textarea rows={2} value={form.original_commentary || ""} onChange={set("original_commentary")} disabled={!editable} />
          <div className="small">Rights review: <span className={`pill r-${v.source.rights_review_status}`}>{v.source.rights_review_status}</span></div>
          <p className="disclaimer small">
            Copyright permission and YouTube monetization eligibility are separate issues. Editing, subtitles, cropping,
            zooming or AI voiceovers do not by themselves make third-party footage safe to use. Clips need meaningful original
            commentary or value (see YouTube's reused-content policy).
          </p>

          {editable && (
            <div className="row">
              <button disabled={busy} onClick={save}>Save changes</button>
              <button disabled={busy} onClick={() => run(() => api.post(`/api/videos/${id}/generate-metadata`), "Metadata regenerated.")}>Regenerate metadata (AI)</button>
            </div>
          )}
        </div>
      </div>

      <h3>Actions</h3>
      <div className="actions">
        {["pending_review", "rejected"].includes(v.status) && <button className="primary" disabled={busy} onClick={() => setApproveOpen(true)}>Approve</button>}
        {["pending_review", "approved", "failed"].includes(v.status) && (
          <button disabled={busy} onClick={() => { const r = prompt("Reason for rejection?"); if (r !== null) run(() => api.post(`/api/videos/${id}/reject`, { reason: r }), "Rejected."); }}>Reject</button>
        )}
        {["approved", "failed"].includes(v.status) && (
          <>
            <button disabled={busy} onClick={() => upload("private")}>Upload privately</button>
            <button disabled={busy} onClick={() => upload("unlisted")}>Upload unlisted</button>
            <span className="schedule-box">
              <input type="datetime-local" value={scheduleAt} onChange={(e) => setScheduleAt(e.target.value)} />
              <span className="muted small">PKT</span>
              <button disabled={busy || !publicOk} title={publicOk ? "" : "Enable public publishing in Settings first"}
                onClick={() => upload("scheduled", scheduleAt || null)}>Schedule</button>
            </span>
            <button className="danger" disabled={busy || !publicOk} title={publicOk ? "" : "Enable public publishing in Settings first"}
              onClick={() => { if (confirm("Publish this video PUBLICLY right now?")) upload("public"); }}>Publish now</button>
          </>
        )}
        {!["uploading", "queued", "uploaded", "scheduled", "published"].includes(v.status) && (
          <button className="danger ghost" disabled={busy} onClick={() => { if (confirm("Delete this clip? The file moves to data/trash.")) run(() => api.del(`/api/videos/${id}`), "Deleted.").then(onDeleted); }}>Delete</button>
        )}
      </div>
      {!publicOk && ["approved", "failed"].includes(v.status) && (
        <div className="muted small">Scheduling and public publishing are disabled until you enable them in Settings. Private and unlisted uploads are available.</div>
      )}

      {approveOpen && (
        <ApproveDialog v={v} onClose={() => setApproveOpen(false)}
          onApprove={(rights_confirmed, notes) => { setApproveOpen(false); run(() => api.post(`/api/videos/${id}/approve`, { rights_confirmed, notes }), "Approved."); }} />
      )}

      <h3>Upload history</h3>
      {v.uploads.length === 0 && <div className="muted">Not uploaded yet.</div>}
      {v.uploads.map((u) => <UploadRow key={u.id} u={u} onAction={(fn, ok) => run(fn, ok)} busy={busy} />)}
    </div>
  );
}

function ApproveDialog({ v, onClose, onApprove }) {
  const [checked, setChecked] = useState(false);
  const [notes, setNotes] = useState("");
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal card" onClick={(e) => e.stopPropagation()}>
        <h3>Approve “{v.title}”</h3>
        <p className="small">Permission status: <b>{v.source.permission_status.replaceAll("_", " ")}</b>{v.source.source_creator ? ` · Source: ${v.source.source_creator}` : ""}</p>
        <label className="check">
          <input type="checkbox" checked={checked} onChange={(e) => setChecked(e.target.checked)} />
          I reviewed the source rights for this clip, and it adds meaningful original commentary, analysis or entertainment value.
        </label>
        <label>Review notes (optional)</label>
        <textarea rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
        <div className="row">
          <button className="primary" disabled={!checked} onClick={() => onApprove(checked, notes)}>Approve</button>
          <button onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

function UploadRow({ u, onAction, busy }) {
  const pct = Math.round((u.progress || 0) * 100);
  return (
    <div className="upload-row">
      <div className="row between">
        <div>
          <span className={`pill s-${u.upload_status}`}>{u.upload_status}</span> <b>{u.publish_mode}</b>
          {u.scheduled_at && <> · publishes {fmtLocal(u.scheduled_at, TZ)} PKT</>}
          {u.youtube_video_id && <> · <a href={u.youtube_url} target="_blank" rel="noreferrer">{u.youtube_video_id}</a> · <a href={u.studio_url} target="_blank" rel="noreferrer">Studio</a></>}
        </div>
        <div className="row">
          {["queued", "uploading", "paused"].includes(u.upload_status) && (
            <button disabled={busy} onClick={() => onAction(() => api.post(`/api/uploads/${u.id}/cancel`), "Cancellation requested.")}>Cancel</button>
          )}
          {["failed", "paused"].includes(u.upload_status) && (
            <button disabled={busy} onClick={() => onAction(() => api.post(`/api/uploads/${u.id}/retry`), "Retry queued.")}>Retry</button>
          )}
          {u.youtube_video_id && (
            <button disabled={busy} onClick={() => onAction(() => api.post(`/api/uploads/${u.id}/verify`), "Status refreshed from YouTube.")}>Check on YouTube</button>
          )}
        </div>
      </div>
      {["uploading", "paused", "queued"].includes(u.upload_status) && u.total_bytes ? (
        <div className="bar"><div className="fill" style={{ width: `${pct}%` }} /></div>
      ) : null}
      <div className="muted small">
        attempts {u.upload_attempts} · insert calls {u.insert_calls}
        {u.resumable_session && " · resumable session saved"} · {fmtBytes(u.bytes_uploaded)} / {fmtBytes(u.total_bytes)} · by {u.requested_by}
      </div>
      {u.last_error && <div className={u.upload_status === "failed" ? "error small" : "warnmsg small"}>{u.error_kind ? `[${u.error_kind}] ` : ""}{u.last_error}</div>}
    </div>
  );
}
