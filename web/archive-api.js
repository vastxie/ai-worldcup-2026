/* Read-only browser adapter for the public AI snapshot. No server functions. */
(function (root, factory) {
  const value = factory(root);
  if (typeof module === "object" && module.exports) module.exports = value;
  if (root) root.WCArchive = value;
})(typeof window !== "undefined" ? window : null, function (root) {
  "use strict";
  const clone = value => JSON.parse(JSON.stringify(value));
  const bounded = (value, fallback, max) => Math.max(0, Math.min(max, Number.parseInt(value, 10) || fallback));
  function createAPI(snapshot) {
    if (snapshot.schema_version !== 1 || snapshot.scope !== "public-ai-only") throw new Error("Invalid archive snapshot");
    const page = (rows, q, fallback) => {
      const offset = bounded(q.get("offset"), 0, rows.length);
      const limit = bounded(q.get("limit"), fallback, 500);
      const items = rows.slice(offset, offset + limit);
      return q.get("with_total") === "1" ? { items, total: rows.length } : items;
    };
    return async function api(path, options) {
      if (String(options?.method || "GET").toUpperCase() !== "GET") throw new Error("This is a read-only AI archive");
      const u = new URL(path, "https://archive.invalid");
      if (u.origin !== "https://archive.invalid") throw new Error("External archive request rejected");
      const p = u.pathname, q = u.searchParams;
      let result;
      if (p === "/api/me") result = { login: null };
      else if (p === "/api/projections") result = snapshot.projections;
      else if (p === "/api/agents") result = snapshot.arena;
      else if (p === "/api/leaderboard") result = page(snapshot.leaderboard, q, 100);
      else if (p === "/api/picks/recent") result = q.has("limit") ? page(snapshot.picks, q, 100) : snapshot.picks;
      else if (p.startsWith("/api/picks/agent/")) result = snapshot.histories[decodeURIComponent(p.slice(17))];
      else if (p.startsWith("/api/timeline/")) result = snapshot.timelines[p.slice(14)];
      else if (p.startsWith("/api/picks/match/")) result = snapshot.picks.filter(r => r.match_no === Number(p.slice(17)));
      else if (p === "/api/posts") {
        const before = Number(q.get("before_id") || Infinity);
        const rows = snapshot.threads.filter(t => t.thread_latest_id < before)
          .filter(t => !q.has("topic_type") || t.topic_type === q.get("topic_type"))
          .filter(t => !q.has("topic_id") || t.topic_id === Number(q.get("topic_id")));
        result = rows.slice(0, bounded(q.get("limit"), 30, 100));
      } else if (p.startsWith("/api/posts/match/")) {
        const id = Number(p.slice(17));
        result = snapshot.threads.flatMap(t => [t].concat(t.children || []))
          .filter(t => t.match_no === id || (t.topic_type === "match" && t.topic_id === id))
          .sort((a, b) => b.id - a.id);
      }
      if (result === undefined) throw new Error("Public AI archive entry not found");
      return clone(result);
    };
  }
  let pending;
  function load() {
    if (!pending) pending = root.fetch("/archive/snapshot.json", { credentials: "omit", cache: "no-cache" })
      .then(r => { if (!r.ok) throw new Error("Archive data unavailable"); return r.json(); })
      .then(snapshot => ({ snapshot, request: createAPI(snapshot) }));
    return pending;
  }
  return { createAPI, api: async (path, options) => (await load()).request(path, options), snapshot: async () => (await load()).snapshot };
});
