// Friendly names for validation errors, so "Input should be less than or equal to 30" says which box it's about.
const FIELD_NAMES = {
  max_candidates: "Moments to find", min_clip_seconds: "Min length (s)", max_clip_seconds: "Max length (s)",
  source_path: "File path", part_minutes: "New part every (minutes)", max_minutes: "Stop after (minutes)",
  target_duration: "Target duration", permission_notes: "Proof of permission", url: "Link", title: "Title",
};

function fieldError(d) {
  const field = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : null;
  const name = FIELD_NAMES[field] || (typeof field === "string" ? field.replaceAll("_", " ") : null);
  const msg = (d.msg || "").replace(/^Input should be /, "must be ").replace("less than or equal to", "at most")
    .replace("greater than or equal to", "at least");
  return name && field !== "body" ? `${name}: ${msg}` : d.msg;
}

// Thin API client. Mutating requests carry the CSRF header the backend requires.
async function request(method, path, body) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (method !== "GET") opts.headers["X-Requested-With"] = "ytshorts";
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const detail = data && data.detail;
    const msg = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map(fieldError).join("; ") : `HTTP ${res.status}`;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

export const api = {
  get: (p) => request("GET", p),
  post: (p, b = {}) => request("POST", p, b),
  put: (p, b) => request("PUT", p, b),
  patch: (p, b) => request("PATCH", p, b),
  del: (p) => request("DELETE", p),
};

export function fmtLocal(iso, tz) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("en-GB", { timeZone: tz || "Asia/Karachi", dateStyle: "medium", timeStyle: "short" });
}

export function fmtDuration(s) {
  if (s == null) return "—";
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}

export function fmtBytes(n) {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

// "YYYY-MM-DDTHH:MM" in the given timezone, for <input type="datetime-local">
export function toLocalInput(iso, tz) {
  if (!iso) return "";
  const d = new Date(iso);
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
    }).formatToParts(d).map((p) => [p.type, p.value])
  );
  const hour = parts.hour === "24" ? "00" : parts.hour;
  return `${parts.year}-${parts.month}-${parts.day}T${hour}:${parts.minute}`;
}
