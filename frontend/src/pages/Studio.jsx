import React, { useEffect, useState } from "react";
import { api, fmtLocal } from "../api.js";

// AI Information Video Studio: project list, New project form, and the stage workflow view.

const STATUS_LABEL = { locked: "locked", ready: "ready", running: "running", review: "needs review", approved: "approved", failed: "failed" };

export default function Studio({ params }) {
  const id = params.get("project");
  const [opts, setOpts] = useState(null);
  useEffect(() => { api.get("/api/studio/options").then(setOpts); }, []);
  if (!opts) return <div className="muted">Loading…</div>;
  return id ? <StudioProject id={Number(id)} opts={opts} /> : <StudioList opts={opts} />;
}

function fmtDur(s) {
  return s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} min` : `${s} s`;
}

function StudioList({ opts }) {
  const [projects, setProjects] = useState([]);
  const [open, setOpen] = useState(false);
  const load = () => api.get("/api/studio/projects").then(setProjects);
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, []);
  return (
    <div>
      <div className="row between">
        <h1>AI Information Video Studio</h1>
        <button className="primary" onClick={() => setOpen(true)} disabled={!opts.openai_key_present}>+ New video</button>
      </div>
      {!opts.openai_key_present && <div className="banner warn">Add OPENAI_API_KEY to backend/.env and restart the app.</div>}
      <p className="muted">
        Researched, scripted and edited information videos (gaming facts, tech, history, science, Top 10s). Every stage
        waits for your review: Research → Script → Storyboard → Assets → Voiceover → Rendering → Quality check.
      </p>
      <div className="project-grid">
        {projects.map((p) => (
          <a key={p.id} className="card project-card" href={`#/studio?project=${p.id}`}>
            <div className="li-title">{p.topic}</div>
            <div className="li-meta">
              <span className="pill">{opts.categories[p.category] || p.category}</span>
              <span>{p.video_format === "short" ? "Short 9:16" : "Long 16:9"} · {fmtDur(p.target_duration)}</span>
            </div>
            <StageBar stages={p.stages} compact />
          </a>
        ))}
        {projects.length === 0 && <div className="muted card">No videos yet.</div>}
      </div>
      {open && <NewProject opts={opts} onClose={() => setOpen(false)} onCreated={(pid) => { window.location.hash = `#/studio?project=${pid}`; }} />}
    </div>
  );
}

function StageBar({ stages, compact, current, onSelect }) {
  return (
    <div className={`stagebar ${compact ? "compact" : ""}`}>
      {stages.map((s, i) => (
        <React.Fragment key={s.key}>
          <button type="button" className={`stage st-${s.status} ${current === s.key ? "current" : ""}`}
            onClick={onSelect ? () => onSelect(s.key) : undefined} disabled={!onSelect}
            title={`${s.label}: ${STATUS_LABEL[s.status] || s.status}${s.implemented === false ? " (not built yet)" : ""}`}>
            <span className="stage-dot">{s.status === "approved" ? "✓" : s.status === "failed" ? "!" : i + 1}</span>
            {!compact && <span className="stage-label">{s.label}</span>}
          </button>
          {i < stages.length - 1 && <span className={`stage-line ${s.status === "approved" ? "done" : ""}`} />}
        </React.Fragment>
      ))}
    </div>
  );
}

