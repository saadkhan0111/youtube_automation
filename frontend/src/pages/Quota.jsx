import React, { useEffect, useState } from "react";
import { api, fmtLocal } from "../api.js";

export function QuotaBar({ b, label }) {
  const pct = b.limit ? Math.min(100, (b.used / b.limit) * 100) : 100;
  const cls = b.exhausted || b.uncertain || b.remaining === 0 ? "bad" : pct > 75 ? "warnbar" : "";
  return (
    <div className="quota">
      <div className="quota-head">
        <span>{label}</span>
        <span>
          {b.used} / {b.limit} {b.unit}
          {b.exhausted && <span className="pill bad">exhausted</span>}
          {b.uncertain && <span className="pill warn">uncertain</span>}
        </span>
      </div>
      <div className="bar"><div className={`fill ${cls}`} style={{ width: `${pct}%` }} /></div>
    </div>
  );
}

export default function Quota() {
  const [q, setQ] = useState(null);
  const [log, setLog] = useState([]);
  const [msg, setMsg] = useState("");
  const load = () => {
    api.get("/api/quota").then(setQ);
    api.get("/api/quota/log?limit=200").then(setLog);
  };
  useEffect(load, []);
  if (!q) return <div className="muted">Loading…</div>;
  const rows = [
    ["upload", "Upload calls (videos.insert)"],
    ["search", "Search calls (search.list)"],
    ["other", "Other API units"],
  ];
  return (
    <div>
      <h1>Quota</h1>
      <p className="muted">{q.disclaimer}</p>
      {q.warnings.map((w) => <div key={w} className="banner warn">{w}</div>)}
      <section className="card">
        <table className="table">
          <thead>
            <tr><th>Bucket</th><th>Used</th><th>App limit</th><th>Remaining (app)</th><th>Google default</th><th>Est. remaining at Google</th><th>Requests</th><th>Failed</th><th>Retries</th><th>Uploads OK</th></tr>
          </thead>
          <tbody>
            {rows.map(([k, label]) => {
              const b = q.buckets[k];
              return (
                <tr key={k}>
                  <td>{label} {b.exhausted && <span className="pill bad">exhausted</span>} {b.uncertain && <span className="pill warn">uncertain</span>}</td>
                  <td>{b.used}</td><td>{b.limit}</td><td>{b.remaining}</td><td>{b.google_default_allowance}</td>
                  <td>{b.google_remaining_estimate}</td><td>{b.requests}</td><td>{b.failed_requests}</td><td>{b.retry_attempts}</td><td>{b.successful_uploads}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <div className="muted small">
          Quota day (Pacific): {q.quota_day_pacific} · Last reset: {fmtLocal(q.last_reset_utc)} PKT · Next reset: {fmtLocal(q.next_reset_utc)} PKT
        </div>
        {Object.values(q.buckets).some((b) => b.uncertain) && (
          <div className="row">
            <button onClick={async () => {
              if (!confirm("Only clear this after checking actual usage in Google Cloud Console > APIs & Services > Quotas. Continue?")) return;
              await api.post("/api/quota/clear-uncertain"); setMsg("Cleared."); load();
            }}>I checked Google Cloud Console — clear uncertainty warning</button>
            {msg}
          </div>
        )}
      </section>
      <section className="card">
        <h3>API request log</h3>
        <table className="table compact">
          <thead><tr><th>Time (PKT)</th><th>Method</th><th>Bucket</th><th>Cost</th><th>Result</th><th>HTTP</th><th>Reason</th><th>Retry</th><th>Upload</th></tr></thead>
          <tbody>
            {log.map((r) => (
              <tr key={r.id} className={r.result === "error" ? "lvl-error" : ""}>
                <td className="nowrap">{fmtLocal(r.timestamp)}</td><td>{r.method}</td><td>{r.bucket}</td><td>{r.estimated_cost}</td>
                <td>{r.result}</td><td>{r.http_status ?? ""}</td><td>{r.error_reason ?? ""}</td><td>{r.is_retry ? "yes" : ""}</td><td>{r.upload_id ?? ""}</td>
              </tr>
            ))}
            {log.length === 0 && <tr><td colSpan={9} className="muted">No API requests yet.</td></tr>}
          </tbody>
        </table>
      </section>
    </div>
  );
}
