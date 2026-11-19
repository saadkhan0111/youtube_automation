import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Settings({ params, auth, reloadAuth }) {
  const [s, setS] = useState(null);
  const [times, setTimes] = useState("");
  const [msg, setMsg] = useState({ text: "", kind: "" });
  const [busy, setBusy] = useState(false);

  const load = () => api.get("/api/settings").then((d) => { setS(d); setTimes(d.publishing_times.join(", ")); });
  useEffect(() => {
    load();
    const o = params.get("oauth");
    if (o === "connected") setMsg({ text: "YouTube channel connected.", kind: "ok" });
    if (o === "error") setMsg({ text: params.get("message") || "OAuth failed.", kind: "error" });
  }, []);

  const act = async (fn, ok) => {
    setBusy(true);
    try {
      const r = await fn();
      setMsg({ text: typeof ok === "function" ? ok(r) : ok, kind: "ok" });
      reloadAuth();
    } catch (e) {
      setMsg({ text: e.message, kind: "error" });
    } finally {
      setBusy(false);
    }
  };

  const connect = () => act(async () => {
    const r = await api.post("/api/auth/start");
    window.location.href = r.auth_url;
  }, "Redirecting to Google…");

  const save = (patch) => act(async () => { const d = await api.put("/api/settings", patch); setS(d); setTimes(d.publishing_times.join(", ")); }, "Settings saved.");

  // n publishing times evenly between 09:00 and 23:30 local time, rounded to 5 minutes
  const spreadTimes = (n) => {
    const start = 9 * 60, end = 23 * 60 + 30;
    const step = n > 1 ? (end - start) / (n - 1) : 0;
    const out = [];
    for (let i = 0; i < n; i++) {
      const m = Math.round((start + step * i) / 5) * 5;
      const hhmm = `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
      if (!out.includes(hhmm)) out.push(hhmm);
    }
    return out.join(", ");
  };

  if (!s) return <div className="muted">Loading…</div>;
  const num = (k) => (e) => setS({ ...s, [k]: Number(e.target.value) });

  return (
    <div>
      <h1>Settings</h1>
      {msg.text && <div className={msg.kind === "error" ? "error" : "okmsg"}>{msg.text}</div>}

      <section className="card">
        <h3>YouTube connection</h3>
        {!auth?.client_secret_present && (
          <div className="banner warn">
            OAuth client file missing. Save your Google Cloud <b>Desktop app</b> OAuth client JSON as <code>backend/credentials/client_secret.json</code>, then reload. Sign-in returns to <code>{auth?.redirect_uri}</code> (a loopback address, which Desktop clients allow without extra setup).
          </div>
        )}
        {auth?.connected ? (
          <div className="row">
            {auth.channel.thumbnail_url && <img className="avatar" src={auth.channel.thumbnail_url} alt="" />}
            <div className="grow">
              <div><b>{auth.channel.name}</b> <span className="muted small">{auth.channel.id}</span></div>
              <div className="muted small">Scopes: {auth.granted_scopes.map((x) => x.split("/").pop()).join(", ")}</div>
            </div>
            <button disabled={busy} onClick={() => act(() => api.post("/api/auth/verify"), (r) => `Token OK — channel ${r.channel.name} verified.`)}>Test connection</button>
            <button disabled={busy} onClick={connect}>Reconnect Google account</button>
            <button className="danger ghost" disabled={busy} onClick={() => { if (confirm("Disconnect and revoke this app's access?")) act(() => api.post("/api/auth/disconnect"), "Disconnected."); }}>Disconnect</button>
          </div>
        ) : (
          <div className="row">
            <span>Status: <span className="pill warn">{auth?.status || "unknown"}</span></span>
            {auth?.last_error && <span className="error small">{auth.last_error}</span>}
            <button className="primary" disabled={busy || !auth?.client_secret_present} onClick={connect}>
              {auth?.channel ? "Reconnect Google account" : "Connect YouTube channel"}
            </button>
          </div>
        )}
        <p className="muted small">Connecting never uploads anything. Tokens are stored only on this machine in backend/credentials/tokens.</p>
      </section>

      <section className="card">
        <h3>Publishing</h3>
        <div className="form-grid">
          <label>Timezone</label>
          <input value={s.timezone} onChange={(e) => setS({ ...s, timezone: e.target.value })} />
          <label>Daily publishing limit</label>
          <input type="number" min={1} max={50} value={s.daily_upload_limit} onChange={num("daily_upload_limit")} />
          <label>Publishing times (HH:MM)</label>
          <div>
            <input value={times} onChange={(e) => setTimes(e.target.value)} placeholder="13:00, 18:00, 21:00" />
            {(() => {
              const n = times.split(/[,\s]+/).filter(Boolean).length;
              const lim = Number(s.daily_upload_limit) || 1;
              return (
                <div className="small muted" style={{ marginTop: 4 }}>
                  One video per time: <b>{Math.min(n, lim)} per day</b> with these settings.
                  {n < lim && <> <span className="warn-text">The limit is {lim} but there are only {n} times.</span>{" "}
                    <button className="tiny" type="button" onClick={() => setTimes(spreadTimes(lim))}>Spread {lim} times evenly (09:00–23:30)</button></>}
                </div>
              );
            })()}
          </div>
          <label>Minimum lead time (minutes)</label>
          <input type="number" min={15} value={s.min_lead_minutes} onChange={num("min_lead_minutes")} />
          <label>Human approval</label>
          <div>Always required <span className="muted small">(only approved clips can be uploaded)</span></div>
        </div>
        <button disabled={busy} onClick={() => save({
          timezone: s.timezone, daily_upload_limit: s.daily_upload_limit, min_lead_minutes: s.min_lead_minutes,
          publishing_times: times.split(/[,\s]+/).filter(Boolean),
        })}>Save publishing settings</button>
      </section>

      <section className="card">
        <h3>Public publishing &amp; automation</h3>
        <label className="check">
          <input type="checkbox" checked={s.allow_public_publishing} disabled={busy} onChange={(e) => {
            const on = e.target.checked;
            if (on && !confirm("Allow scheduled and immediate PUBLIC publishing? Until now only private/unlisted uploads were possible.")) return;
            save({ allow_public_publishing: on });
          }} />
          Allow public publishing (scheduled publication and “Publish now”)
        </label>
        <label className="check">
          <input type="checkbox" checked={s.enabled} disabled={busy} onChange={(e) => {
            const on = e.target.checked;
            if (on && !confirm("Automatically schedule APPROVED, rights-cleared clips into free publishing slots?")) return;
            save({ enabled: on });
          }} />
          Auto-schedule approved clips into free slots (only clips you approved; needs public publishing enabled)
        </label>
      </section>

      <section className="card">
        <h3>Quota safety limits</h3>
        <p className="muted small">These are this app's own limits, below Google's defaults (100 uploads, 100 searches, 10,000 other units per day). Uploads stop when a limit is reached.</p>
        <div className="form-grid">
          <label>Max upload calls / day</label>
          <input type="number" min={0} max={100} value={s.max_daily_upload_calls} onChange={num("max_daily_upload_calls")} />
          <label>Max search calls / day</label>
          <input type="number" min={0} max={100} value={s.max_daily_search_calls} onChange={num("max_daily_search_calls")} />
          <label>Max other API units / day</label>
          <input type="number" min={0} max={10000} value={s.max_daily_other_quota_units} onChange={num("max_daily_other_quota_units")} />
        </div>
        <button disabled={busy} onClick={() => save({
          max_daily_upload_calls: s.max_daily_upload_calls, max_daily_search_calls: s.max_daily_search_calls,
          max_daily_other_quota_units: s.max_daily_other_quota_units,
        })}>Save limits</button>
      </section>

      <section className="card">
        <h3>System</h3>
        <dl className="facts">
          <dt>Incoming folder</dt><dd><code>{s.incoming_dir}</code></dd>
          <dt>Library folder</dt><dd><code>{s.library_dir}</code></dd>
          <dt>FFprobe</dt><dd>{s.ffprobe_available ? <span className="pill ok">found</span> : <span className="pill bad">missing: install FFmpeg</span>}</dd>
          <dt>AI metadata</dt><dd>{s.ai_metadata.api_key_present ? <span className="pill ok">{s.ai_metadata.model}</span> : <span className="pill warn">no ANTHROPIC_API_KEY (template fallback)</span>}</dd>
          <dt>Agent API</dt><dd>{s.agent_api_enabled ? <span className="pill ok">enabled</span> : <span className="pill">disabled</span>}</dd>
        </dl>
      </section>
    </div>
  );
}