function NewProject({ opts, onClose, onCreated }) {
  const [f, setF] = useState({
    topic: "", category: "gaming", video_format: "short", target_duration: 60, audience: "",
    editing_style: "viral", narration_style: "energetic", language: "en", voice: opts.default_voice,
    reference_urls: "", instructions: "", use_avatar: false,
  });
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const [lo, hi] = opts.duration_limits[f.video_format];

  const setFormat = (fmt) => {
    const [l, h] = opts.duration_limits[fmt];
    setF({ ...f, video_format: fmt, target_duration: Math.min(h, Math.max(l, fmt === "short" ? 60 : 480)) });
  };
  const submit = async () => {
    setErr("");
    setBusy(true);
    try {
      const p = await api.post("/api/studio/projects", {
        ...f, target_duration: Number(f.target_duration),
        reference_urls: f.reference_urls.split(/\s+/).map((u) => u.trim()).filter(Boolean),
      });
      onCreated(p.id);
    } catch (e) { setErr(e.message); setBusy(false); }
  };

  return (
    <div className="modal-bg" onClick={busy ? undefined : onClose}>
      <div className="modal card wide" onClick={(e) => e.stopPropagation()}>
        <h3>New information video</h3>
        <label>Video topic</label>
        <input value={f.topic} onChange={set("topic")} placeholder="e.g. 10 hidden details in the GTA 6 trailers" autoFocus />
        <div className="row">
          <div className="grow"><label>Category</label>
            <select value={f.category} onChange={set("category")}>{Object.entries(opts.categories).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></div>
          <div className="grow"><label>Format</label>
            <select value={f.video_format} onChange={(e) => setFormat(e.target.value)}>{Object.entries(opts.formats).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></div>
          <div className="grow"><label>Target duration (seconds, {lo}–{hi})</label>
            <input type="number" min={lo} max={hi} value={f.target_duration} onChange={set("target_duration")} /></div>
        </div>
        <div className="row">
          <div className="grow"><label>Target audience</label>
            <input value={f.audience} onChange={set("audience")} placeholder="e.g. GTA fans aged 16-30" /></div>
          <div className="grow"><label>Language</label>
            <select value={f.language} onChange={set("language")}>{Object.entries(opts.languages).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></div>
        </div>
        <div className="row">
          <div className="grow"><label>Editing style</label>
            <select value={f.editing_style} onChange={set("editing_style")}>{Object.entries(opts.editing_styles).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></div>
          <div className="grow"><label>Narration style</label>
            <select value={f.narration_style} onChange={set("narration_style")}>{Object.entries(opts.narration_styles).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></div>
          <div className="grow"><label>Preferred voice</label>
            <select value={f.voice} onChange={set("voice")}>{opts.voices.map((v) => <option key={v} value={v}>{v}</option>)}</select></div>
        </div>
        <label>Reference URLs <span className="muted small">(optional, one per line; used as primary sources)</span></label>
        <textarea rows={3} value={f.reference_urls} onChange={set("reference_urls")} placeholder="https://www.rockstargames.com/VI" />
        <label>Additional instructions <span className="muted small">(optional)</span></label>
        <textarea rows={2} value={f.instructions} onChange={set("instructions")} placeholder="e.g. focus on things most viewers missed; no spoilers" />
        <label className="check"><input type="checkbox" checked={f.use_avatar} onChange={set("use_avatar")} /> Show my animated avatar as an on-screen presenter</label>
        {err && <div className="error">{err}</div>}
        <div className="row" style={{ marginTop: 10 }}>
          <button className="primary" onClick={submit} disabled={busy || f.topic.trim().length < 3}>Create project</button>
          <button onClick={onClose} disabled={busy}>Cancel</button>
          <span className="muted small">Nothing runs until you start the Research stage.</span>
        </div>
      </div>
    </div>
  );
}

function StudioProject({ id, opts }) {
  const [p, setP] = useState(null);
  const [view, setView] = useState(null);
  const [msg, setMsg] = useState({ text: "", kind: "" });
  const load = () => api.get(`/api/studio/projects/${id}`).then((d) => { setP(d); setView((v) => v || d.stage); })
    .catch((e) => setMsg({ text: e.message, kind: "error" }));
  useEffect(() => { load(); }, [id]);
  useEffect(() => {
    if (!p || !p.stages.some((s) => s.status === "running")) return;
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [p]);
  if (!p) return msg.text ? <div className="error">{msg.text}</div> : <div className="muted">Loading…</div>;

  const act = async (fn, ok) => {
    setMsg({ text: "", kind: "" });
    try { const d = await fn(); if (d && d.stages) setP(d); setMsg({ text: ok, kind: "ok" }); } catch (e) { setMsg({ text: e.message, kind: "error" }); }
  };
  const stage = p.stages.find((s) => s.key === view) || p.stages[0];

  return (
    <div>
      <div className="row between">
        <h1><a href="#/studio">Studio</a> / {p.topic}</h1>
        <button className="danger ghost" onClick={() => {
          if (confirm("Delete this video project and all its generated files?")) act(() => api.del(`/api/studio/projects/${id}`).then(() => { window.location.hash = "#/studio"; }), "");
        }}>Delete project</button>
      </div>
      <div className="card">
        <StageBar stages={p.stages} current={view} onSelect={setView} />
      </div>
      {msg.text && <div className={msg.kind === "error" ? "error" : "okmsg"}>{msg.text}</div>}
      <div className="studio-grid">
        <section className="card">
          <div className="row between">
            <h3>{stage.label} <span className={`pill st-pill-${stage.status}`}>{STATUS_LABEL[stage.status] || stage.status}</span></h3>
            <div className="row">
              {["ready", "review", "failed", "approved"].includes(stage.status) && stage.key !== "complete" && (
                <button disabled={!stage.implemented} title={stage.implemented ? "" : "This stage is not built yet"}
                  onClick={() => act(() => api.post(`/api/studio/projects/${id}/stages/${stage.key}/run`),
                    `${stage.label} started.`)}>{stage.status === "ready" ? "▶ Run" : "↻ Run again"}</button>
              )}
              {stage.status === "review" && (
                <button className="primary" onClick={() => act(async () => {
                  const d = await api.post(`/api/studio/projects/${id}/stages/${stage.key}/approve`);
                  setView(d.stage);                     // move straight on to the next stage
                  return d;
                }, `${stage.label} approved.`)}>Approve</button>
              )}
            </div>
          </div>
          {stage.status === "running" && (
            <>
              <div className="bar"><div className="fill" style={{ width: `${Math.round((stage.progress || 0) * 100)}%` }} /></div>
              <div className="muted small">{stage.message}</div>
            </>
          )}
          {stage.error && <div className="error small">{stage.error}</div>}
          {!stage.implemented && stage.key !== "complete" && (
            <div className="banner warn">The {stage.label} stage isn't built yet; it arrives in a later step of the Studio plan.</div>
          )}
          {stage.status === "locked" && <div className="muted">Approve the earlier stages first.</div>}
          {stage.key === "research" && stage.status === "ready" && (
            <p className="muted">
              Research searches the web for reliable sources (official sites and reputable publications first), reads your
              reference URLs, and builds a fact sheet with a source for every claim. It takes 1–3 minutes.
            </p>
          )}
          {stage.key === "research" && ["review", "approved", "failed"].includes(stage.status) && (
            <ResearchPanel id={id} editable={stage.status !== "running"} onChanged={load} />
          )}
          {stage.key === "script" && stage.status === "ready" && (
            <p className="muted">
              The scriptwriter turns the included research facts into spoken narration: a hook in the first seconds,
              scene-by-scene narration, title, thumbnail idea and description. Every number is checked against the facts.
            </p>
          )}
          {stage.key === "script" && ["review", "approved", "failed"].includes(stage.status) && (
            <ScriptPanel id={id} target={p.target_duration} onChanged={load} />
          )}
          {stage.key === "storyboard" && stage.status === "ready" && (
            <p className="muted">
              The storyboard plans every shot: what's on screen for each line, stock search keywords, camera moves, graphics,
              sound effects, music mood and transitions, paced for the {opts.editing_styles[p.editing_style]} style.
            </p>
          )}
          {stage.key === "storyboard" && ["review", "approved", "failed"].includes(stage.status) && (
            <StoryboardPanel id={id} opts={opts.storyboard} onChanged={load} />
          )}
          {stage.key === "voice" && ["ready", "review", "approved", "failed"].includes(stage.status) && (
            <VoicePanel id={id} hasResult={stage.status !== "ready"} onChanged={load} />
          )}
          {stage.key === "assets" && ["ready", "review", "approved", "failed"].includes(stage.status) && (
            <AssetsPanel id={id} hasResult={stage.status !== "ready"} onChanged={load} />
          )}
          {stage.key === "render" && ["ready", "review", "approved", "failed"].includes(stage.status) && (
            <RenderPanel id={id} hasResult={stage.status !== "ready"} />
          )}
          {stage.key === "qa" && ["ready", "review", "approved", "failed"].includes(stage.status) && (
            <QAPanel id={id} hasResult={stage.status !== "ready"} goTo={setView} onChanged={load} />
          )}
          {stage.key === "package" && ["ready", "review", "approved", "failed"].includes(stage.status) && (
            <PackagePanel id={id} hasResult={stage.status !== "ready"} approved={stage.status === "approved"} />
          )}
        </section>
        <aside>
          <section className="card">
            <h3>Video settings</h3>
            <dl className="facts">
              <dt>Category</dt><dd>{opts.categories[p.category]}</dd>
              <dt>Format</dt><dd>{opts.formats[p.video_format]} · {fmtDur(p.target_duration)}</dd>
              <dt>Audience</dt><dd>{p.audience || "—"}</dd>
              <dt>Editing</dt><dd>{opts.editing_styles[p.editing_style]}</dd>
              <dt>Narration</dt><dd>{opts.narration_styles[p.narration_style]} · voice {p.voice}</dd>
              <dt>Language</dt><dd>{opts.languages[p.language]}</dd>
              <dt>Presenter</dt><dd>{p.use_avatar ? "avatar on" : "off"}</dd>
              <dt>References</dt><dd>{p.reference_urls.length ? p.reference_urls.map((u) => <div key={u} className="small"><a href={u} target="_blank" rel="noreferrer">{u}</a></div>) : "—"}</dd>
              <dt>Instructions</dt><dd className="small">{p.instructions || "—"}</dd>
            </dl>
          </section>
          <section className="card">
            <h3>Costs</h3>
            <div><b>${p.costs.total_usd.toFixed(2)}</b> <span className="muted small">across {p.costs.events} API calls</span></div>
            {Object.entries(p.costs.by_stage).map(([s, c]) => (
              <div key={s} className="small muted">{s}: ${c.usd.toFixed(3)}{c.unpriced_calls ? ` + ${c.unpriced_calls} unpriced` : ""}</div>
            ))}
            {p.costs.note && <div className="muted small">{p.costs.note}</div>}
            <div className="muted small">Created {fmtLocal(p.created_at)}</div>
          </section>
        </aside>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ research review
const FACT_STATUS = { verified: "verified", disputed: "disputed", uncertain: "uncertain", opinion: "opinion" };

function ResearchPanel({ id, editable, onChanged }) {
  const [r, setR] = useState(null);
  const [err, setErr] = useState("");
  const [editing, setEditing] = useState(null);
  const load = () => api.get(`/api/studio/projects/${id}/research`).then((d) => { setR(d); setErr(""); }).catch((e) => setErr(e.status === 404 ? "" : e.message));
  useEffect(() => { load(); }, [id]);
  if (err) return <div className="error small">{err}</div>;
  if (!r) return null;
  const src = Object.fromEntries(r.sources.map((s) => [s.id, s]));
  const save = async (facts, extra = {}) => {
    try { const d = await api.patch(`/api/studio/projects/${id}/research`, { facts, ...extra }); setR(d); setEditing(null); onChanged(); }
    catch (e) { setErr(e.message); }
  };
  const included = r.facts.filter((f) => f.include).length;

  return (
    <div className="research">
      <div className="muted small">
        {r.search.performed
          ? <>Web research with <b>{r.search.model}</b>: {r.search.search_calls} searches, {r.search.pages_seen} pages seen.</>
          : <span className="warnmsg">Web search was unavailable{r.search.error ? ` (${r.search.error})` : ""}; only your references were used.</span>}
        {" "}{r.references.map((ref) => (
          <span key={ref.url} className={`pill ${ref.ok ? "ok" : "bad"}`} title={ref.error || ref.title || ""}>
            {ref.ok ? "✓" : "✗"} {new URL(ref.url).hostname}</span>
        ))}
      </div>
      <h4>Summary</h4>
      <p>{r.summary}</p>
      <h4>Best angle for the video</h4>
      <AngleEditor value={r.best_angle} editable={editable} onSave={(v) => save([], { best_angle: v })} />

      <h4>Facts <span className="muted small">({included} of {r.facts.length} will be used in the script; only verified facts are included by default)</span></h4>
      {r.facts.map((f) => (
        <div key={f.id} className={`fact ${f.include ? "" : "excluded"}`}>
          {editing && editing.id === f.id ? (
            <div className="fact-edit">
              <input value={editing.claim} onChange={(e) => setEditing({ ...editing, claim: e.target.value })} />
              <textarea rows={2} value={editing.detail} onChange={(e) => setEditing({ ...editing, detail: e.target.value })} />
              <div className="row">
                <select value={editing.status} onChange={(e) => setEditing({ ...editing, status: e.target.value })}>
                  {Object.keys(FACT_STATUS).map((s) => <option key={s} value={s} disabled={s === "verified" && !f.source_ids.length}>{s}</option>)}
                </select>
                <button className="primary" onClick={() => save([{ id: f.id, claim: editing.claim, detail: editing.detail, status: editing.status }])}>Save</button>
                <button onClick={() => setEditing(null)}>Cancel</button>
              </div>
            </div>
          ) : (
            <>
              <div className="row between">
                <label className="check" style={{ margin: 0 }}>
                  <input type="checkbox" checked={f.include} disabled={!editable} onChange={(e) => save([{ id: f.id, include: e.target.checked }])} />
                  <span><span className={`pill fs-${f.status}`}>{f.status}</span> <b>{f.claim}</b></span>
                </label>
                {editable && (
                  <span className="row">
                    <button className="tiny" onClick={() => setEditing({ id: f.id, claim: f.claim, detail: f.detail, status: f.status })}>Edit</button>
                    <button className="tiny danger ghost" onClick={() => { if (confirm("Remove this fact?")) save([{ id: f.id, delete: true }]); }}>✕</button>
                  </span>
                )}
              </div>
              <div className="small">{f.detail}</div>
              <div className="small muted">
                {f.source_ids.map((sid) => src[sid] && (
                  <a key={sid} className="src-chip" href={src[sid].url} target="_blank" rel="noreferrer" title={src[sid].url}>
                    {sid} · {src[sid].publisher || new URL(src[sid].url).hostname}</a>
                ))}
                {f.flags && f.flags.map((fl) => <span key={fl} className="pill warn">{fl}</span>)}
                {f.edited && <span className="pill">edited</span>}
              </div>
            </>
          )}
        </div>
      ))}

      {r.disputed.length > 0 && (<>
        <h4>Disputed, rumoured or unconfirmed <span className="muted small">(never stated as fact in the video)</span></h4>
        <ul className="plain small">{r.disputed.map((d, i) => (
          <li key={i}><b>{d.claim}</b>: {d.explanation} {d.source_ids.map((sid) => src[sid] && <a key={sid} className="src-chip" href={src[sid].url} target="_blank" rel="noreferrer">{sid}</a>)}</li>
        ))}</ul>
      </>)}
      {r.gaps.length > 0 && (<>
        <h4>Could not be verified</h4>
        <ul className="plain small muted">{r.gaps.map((g, i) => <li key={i}>{g}</li>)}</ul>
      </>)}

      <h4>Sources ({r.sources.length})</h4>
      <table className="table compact">
        <thead><tr><th>ID</th><th>Source</th><th>Type</th><th>Reliability</th></tr></thead>
        <tbody>{r.sources.map((s) => (
          <tr key={s.id}>
            <td>{s.id}</td>
            <td><a href={s.url} target="_blank" rel="noreferrer">{s.title || s.url}</a><div className="muted small">{s.publisher}{s.user_provided ? " · your reference" : ""}</div></td>
            <td>{s.source_type}</td>
            <td><span className={`pill rel-${s.reliability}`}>{s.reliability}</span></td>
          </tr>
        ))}</tbody>
      </table>
      {r.rejected_sources.length > 0 && (
        <div className="muted small">{r.rejected_sources.length} source(s) were discarded because the search never actually visited them.</div>
      )}
    </div>
  );
}

function AngleEditor({ value, editable, onSave }) {
  const [v, setV] = useState(null);
  if (v === null) return <p>{value} {editable && <button className="tiny" onClick={() => setV(value)}>Edit</button>}</p>;
  return (
    <div>
      <textarea rows={2} value={v} onChange={(e) => setV(e.target.value)} />
      <div className="row"><button className="primary" onClick={() => { onSave(v); setV(null); }}>Save</button><button onClick={() => setV(null)}>Cancel</button></div>
    </div>
  );
}

// ------------------------------------------------------------------ script editor
const PURPOSES = ["hook", "setup", "point", "reveal", "transition", "payoff", "cta"];
const WPS_HINT = 2.8; // only for the live estimate while typing; the server recomputes exactly on save

function ScriptPanel({ id, target, onChanged }) {
  const [s, setS] = useState(null);           // saved script from the server
  const [draft, setDraft] = useState(null);   // local edits
  const [facts, setFacts] = useState({});
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");

  const load = async () => {
    try {
      const [sc, r] = await Promise.all([api.get(`/api/studio/projects/${id}/script`), api.get(`/api/studio/projects/${id}/research`)]);
      setS(sc); setDraft(clone(sc)); setFacts(Object.fromEntries(r.facts.map((f) => [f.id, f]))); setErr("");
    } catch (e) { setErr(e.status === 404 ? "" : e.message); }
  };
  useEffect(() => { load(); }, [id]);
  if (err) return <div className="error small">{err}</div>;
  if (!s || !draft) return null;

  const dirty = JSON.stringify(strip(draft)) !== JSON.stringify(strip(s));
  const setScene = (i, patch) => setDraft({ ...draft, scenes: draft.scenes.map((x, j) => (j === i ? { ...x, ...patch } : x)) });
  const move = (i, d) => {
    const sc = [...draft.scenes];
    const [x] = sc.splice(i, 1);
    sc.splice(i + d, 0, x);
    setDraft({ ...draft, scenes: sc });
  };
  const save = async () => {
    setBusy("save"); setErr("");
    try {
      const d = await api.patch(`/api/studio/projects/${id}/script`, {
        title: draft.title, description: draft.description, thumbnail: draft.thumbnail,
        scenes: draft.scenes.map(({ id: sid, purpose, narration, fact_ids, emphasis }) => ({ id: sid, purpose, narration, fact_ids, emphasis })),
      });
      setS(d); setDraft(clone(d)); onChanged();
    } catch (e) { setErr(e.message); }
    setBusy("");
  };
  const regenerate = async (sc) => {
    if (dirty) { alert("Save your changes first."); return; }
    const instruction = prompt(`Rewrite scene "${sc.narration.slice(0, 40)}…"\nOptional instruction (e.g. "funnier", "shorter", "mention the Tom Petty song"):`, "");
    if (instruction === null) return;
    setBusy(sc.id); setErr("");
    try {
      const d = await api.post(`/api/studio/projects/${id}/script/scenes/${sc.id}/regenerate`, { instruction: instruction || null });
      setS(d); setDraft(clone(d)); onChanged();
    } catch (e) { setErr(e.message); }
    setBusy("");
  };

  const liveSeconds = dirty
    ? draft.scenes.reduce((t, x) => t + (x.narration.match(/[\w'’-]+/g) || []).length / WPS_HINT + 0.25, 0)
    : s.estimated_seconds;
  const off = (liveSeconds - target) / target;
  const sceneWarnings = (i) => s.warnings.filter((w) => w.scene === i);

  return (
    <div className="script">
      <div className="row between">
        <div className="muted small">
          {draft.scenes.length} scenes · {dirty ? "~" : ""}{Math.round(liveSeconds)}s of {target}s target
          <span className={`pill ${Math.abs(off) > 0.15 ? "warn" : "ok"}`}>{off >= 0 ? "+" : ""}{Math.round(off * 100)}%</span>
          {s.model && <> · written by {s.model}</>}
        </div>
        <button className="primary" disabled={!dirty || busy} onClick={save}>{busy === "save" ? "Saving…" : dirty ? "Save changes" : "Saved"}</button>
      </div>
      {err && <div className="error small">{err}</div>}
      {s.warnings.filter((w) => w.scene === null).map((w, i) => <div key={i} className="warnmsg small">⚠ {w.message}</div>)}
      {s.warnings.length === 0 && <div className="okmsg small">✓ All checks passed: every number is backed by a cited fact, no generic intros, no repetition, length on target.</div>}

      <label>Title <span className="muted small">{draft.title.length}/100</span></label>
      <input value={draft.title} maxLength={100} onChange={(e) => setDraft({ ...draft, title: e.target.value })} />

      <h4>Narration</h4>
      {draft.scenes.map((sc, i) => (
        <div key={sc.id} className={`scene-card p-${sc.purpose}`}>
          <div className="row between">
            <div className="row">
              <span className="scene-no">{i + 1}</span>
              <select value={sc.purpose} onChange={(e) => setScene(i, { purpose: e.target.value })}>
                {PURPOSES.map((x) => <option key={x}>{x}</option>)}
              </select>
              <span className="muted small">{sc.seconds && !dirty ? `${sc.seconds}s` : ""}</span>
              {sc.fact_ids.map((fid) => (
                <span key={fid} className={`src-chip ${facts[fid] ? "" : "bad-chip"}`} title={facts[fid] ? facts[fid].claim : "unknown fact"}>{fid}</span>
              ))}
            </div>
            <div className="row">
              <button className="tiny" disabled={i === 0} onClick={() => move(i, -1)} title="Move up">↑</button>
              <button className="tiny" disabled={i === draft.scenes.length - 1} onClick={() => move(i, 1)} title="Move down">↓</button>
              <button className="tiny" disabled={!!busy} onClick={() => regenerate(sc)} title="Rewrite this scene with AI">{busy === sc.id ? "…" : "↻ Rewrite"}</button>
              <button className="tiny" onClick={() => setDraft({ ...draft, scenes: [...draft.scenes.slice(0, i + 1),
                { id: null, purpose: "point", narration: "", fact_ids: [], emphasis: [] }, ...draft.scenes.slice(i + 1)] })} title="Add a scene after this one">＋</button>
              <button className="tiny danger ghost" disabled={draft.scenes.length <= 2}
                onClick={() => setDraft({ ...draft, scenes: draft.scenes.filter((_, j) => j !== i) })} title="Delete scene">✕</button>
            </div>
          </div>
          <textarea rows={Math.max(2, Math.ceil(sc.narration.length / 90))} value={sc.narration}
            onChange={(e) => setScene(i, { narration: e.target.value })} placeholder="What the voice says in this scene" />
          <div className="row small">
            <span className="muted">Highlight on screen:</span>
            <input className="emph" value={sc.emphasis.join(", ")} placeholder="key words"
              onChange={(e) => setScene(i, { emphasis: e.target.value.split(",").map((x) => x.trim()).filter(Boolean).slice(0, 5) })} />
          </div>
          {!dirty && sceneWarnings(i).map((w, k) => <div key={k} className="warnmsg small">⚠ {w.message}</div>)}
        </div>
      ))}

      <h4>Thumbnail idea</h4>
      <div className="row">
        <input className="grow" value={draft.thumbnail.concept} onChange={(e) => setDraft({ ...draft, thumbnail: { ...draft.thumbnail, concept: e.target.value } })} />
        <input style={{ maxWidth: 220 }} value={draft.thumbnail.text} maxLength={60} placeholder="Text on thumbnail"
          onChange={(e) => setDraft({ ...draft, thumbnail: { ...draft.thumbnail, text: e.target.value } })} />
      </div>
      <h4>YouTube description <span className="muted small">(the source list is added automatically when the video is published)</span></h4>
      <textarea rows={5} value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
      {dirty && <div className="row" style={{ marginTop: 8 }}>
        <button className="primary" disabled={!!busy} onClick={save}>Save changes</button>
        <button onClick={() => setDraft(clone(s))}>Discard</button>
      </div>}
    </div>
  );
}

function clone(x) { return JSON.parse(JSON.stringify(x)); }
function strip(x) {
  return { title: x.title, description: x.description, thumbnail: x.thumbnail,
    scenes: x.scenes.map(({ id, purpose, narration, fact_ids, emphasis }) => ({ id, purpose, narration, fact_ids, emphasis })) };
}

// ------------------------------------------------------------------ storyboard
const VT_LABEL = { stock_video: "stock video", stock_image: "stock photo", ai_image: "AI illustration", gameplay: "gameplay",
  user_footage: "your footage", graphic: "graphic" };

function StoryboardPanel({ id, opts, onChanged }) {
  const [b, setB] = useState(null);
  const [err, setErr] = useState("");
  const [editing, setEditing] = useState(null);   // scene_id
  const [busy, setBusy] = useState("");
  const [overall, setOverall] = useState(null);

  const load = () => api.get(`/api/studio/projects/${id}/storyboard`).then((d) => { setB(d); setOverall(null); setErr(""); })
    .catch((e) => setErr(e.status === 404 ? "" : e.message));
  useEffect(() => { load(); }, [id]);
  if (err) return <div className="error small">{err}</div>;
  if (!b) return null;

  const apply = (d) => { setB(d); onChanged(); };
  const regen = async (sid) => {
    const instruction = prompt("Re-plan this scene's visuals.\nOptional instruction (e.g. \"use a map graphic\", \"more stock footage\"):", "");
    if (instruction === null) return;
    setBusy(sid); setErr("");
    try { apply(await api.post(`/api/studio/projects/${id}/storyboard/scenes/${sid}/regenerate`, { instruction: instruction || null })); }
    catch (e) { setErr(e.message); }
    setBusy("");
  };
  const warnCount = b.scenes.reduce((n, s) => n + s.warnings.length, 0);

  return (
    <div className="storyboard">
      <div className="muted small">
        {b.scenes.length} scenes · {b.shot_count} shots · {b.total_seconds}s{b.model ? ` · planned by ${b.model}` : ""}
        {warnCount ? <span className="pill warn">{warnCount} warning{warnCount > 1 ? "s" : ""}</span> : <span className="pill ok">all checks passed</span>}
      </div>
      <div className="card inset">
        {overall ? (
          <div className="form-grid">
            <label>Music mood</label>
            <select value={overall.music_mood} onChange={(e) => setOverall({ ...overall, music_mood: e.target.value })}>{opts.moods.map((m) => <option key={m}>{m}</option>)}</select>
            <label>AI image style</label>
            <input value={overall.visual_style} onChange={(e) => setOverall({ ...overall, visual_style: e.target.value })} />
            <label>Colour grade</label>
            <input value={overall.color_grade} onChange={(e) => setOverall({ ...overall, color_grade: e.target.value })} />
            <span />
            <div className="row">
              <button className="primary" onClick={async () => { try { apply(await api.patch(`/api/studio/projects/${id}/storyboard`, overall)); setOverall(null); } catch (e) { setErr(e.message); } }}>Save</button>
              <button onClick={() => setOverall(null)}>Cancel</button>
            </div>
          </div>
        ) : (
          <div className="row between">
            <div className="small"><b>Look:</b> {b.overall.visual_style} · <b>grade:</b> {b.overall.color_grade} · <b>music:</b> {b.overall.music_mood}</div>
            <button className="tiny" onClick={() => setOverall({ ...b.overall })}>Edit</button>
          </div>
        )}
      </div>

      {b.scenes.map((sc, i) => (
        <div key={sc.scene_id} className={`board-scene p-${sc.purpose}`}>
          <div className="row between">
            <div className="row small">
              <span className="scene-no">{i + 1}</span><span className="pill">{sc.purpose}</span>
              <span className="muted">{sc.seconds}s · music {sc.music_mood} · then {sc.transition_out.replace("_", " ")}</span>
              {sc.sound_effects.filter((x) => x !== "none").map((x, k) => <span key={k} className="sfx-chip">♪ {x}</span>)}
              {sc.edited && <span className="pill">edited</span>}
            </div>
            <div className="row">
              <button className="tiny" onClick={() => setEditing(editing === sc.scene_id ? null : sc.scene_id)}>✎ Edit</button>
              <button className="tiny" disabled={!!busy} onClick={() => regen(sc.scene_id)}>{busy === sc.scene_id ? "…" : "↻ Re-plan"}</button>
            </div>
          </div>
          <div className="narr">“{sc.narration}”</div>
          {sc.on_screen_text && <div className="small"><b>On screen:</b> {sc.on_screen_text} <span className="muted">({sc.animation})</span></div>}
          <div className="shots">
            {sc.shots.map((sh, k) => (
              <div key={k} className={`shot vt-${sh.visual_type}`} style={{ flexGrow: Math.max(0.6, sh.seconds) }}>
                <div className="shot-head"><span>{VT_LABEL[sh.visual_type]}</span><span>{sh.seconds}s</span></div>
                {sh.graphic.kind !== "none" && (
                  <div className="gfx-preview"><div className="gfx-kind">{sh.graphic.kind.replace("_", " ")}</div>
                    <div className="gfx-text">{sh.graphic.text}</div>{sh.graphic.subtext && <div className="gfx-sub">{sh.graphic.subtext}</div>}</div>
                )}
                <div className="small">{sh.visual_description}</div>
                {sh.search_keywords.length > 0 && sh.visual_type !== "graphic" && (
                  <div>{sh.search_keywords.map((k2) => <span key={k2} className="kw-chip">{k2}</span>)}</div>
                )}
                <div className="muted tiny-text">🎥 {sh.camera_movement.replaceAll("_", " ")}{sh.effects.length ? ` · ${sh.effects.join(", ")}` : ""}</div>
              </div>
            ))}
          </div>
          {sc.warnings.map((w, k) => <div key={k} className="warnmsg small">⚠ {w}</div>)}
          {editing === sc.scene_id && (
            <SceneEditor sc={sc} opts={opts} onCancel={() => setEditing(null)} onSave={async (patch) => {
              try { apply(await api.patch(`/api/studio/projects/${id}/storyboard/scenes/${sc.scene_id}`, patch)); setEditing(null); }
              catch (e) { setErr(e.message); }
            }} />
          )}
        </div>
      ))}
      {err && <div className="error small">{err}</div>}
    </div>
  );
}

function SceneEditor({ sc, opts, onSave, onCancel }) {
  const [d, setD] = useState(() => JSON.parse(JSON.stringify({
    shots: sc.shots, on_screen_text: sc.on_screen_text, animation: sc.animation, sound_effects: sc.sound_effects,
    music_mood: sc.music_mood, transition_out: sc.transition_out })));
  const setShot = (k, patch) => setD({ ...d, shots: d.shots.map((s, j) => (j === k ? { ...s, ...patch } : s)) });
  const list = (v) => v.split(",").map((x) => x.trim()).filter(Boolean);
  return (
    <div className="scene-editor">
      {d.shots.map((sh, k) => (
        <div key={k} className="shot-edit">
          <div className="row">
            <b className="small">Shot {k + 1}</b>
            <select value={sh.visual_type} onChange={(e) => setShot(k, { visual_type: e.target.value })}>{opts.visual_types.map((x) => <option key={x} value={x}>{VT_LABEL[x]}</option>)}</select>
            <select value={sh.camera_movement} onChange={(e) => setShot(k, { camera_movement: e.target.value })}>{opts.camera.map((x) => <option key={x}>{x}</option>)}</select>
            <span className="small muted">length share</span>
            <input type="number" step="0.05" min="0.05" max="1" style={{ width: 70 }} value={sh.share} onChange={(e) => setShot(k, { share: Number(e.target.value) })} />
            <button className="tiny danger ghost" disabled={d.shots.length <= 1} onClick={() => setD({ ...d, shots: d.shots.filter((_, j) => j !== k) })}>✕</button>
          </div>
          <textarea rows={2} value={sh.visual_description} onChange={(e) => setShot(k, { visual_description: e.target.value })} />
          {sh.visual_type === "graphic" ? (
            <div className="row">
              <select value={sh.graphic.kind} onChange={(e) => setShot(k, { graphic: { ...sh.graphic, kind: e.target.value } })}>{opts.graphic_kinds.map((x) => <option key={x}>{x}</option>)}</select>
              <input className="grow" placeholder="Graphic text" value={sh.graphic.text} onChange={(e) => setShot(k, { graphic: { ...sh.graphic, text: e.target.value } })} />
              <input className="grow" placeholder="Subtext" value={sh.graphic.subtext} onChange={(e) => setShot(k, { graphic: { ...sh.graphic, subtext: e.target.value } })} />
            </div>
          ) : (
            <input placeholder="Search keywords, comma separated" value={sh.search_keywords.join(", ")} onChange={(e) => setShot(k, { search_keywords: list(e.target.value) })} />
          )}
          <input placeholder="Effects, comma separated" value={sh.effects.join(", ")} onChange={(e) => setShot(k, { effects: list(e.target.value).filter((x) => opts.effects.includes(x)) })} />
        </div>
      ))}
      <button className="tiny" disabled={d.shots.length >= 5} onClick={() => setD({ ...d, shots: [...d.shots, { visual_type: "stock_video", visual_description: "", search_keywords: [], camera_movement: "slow_push_in", effects: [], share: 0.3, graphic: { kind: "none", text: "", subtext: "" } }] })}>＋ Add shot</button>
      <div className="form-grid" style={{ marginTop: 8 }}>
        <label>On-screen text</label><input value={d.on_screen_text} onChange={(e) => setD({ ...d, on_screen_text: e.target.value })} />
        <label>Animation</label><input value={d.animation} onChange={(e) => setD({ ...d, animation: e.target.value })} />
        <label>Sound effects</label><input value={d.sound_effects.join(", ")} onChange={(e) => setD({ ...d, sound_effects: list(e.target.value).filter((x) => opts.sfx.includes(x)) })} />
        <label>Music mood</label><select value={d.music_mood} onChange={(e) => setD({ ...d, music_mood: e.target.value })}>{opts.moods.map((x) => <option key={x}>{x}</option>)}</select>
        <label>Transition out</label><select value={d.transition_out} onChange={(e) => setD({ ...d, transition_out: e.target.value })}>{opts.transitions.map((x) => <option key={x}>{x}</option>)}</select>
      </div>
      <div className="row"><button className="primary" onClick={() => onSave(d)}>Save scene</button><button onClick={onCancel}>Cancel</button></div>
    </div>
  );
}

// ------------------------------------------------------------------ voiceover
function VoicePanel({ id, hasResult, onChanged }) {
  const [v, setV] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [pron, setPron] = useState(null);   // editable rows [[written, spoken]]
  const [speed, setSpeed] = useState(1);
  const [playing, setPlaying] = useState(null);
  const audioRef = React.useRef(null);
  const queueRef = React.useRef([]);

  const load = () => api.get(`/api/studio/projects/${id}/voice`).then((d) => {
    setV(d); setPron(Object.entries(d.pronunciations)); setSpeed(d.speed); setErr("");
  }).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [id, hasResult]);
  useEffect(() => () => { if (audioRef.current) audioRef.current.pause(); }, []);
  if (err) return <div className="error small">{err}</div>;
  if (!v) return null;
  const s = v.summary;

  const playScene = (sid, queue = []) => {
    if (audioRef.current) audioRef.current.pause();
    const a = new Audio(`/api/studio/projects/${id}/voice/scenes/${sid}/audio?t=${Date.now()}`);
    audioRef.current = a;
    queueRef.current = queue;
    setPlaying(sid);
    a.onended = () => { const [next, ...rest] = queueRef.current; if (next) playScene(next, rest); else setPlaying(null); };
    a.onerror = () => { setPlaying(null); setErr("Couldn't play this scene's audio."); };
    a.play();
  };
  const stop = () => { if (audioRef.current) audioRef.current.pause(); queueRef.current = []; setPlaying(null); };
  const saveSettings = async () => {
    setBusy("settings");
    try {
      const d = await api.patch(`/api/studio/projects/${id}/voice/settings`, { pronunciations: Object.fromEntries(pron.filter(([a, b]) => a && b)), speed: Number(speed) });
      setV({ ...v, ...d }); onChanged();
    } catch (e) { setErr(e.message); }
    setBusy("");
  };
  const regen = async (sid) => {
    setBusy(sid);
    try { const sum = await api.post(`/api/studio/projects/${id}/voice/scenes/${sid}/regenerate`); setV({ ...v, summary: sum }); onChanged(); }
    catch (e) { setErr(e.message); }
    setBusy("");
  };

  return (
    <div className="voice">
      {!s && <p className="muted">
        Voiceover records every scene with the project's voice, measures exact word timings with Whisper (for captions)
        and checks the audio against the script. About 5–10 seconds per scene.
      </p>}
      {s && (
        <>
          <div className="row between">
            <div className="small">
              <b>{s.total_seconds}s</b> of narration (target {s.target_seconds}s) · voice <b>{s.voice}</b> · {s.narration_style} · speed ×{s.speed}
            </div>
            {playing ? <button onClick={stop}>■ Stop</button>
              : <button className="primary" onClick={() => { const ids = s.scenes.map((x) => x.scene_id); playScene(ids[0], ids.slice(1)); }}>▶ Play full narration</button>}
          </div>
          {s.warnings.map((w, i) => <div key={i} className="warnmsg small">⚠ {w} Shorten the script, or raise the speed below.</div>)}
          {s.scenes.map((sc, i) => (
            <div key={sc.scene_id} className={`voice-scene ${playing === sc.scene_id ? "playing" : ""}`}>
              <div className="row between">
                <div className="row small">
                  <span className="scene-no">{i + 1}</span>
                  <button className="tiny" onClick={() => (playing === sc.scene_id ? stop() : playScene(sc.scene_id))}>{playing === sc.scene_id ? "■" : "▶"}</button>
                  <b>{sc.duration.toFixed(1)}s</b>
                  <span className={`pill ${sc.match >= 0.85 ? "ok" : "warn"}`} title={`Whisper heard: ${sc.heard}`}>{Math.round(sc.match * 100)}% match</span>
                  {sc.pause_after > 0 && <span className="muted">+{sc.pause_after}s pause</span>}
                </div>
                <button className="tiny" disabled={!!busy} onClick={() => regen(sc.scene_id)}>{busy === sc.scene_id ? "Recording…" : "↻ New take"}</button>
              </div>
              <div className="sentences">
                {sc.sentences.map((x, k) => <span key={k} className="sent"><span className="ts">{x.start.toFixed(1)}s</span> {x.text} </span>)}
              </div>
              {sc.spoken_text !== sc.sentences.map((x) => x.text).join(" ") && <div className="muted tiny-text">Spoken as: {sc.spoken_text}</div>}
              {sc.warnings.map((w, k) => <div key={k} className="warnmsg small">⚠ {w}</div>)}
            </div>
          ))}
        </>
      )}
      {pron && (
        <details className="card inset" open={!s}>
          <summary><b>Pronunciation & speed</b> <span className="muted small">(changes what is spoken, never the captions)</span></summary>
          <table className="table compact">
            <thead><tr><th>Written</th><th>Spoken as</th><th /></tr></thead>
            <tbody>
              {pron.map(([a, b], i) => (
                <tr key={i}>
                  <td><input value={a} onChange={(e) => setPron(pron.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} /></td>
                  <td><input value={b} onChange={(e) => setPron(pron.map((r, j) => (j === i ? [r[0], e.target.value] : r)))} /></td>
                  <td><button className="tiny danger ghost" onClick={() => setPron(pron.filter((_, j) => j !== i))}>✕</button></td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="row">
            <button className="tiny" onClick={() => setPron([...pron, ["", ""]])}>＋ Add word</button>
            <span className="small muted">Speed</span>
            <input type="range" min="0.9" max="1.15" step="0.01" value={speed} onChange={(e) => setSpeed(e.target.value)} style={{ width: 160 }} />
            <span className="small">×{Number(speed).toFixed(2)}</span>
            <button className="primary" disabled={busy === "settings"} onClick={saveSettings}>Save</button>
          </div>
          {s && <div className="muted small">After saving, use ↻ Run again: only scenes whose spoken text or speed changed are re-recorded.</div>}
        </details>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ assets
const ORIGIN_LABEL = { pexels: "Pexels", pixabay: "Pixabay", ai: "AI image", library: "Your library", upload: "Your upload", graphic: "Graphic", none: "—" };

function AssetsPanel({ id, hasResult, onChanged }) {
  const [o, setO] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [open, setOpen] = useState(null);   // asset shown in the preview modal

  const load = () => api.get(`/api/studio/projects/${id}/assets`).then((d) => { setO(d); setErr(""); }).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [id, hasResult]);
  if (!o) return err ? <div className="error small">{err}</div> : null;

  const act = async (key, fn) => {
    setBusy(key); setErr("");
    try { const d = await fn(); if (d && d.scenes) setO(d); onChanged(); }
    catch (e) { setErr(e.message); }
    setBusy("");
  };
  const replace = (sid, i, action) => act(`${sid}/${i}`, () => api.post(`/api/studio/projects/${id}/assets/shots/${sid}/${i}/replace`, { action }));
  const upload = (sid, i, file) => act(`${sid}/${i}`, async () => {
    const fd = new FormData(); fd.append("file", file);
    const r = await fetch(`/api/studio/projects/${id}/assets/shots/${sid}/${i}/upload`, { method: "POST", body: fd, headers: { "X-Requested-With": "ytshorts" } });
    const d = await r.json().catch(() => null);
    if (!r.ok) throw new Error((d && d.detail) || `HTTP ${r.status}`);
    return d;
  });
  const fill = () => act("fill", () => api.post(`/api/studio/projects/${id}/assets/fill-missing`));
  const c = o.counts, pl = o.plan;
  const gaps = c.missing + c.failed;

  return (
    <div className="assets">
      {!hasResult && (
        <div className="muted small">
          <p>Assets finds or makes one visual per shot: <b>{pl.stock}</b> stock ({o.stock_providers.length ? o.stock_providers.map((x) => ORIGIN_LABEL[x]).join(" + ") : "no stock key yet"}),
            {" "}<b>{pl.ai_image}</b> AI illustrations ({o.image_model}, {o.image_quality} quality), <b>{pl.library}</b> from your own footage
            {" "}({o.library_videos} video{o.library_videos === 1 ? "" : "s"} in your library), <b>{pl.graphic}</b> graphics drawn at render time.</p>
          {!o.stock_providers.length && pl.stock > 0 && <div className="warnmsg">⚠ No stock key: stock shots will be marked missing. Get a free key at pixabay.com/api/docs (sign up, the key is shown on that page) and add <code>PIXABAY_API_KEY=...</code> to backend/.env, then restart. You can also upload your own or fill gaps with AI images afterwards.</div>}
        </div>
      )}
      {hasResult && (
        <div className="row between">
          <div className="small"><b>{c.ready}</b> ready · {gaps > 0 ? <b className="warn-text">{gaps} need a visual</b> : "0 missing"}{c.pending > 0 && <> · <b>{c.pending}</b> changed since last run</>}</div>
          {gaps > 0 && <button disabled={!!busy} onClick={fill}>{busy === "fill" ? "Starting…" : `✨ Fill ${gaps} gap${gaps > 1 ? "s" : ""} with AI images`}</button>}
        </div>
      )}
      {err && <div className="error small">{err}</div>}
      {hasResult && o.scenes.map((sc, n) => (
        <div key={sc.scene_id} className="asset-scene">
          <div className="small"><span className="scene-no">{n + 1}</span> <span className="muted">“{sc.narration}”</span></div>
          <div className="asset-grid">
            {sc.shots.map((sh) => <ShotAsset key={sh.index} id={id} sid={sc.scene_id} sh={sh} busy={busy === `${sc.scene_id}/${sh.index}`}
              disabled={!!busy} onReplace={replace} onUpload={upload} onOpen={setOpen} />)}
          </div>
        </div>
      ))}
      {hasResult && o.credits.length > 0 && (
        <details className="small"><summary>Stock credits ({o.credits.length}), added to the description later</summary>
          <ul>{o.credits.map((x) => <li key={x}>{x}</li>)}</ul></details>
      )}
      {open && (
        <div className="modal-back" onClick={() => setOpen(null)}>
          <div className="asset-modal" onClick={(e) => e.stopPropagation()}>
            {open.kind === "video"
              ? <video src={`/api/studio/projects/${id}/assets/${open.id}/file`} controls autoPlay muted
                  onLoadedMetadata={(e) => { if (open.meta.start) e.target.currentTime = open.meta.start; }} />
              : <img src={`/api/studio/projects/${id}/assets/${open.id}/file`} alt="" />}
            <div className="small">{open.attribution || ORIGIN_LABEL[open.origin]} · {open.license}
              {open.source_url && <> · <a href={open.source_url} target="_blank" rel="noreferrer">source</a></>}</div>
            {open.prompt && <div className="muted tiny-text">Prompt: {open.prompt}</div>}
            <button onClick={() => setOpen(null)}>Close</button>
          </div>
        </div>
      )}
    </div>
  );
}

function ShotAsset({ id, sid, sh, busy, disabled, onReplace, onUpload, onOpen }) {
  const a = sh.asset;
  const fileRef = React.useRef(null);
  const ok = sh.state === "ready";
  return (
    <div className={`shot-asset ${ok ? "" : "bad"}`}>
      <div className="thumb" onClick={() => a && a.has_file && onOpen(a)} title={sh.description}>
        {a && a.has_preview ? <img src={`/api/studio/projects/${id}/assets/${a.id}/file?preview=1`} alt="" loading="lazy" />
          : sh.visual_type === "graphic" ? <div className="graphic-ph">{(sh.graphic && sh.graphic.text) || "GRAPHIC"}<span>{sh.graphic && sh.graphic.kind.replaceAll("_", " ")}</span></div>
          : <div className="graphic-ph">{busy ? "Working…" : sh.state === "pending" ? "Not fetched yet" : "No visual"}</div>}
        {a && a.kind === "video" && a.has_file && <span className="play">▶</span>}
      </div>
      <div className="tiny-text">
        <b>{VT_LABEL[sh.visual_type]}</b> · {sh.seconds}s
        {a && a.status === "ready" && !["graphic", "ai"].includes(a.origin) && <span className="pill">{ORIGIN_LABEL[a.origin]}</span>}
        {a && a.origin === "ai" && <span className="pill warn" title="Never presented as real footage">illustration</span>}
      </div>
      <div className="tiny-text muted shot-desc">{sh.description}</div>
      {a && a.note && <div className={`tiny-text ${ok ? "muted" : "warn-text"}`}>{a.note}</div>}
      {sh.stale && <div className="tiny-text warn-text">Shot changed. Run Assets again.</div>}
      {sh.visual_type !== "graphic" && (
        <div className="row wrap">
          {a && ["pexels", "pixabay", "library"].includes(a.origin) && <button className="tiny" disabled={disabled} onClick={() => onReplace(sid, sh.index, "next")}>↻ Next</button>}
          {!(a && ["pexels", "pixabay"].includes(a.origin)) && sh.visual_type.startsWith("stock") && <button className="tiny" disabled={disabled} onClick={() => onReplace(sid, sh.index, sh.visual_type)}>Stock</button>}
          <button className="tiny" disabled={disabled} onClick={() => onReplace(sid, sh.index, "ai_image")}>{busy ? "…" : a && a.origin === "ai" ? "↻ New image" : "✨ AI"}</button>
          <button className="tiny" disabled={disabled} onClick={() => fileRef.current.click()}>⬆ Upload</button>
          <input ref={fileRef} type="file" accept="video/*,image/*" hidden onChange={(e) => e.target.files[0] && onUpload(sid, sh.index, e.target.files[0])} />
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ rendering
function RenderPanel({ id, hasResult }) {
  const [r, setR] = useState(null);
  const [err, setErr] = useState("");
  useEffect(() => { api.get(`/api/studio/projects/${id}/render`).then(setR).catch((e) => setErr(e.message)); }, [id, hasResult]);
  if (err) return <div className="error small">{err}</div>;
  if (!r) return null;
  const s = r.summary;
  const musicNote = r.music_local_tracks > 0 ? `${r.music_local_tracks} track(s) of your own for “${r.music_mood}”`
    : r.jamendo_key_present ? `Jamendo (CC BY / CC BY-SA only) for “${r.music_mood}”` : null;
  return (
    <div className="render">
      {!s && (
        <div className="muted small">
          <p>Rendering builds the video: every shot with its camera move and effects, transitions, word-by-word captions,
            graphics, narration, music ducked under the voice and sound effects. A Short takes about 1–3 minutes.</p>
          <div className={musicNote ? "small" : "warnmsg"}>
            {musicNote ? <>♪ Music: {musicNote}.</> : <>⚠ No music source yet. Put .mp3 files in <code>{r.music_folder}</code>,
              or add a free <code>JAMENDO_CLIENT_ID</code> (devportal.jamendo.com) to backend/.env and restart. Without one the video renders without music.</>}
          </div>
        </div>
      )}
      {s && (
        <div className="render-result">
          <video key={s.render_id} className={s.width < s.height ? "vertical" : "horizontal"} controls preload="metadata"
            poster={`/api/studio/projects/${id}/renders/${s.render_id}/poster`}
            src={`/api/studio/projects/${id}/renders/${s.render_id}/video`} />
          <div className="render-info small">
            <div><b>Version {s.version}</b> · {s.duration}s · {s.width}×{s.height} · {s.size_mb} MB</div>
            <div>{s.shots} shots ({s.segments_reused} reused from the last render) · {s.sfx} sound effects</div>
            <div>Captions: {s.caption_style.replaceAll("_", " ")} · transitions: {s.transitions.join(", ")}{s.avatar ? " · avatar presenter" : ""}</div>
            <div>{s.music ? <>♪ {s.music.title}{s.music.artist ? ` by ${s.music.artist}` : ""} ({s.music.source}{s.music.license ? `, ${s.music.license.split(":")[0]}` : ""})</> : "♪ No music"}</div>
            {s.music && s.music.credit && <div className="muted tiny-text">Credit for the description: {s.music.credit}</div>}
            {s.warnings.map((w, i) => <div key={i} className="warnmsg">⚠ {w}</div>)}
            <a href={`/api/studio/projects/${id}/renders/${s.render_id}/video`} download>⬇ Download MP4</a>
            {r.renders.length > 1 && (
              <details><summary>Earlier renders ({r.renders.length - 1})</summary>
                <ul>{r.renders.slice(1).map((x) => <li key={x.id}>{x.status === "done"
                  ? <a href={`/api/studio/projects/${id}/renders/${x.id}/video`} target="_blank" rel="noreferrer">#{x.id} · {x.aspect}</a>
                  : <>#{x.id} · {x.status}{x.error ? `: ${x.error.slice(0, 120)}` : ""}</>} <span className="muted">{x.created_at}</span></li>)}</ul>
              </details>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ quality check
const QA_ICON = { pass: "✓", info: "i", warn: "!", fail: "✕", dismissed: "–" };
const QA_ORDER = ["fail", "warn", "info", "dismissed", "pass"];
const QA_HARD = new Set(["format", "sync", "length", "script_facts"]);
const QA_GROUPS = [["technical", "Technical"], ["content", "Content & facts"], ["visual", "Frames (AI review)"]];
const STAGE_NAME = { research: "Research", script: "Script", storyboard: "Storyboard", voice: "Voiceover", assets: "Assets", render: "Rendering" };

function QAPanel({ id, hasResult, goTo, onChanged }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const [big, setBig] = useState(null);
  const [busy, setBusy] = useState("");
  const dismiss = async (x, undo) => {
    const note = undo ? "" : window.prompt(`Dismiss “${x.label}”? Add a short note (e.g. "checked, text is correct"):`, "Checked: looks fine");
    if (note === null) return;
    setBusy(x.id);
    try { setD(await api.post(`/api/studio/projects/${id}/qa/checks/${x.id}/dismiss`, { note, undo })); onChanged && onChanged(); }
    catch (e) { setErr(e.message); }
    setBusy("");
  };
  useEffect(() => { api.get(`/api/studio/projects/${id}/qa`).then(setD).catch((e) => setErr(e.message)); }, [id, hasResult]);
  if (err) return <div className="error small">{err}</div>;
  if (!d) return null;
  const r = d.result;
  if (!r) return (
    <p className="muted small">
      The quality check inspects the finished render: format and length, loudness and clipping, dead air, black or frozen
      picture, open script/fact warnings, credits, and one frame per shot reviewed by {d.model} for misspelled or unwanted
      text, real-person likeness, logos, caption problems and glitches. Takes about a minute; the frame review costs a few cents.
    </p>
  );
  const c = r.counts;
  return (
    <div className="qa">
      <div className={`qa-banner ${r.passed ? "ok" : "bad"}`}>
        {r.passed ? "✓ Ready to publish" : `✕ ${c.fail} problem${c.fail > 1 ? "s" : ""} must be fixed before approval`}
        <span className="small"> · {c.pass} passed · {c.warn} warning{c.warn === 1 ? "" : "s"} · {c.info} note{c.info === 1 ? "" : "s"}{c.dismissed ? ` · ${c.dismissed} dismissed` : ""}</span>
      </div>
      {QA_GROUPS.map(([g, label]) => {
        const rows = r.checks.filter((x) => x.group === g);
        if (!rows.length) return null;
        return (
          <div key={g} className="qa-group">
            <div className="qa-group-title">{label}</div>
            {rows.sort((a, b) => QA_ORDER.indexOf(a.status) - QA_ORDER.indexOf(b.status)).map((x) => (
              <div key={x.id} className={`qa-row ${x.status}`}>
                <span className="qa-icon">{QA_ICON[x.status]}</span>
                <div className="qa-body">
                  <b>{x.label}</b> <span className="small">{x.detail}</span>
                  {x.status === "dismissed" && <div className="tiny-text muted">Dismissed (was {x.status_before}): {x.dismissed_note}</div>}
                  {x.frames && x.frames.length > 0 && (
                    <div className="qa-frames">{[...new Set(x.frames)].slice(0, 6).map((f) => (
                      <img key={f} src={`/api/studio/projects/${id}/qa/frames/${f}`} alt="" onClick={() => setBig(f)} />))}</div>
                  )}
                </div>
                {x.fix && !["pass", "dismissed"].includes(x.status) && <button className="tiny" onClick={() => goTo(x.fix)}>Fix in {STAGE_NAME[x.fix] || x.fix} →</button>}
                {["warn", "fail"].includes(x.status) && !(QA_HARD.has(x.id) && x.status === "fail") && <button className="tiny ghost" disabled={busy === x.id} onClick={() => dismiss(x, false)}>Dismiss</button>}
                {x.status === "dismissed" && <button className="tiny ghost" disabled={busy === x.id} onClick={() => dismiss(x, true)}>Undo</button>}
              </div>
            ))}
          </div>
        );
      })}
      <div className="muted tiny-text">Checked render #{r.render_id}. After fixing, render again and re-run this check.</div>
      {big && <div className="modal-back" onClick={() => setBig(null)}><img className="qa-big" src={`/api/studio/projects/${id}/qa/frames/${big}`} alt="" /></div>}
    </div>
  );
}

// ------------------------------------------------------------------ package
function PackagePanel({ id, hasResult, approved }) {
  const [pk, setPk] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [form, setForm] = useState(null);
  const [thumbText, setThumbText] = useState({});
  const sync = (p) => {
    setPk(p);
    if (p) setForm({ title: p.title, intro: p.intro, hashtags: p.hashtags.join(" "), tags: p.tags.join(", "), pinned_comment: p.pinned_comment });
  };
  useEffect(() => { api.get(`/api/studio/projects/${id}/package`).then((d) => sync(d.package)).catch((e) => setErr(e.message)); }, [id, hasResult, approved]);
  if (err && !pk) return <div className="error small">{err}</div>;
  if (!pk) return (
    <p className="muted small">
      Package writes 5 title options (checked against your facts), hashtags, tags and a pinned comment, builds the description
      from your sources and credits (sources and credits are added by the app, never invented), and makes 3 thumbnails from
      your best images. Approving it sends the video to the Videos queue for the normal review and schedule. Costs about a cent.
    </p>
  );
  const locked = !!pk.handoff;
  const save = async (extra = {}) => {
    setBusy("save"); setErr("");
    try {
      const d = await api.patch(`/api/studio/projects/${id}/package`, {
        title: form.title, intro: form.intro, pinned_comment: form.pinned_comment,
        hashtags: form.hashtags.split(/[\s,]+/).filter(Boolean), tags: form.tags.split(",").map((x) => x.trim()).filter(Boolean), ...extra,
      });
      sync(d.package);
    } catch (e) { setErr(e.message); }
    setBusy("");
  };
  const remake = async (i) => {
    setBusy(`t${i}`); setErr("");
    try { const d = await api.post(`/api/studio/projects/${id}/package/thumbnails/${i}`, { text: thumbText[i] }); sync(d.package); }
    catch (e) { setErr(e.message); }
    setBusy("");
  };
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  return (
    <div className="package">
      {locked && (
        <div className="qa-banner ok">✓ Sent to the publishing queue as <a href={`#/videos?status=pending_review&id=${pk.handoff.video_id}`}>video #{pk.handoff.video_id}</a>.
          <span className="small"> Review, schedule and upload it from Videos as usual.</span></div>
      )}
      {err && <div className="error small">{err}</div>}
      <div className="field"><label>Title</label>
        <input value={form.title} maxLength={100} disabled={locked} onChange={set("title")} />
        <div className="small muted">{form.title.length}/100 · keep it under 70 for phones</div>
      </div>
      {!locked && (
        <div className="title-options">
          {pk.titles.map((t) => (
            <label key={t.title} className={`title-opt ${form.title === t.title ? "on" : ""}`}>
              <input type="radio" checked={form.title === t.title} onChange={() => setForm({ ...form, title: t.title })} />
              <span>{t.title} <span className="muted tiny-text">({t.angle})</span>
                {t.warnings.map((w, i) => <span key={i} className="pill warn">{w}</span>)}</span>
            </label>
          ))}
        </div>
      )}
      <div className="field"><label>Thumbnail</label>
        <div className="thumb-grid">
          {pk.thumbnails.map((t, i) => (
            <div key={t.file} className={`thumb-opt ${pk.thumbnail === t.file ? "on" : ""}`}>
              <img src={`/api/studio/projects/${id}/package/files/${t.file}`} alt="" onClick={() => !locked && save({ thumbnail: t.file })} />
              {!locked && <div className="row">
                <input placeholder={t.text} value={thumbText[i] ?? ""} maxLength={40} onChange={(e) => setThumbText({ ...thumbText, [i]: e.target.value })} />
                <button className="tiny" disabled={!thumbText[i] || busy === `t${i}`} onClick={() => remake(i)}>{busy === `t${i}` ? "…" : "↻"}</button>
              </div>}
              <a className="tiny-text" href={`/api/studio/projects/${id}/package/files/${t.file}`} download>⬇ download</a>
            </div>
          ))}
        </div>
        <div className="small muted">Click a thumbnail to choose it. YouTube doesn't reliably apply custom thumbnails to Shorts (it uses a frame
          you pick in the YouTube app); for Shorts this is mainly for sharing. Uploading thumbnails automatically is not built yet.</div>
      </div>
      <div className="field"><label>Description intro</label>
        <textarea rows={3} value={form.intro} disabled={locked} onChange={set("intro")} />
      </div>
      <details className="small" open>
        <summary>Full description preview (sources, credits and hashtags are added automatically)</summary>
        <pre className="desc-preview">{pk.description}</pre>
      </details>
      <div className="grid2">
        <div className="field"><label>Hashtags</label><input value={form.hashtags} disabled={locked} onChange={set("hashtags")} /></div>
        <div className="field"><label>Tags (comma separated)</label><input value={form.tags} disabled={locked} onChange={set("tags")} /></div>
      </div>
      <div className="field"><label>Pinned comment (post it yourself after publishing)</label>
        <input value={form.pinned_comment} disabled={locked} onChange={set("pinned_comment")} /></div>
      {!locked && <div className="row"><button className="primary" disabled={busy === "save"} onClick={() => save()}>{busy === "save" ? "Saving…" : "Save changes"}</button>
        <span className="small muted">Then Approve to send it to the Videos queue.</span></div>}
    </div>
  );
}
