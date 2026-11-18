import React, { useEffect, useState } from "react";
import { api, fmtLocal } from "../api.js";
import { QuotaBar } from "./Quota.jsx";

export default function Overview() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const load = () => api.get("/api/overview").then(setData).catch((e) => setErr(e.message));
  useEffect(() => {
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);
  if (err) return <div className="error">{err}</div>;
  if (!data) return <div className="muted">Loading…</div>;
  const tz = data.timezone;
  const q = data.quota;
  const stats = [
    ["Total clips", data.total_clips, "#/videos"],
    ["Pending approval", data.pending_approvals, "#/videos?status=pending_review"],
    ["Uploaded", data.uploaded, "#/videos?status=uploaded"],
    ["Scheduled", data.scheduled, "#/calendar"],
    ["Failed uploads", data.failed_uploads, "#/videos?status=failed"],
  ];
  return (
    <div>
      <h1>Overview</h1>
      {!data.auth.connected && (
        <div className="banner warn">
          No YouTube channel connected ({data.auth.status}). <a href="#/settings">Connect in Settings</a>.
        </div>
      )}
      {q.warnings.map((w) => <div key={w} className="banner warn">{w}</div>)}
      <div className="stats">
        {stats.map(([label, value, href]) => (
          <a key={label} className="stat card" href={href}>
            <div className="stat-value">{value}</div>
            <div className="stat-label">{label}</div>
          </a>
        ))}
      </div>
      <div className="grid2">
        <section className="card">
          <h3>Today's quota <span className="muted small">(Pacific day {q.quota_day_pacific})</span></h3>
          <QuotaBar b={q.buckets.upload} label="Uploads (videos.insert)" />
          <QuotaBar b={q.buckets.other} label="Other API units" />
          <QuotaBar b={q.buckets.search} label="Search calls" />
          <div className="muted small">Next reset: {fmtLocal(q.next_reset_utc, tz)} ({tz})</div>
        </section>
        <section className="card">
          <h3>Next scheduled</h3>
          {data.upcoming.length === 0 && <div className="muted">Nothing scheduled.</div>}
          <ul className="plain">
            {data.upcoming.map((u) => (
              <li key={u.id}>
                <b>{fmtLocal(u.scheduled_at, tz)}</b> — <a href={`#/videos?id=${u.video_id}`}>{u.title}</a>{" "}
                <span className={`pill s-${u.upload_status}`}>{u.upload_status}</span>
              </li>
            ))}
          </ul>
        </section>
      </div>
      <section className="card">
        <h3>Recent activity</h3>
        <table className="table">
          <tbody>
            {data.events.map((e, i) => (
              <tr key={i} className={`lvl-${e.level}`}>
                <td className="nowrap muted">{fmtLocal(e.timestamp, tz)}</td>
                <td><span className="pill">{e.category}</span></td>
                <td>
                  {e.message}
                  {e.video_id && <> · <a href={`#/videos?id=${e.video_id}`}>video #{e.video_id}</a></>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
