import React, { useEffect, useState } from "react";
import { api, fmtLocal, toLocalInput } from "../api.js";

export default function Calendar() {
  const [data, setData] = useState(null);
  const [edit, setEdit] = useState({});
  const [msg, setMsg] = useState({ text: "", kind: "" });
  const load = () => api.get("/api/calendar?days=45").then(setData).catch((e) => setMsg({ text: e.message, kind: "error" }));
  useEffect(() => { load(); }, []);
  if (!data) return <div className="muted">Loading…</div>;
  const tz = data.timezone;

  const byDay = {};
  for (const e of data.entries) {
    const day = e.scheduled_at_local.slice(0, 10);
    (byDay[day] ||= []).push(e);
  }
  const days = Object.keys(byDay).sort();

  const reschedule = async (e) => {
    try {
      await api.post(`/api/uploads/${e.id}/reschedule`, { publish_at: edit[e.id] });
      setMsg({ text: "Rescheduled.", kind: "ok" });
      setEdit({ ...edit, [e.id]: undefined });
      load();
    } catch (err) {
      setMsg({ text: err.message, kind: "error" });
    }
  };

  return (
    <div>
      <h1>Publishing calendar <span className="muted small">({tz})</span></h1>
      {msg.text && <div className={msg.kind === "error" ? "error" : "okmsg"}>{msg.text}</div>}
      <div className="grid2">
        <section className="card">
          <h3>Scheduled publications</h3>
          {days.length === 0 && <div className="muted">No scheduled videos. Approve a clip and use “Schedule” on the Videos page.</div>}
          {days.map((day) => (
            <div key={day} className="cal-day">
              <div className="cal-date">{new Date(day + "T12:00:00").toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" })}</div>
              {byDay[day].map((e) => (
                <div key={e.id} className="cal-entry">
                  <div className="cal-time">{e.scheduled_at_local.slice(11, 16)}</div>
                  <div className="grow">
                    <a href={`#/videos?id=${e.video_id}`}>{e.title}</a>{" "}
                    <span className={`pill s-${e.upload_status}`}>{e.upload_status}</span>{" "}
                    <span className={`pill s-${e.video_status}`}>{e.video_status}</span>
                    {e.youtube_video_id && <> · <a href={e.studio_url} target="_blank" rel="noreferrer">Studio</a></>}
                    {e.last_error && <div className="warnmsg small">{e.last_error}</div>}
                  </div>
                  {e.video_status !== "published" && e.upload_status !== "cancelled" && (
                    edit[e.id] !== undefined ? (
                      <span className="row">
                        <input type="datetime-local" value={edit[e.id]} onChange={(ev) => setEdit({ ...edit, [e.id]: ev.target.value })} />
                        <button onClick={() => reschedule(e)}>Save</button>
                        <button onClick={() => setEdit({ ...edit, [e.id]: undefined })}>×</button>
                      </span>
                    ) : (
                      <button onClick={() => setEdit({ ...edit, [e.id]: toLocalInput(e.scheduled_at, tz) })}>Reschedule</button>
                    )
                  )}
                </div>
              ))}
            </div>
          ))}
        </section>
        <section className="card">
          <h3>Next free slots</h3>
          <p className="muted small">Based on your publishing times and daily limit in Settings. Publishing time does not guarantee views.</p>
          <ul className="plain">
            {data.free_slots.slice(0, 12).map((s) => <li key={s.utc}>{fmtLocal(s.utc, tz)}</li>)}
          </ul>
        </section>
      </div>
    </div>
  );
}
