import React, { useEffect, useState } from "react";
import { api } from "./api.js";
import Overview from "./pages/Overview.jsx";
import Videos from "./pages/Videos.jsx";
import Calendar from "./pages/Calendar.jsx";
import Quota from "./pages/Quota.jsx";
import Settings from "./pages/Settings.jsx";
import Clipper from "./pages/Clipper.jsx";
import Studio from "./pages/Studio.jsx";

const PAGES = [
  ["overview", "Overview", Overview],
  ["clipper", "AI Clipper", Clipper],
  ["studio", "Studio", Studio],
  ["videos", "Videos", Videos],
  ["calendar", "Calendar", Calendar],
  ["quota", "Quota", Quota],
  ["settings", "Settings", Settings],
];

function useHashRoute() {
  const parse = () => {
    const raw = window.location.hash.replace(/^#\/?/, "");
    const [path, query = ""] = raw.split("?");
    return { page: path || "overview", params: new URLSearchParams(query) };
  };
  const [route, setRoute] = useState(parse);
  useEffect(() => {
    const on = () => setRoute(parse());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

function Login({ onDone }) {
  const [token, setToken] = useState("");
  const [err, setErr] = useState("");
  return (
    <div className="login card">
      <h2>Sign in</h2>
      <p className="muted">This dashboard is protected by DASHBOARD_TOKEN.</p>
      <input type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="Dashboard token" />
      <button className="primary" onClick={async () => {
        try { await api.post("/api/login", { token }); onDone(); } catch (e) { setErr(e.message); }
      }}>Sign in</button>
      {err && <div className="error">{err}</div>}
    </div>
  );
}

export default function App() {
  const route = useHashRoute();
  const [auth, setAuth] = useState(null);
  const [needLogin, setNeedLogin] = useState(false);

  const loadAuth = () =>
    api.get("/api/auth/status").then(setAuth).catch((e) => { if (e.status === 401) setNeedLogin(true); });

  useEffect(() => { loadAuth(); }, [route.page]);

  if (needLogin) return <Login onDone={() => { setNeedLogin(false); loadAuth(); }} />;

  const Page = (PAGES.find((p) => p[0] === route.page) || PAGES[0])[2];
  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">Shorts Publisher</div>
        <nav>
          {PAGES.map(([key, label]) => (
            <a key={key} href={`#/${key}`} className={route.page === key ? "active" : ""}>{label}</a>
          ))}
        </nav>
        <div className="channel">
          {auth?.connected ? (
            <>
              {auth.channel?.thumbnail_url && <img src={auth.channel.thumbnail_url} alt="" />}
              <div>
                <div className="channel-name">{auth.channel?.name}</div>
                <div className="pill ok">connected</div>
              </div>
            </>
          ) : (
            <a href="#/settings" className="pill warn">{auth ? `YouTube: ${auth.status}` : "…"}</a>
          )}
        </div>
      </aside>
      <main className="content">
        <Page params={route.params} auth={auth} reloadAuth={loadAuth} />
      </main>
    </div>
  );
}
