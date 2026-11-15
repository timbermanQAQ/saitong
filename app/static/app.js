"use strict";

const $ = (id) => document.getElementById(id);
const STATUS_LABEL = { todo: "待办", doing: "进行中", blocked: "受阻", done: "已完成" };
const STATUS_ORDER = ["todo", "doing", "blocked", "done"];
const STATUS_RANK = { done: 0, todo: 1, doing: 2, blocked: 3 };
const TEAM_KEY = "saitong.team";

const state = {
  tracks: [],          // /api/tracks 的赛道统计
  topicTitles: {},     // 赛道 -> 文档标题（赛题候选）
  teams: [],
  team: localStorage.getItem(TEAM_KEY) || "",
  profile: null,
  extra: {},           // 赛道 -> 手工添加的赛题名（尚未保存）
  busy: false,
};

/* ------------------------------------------------------------------ 基础设施 */

const esc = (value) => String(value ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// 文档 front matter 里的链接是外部数据，只放行 http(s)，避免 javascript: 之类的伪链接
const safeUrl = (value) => {
  const url = String(value || "").trim();
  return /^https?:\/\/[^\s]+$/i.test(url) ? url : "";
};

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = null;
    try { detail = (await res.json())?.detail; } catch { /* 非 JSON 响应 */ }
    const text = typeof detail === "string" ? detail
      : detail ? `${detail.error || "error"}${detail.message ? "：" + detail.message : ""}` : res.statusText;
    throw new Error(`${res.status} ${text}`);
  }
  return res.json();
}

let toastTimer = 0;
function toast(message, bad = false) {
  const node = $("toast");
  node.textContent = message;
  node.className = `show${bad ? " bad" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.className = ""; }, bad ? 5000 : 2500);
}

async function guard(work) {
  if (state.busy) { toast("上一条请求还在进行", true); return; }
  state.busy = true;
  document.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  try {
    await work();
  } catch (err) {
    toast(`请求失败：${err.message}`, true);
  } finally {
    state.busy = false;
    document.querySelectorAll("button").forEach((b) => { b.disabled = false; });
  }
}

const topicApi = (suffix, query = "") =>
  `/api/teams/${encodeURIComponent(state.team)}${suffix}${query}`;

/* ------------------------------------------------------------------ 顶栏 */

function renderHealth() {
  const h = state.health;
  const mode = h.search_mode === "hybrid" ? "混合检索" : "离线全文检索";
  const llm = h.llm?.enabled ? `LLM ${esc(h.llm.model)}` : "LLM 未配置（抽取式答案）";
  const pc = h.powercontext?.ok ? "PowerContext 正常" : `PowerContext 不可用${h.powercontext?.error ? "：" + esc(h.powercontext.error) : ""}`;
  const days = h.days_left ?? 0;
  const dayText = days < 0 ? `<span class="bad">已过截止 ${-days} 天</span>` : `距截止 <b>${days}</b> 天`;
  $("health").innerHTML =
    `知识库 <b>${h.seekdb?.chunks ?? "?"}</b> 块 · ${mode} · ${dayText} · ${llm} · ${pc}`;
}

function renderTeams() {
  const select = $("team-select");
  select.innerHTML = `<option value="">（未选择）</option>` + state.teams
    .map((t) => `<option value="${esc(t.name)}">${esc(t.name)}${t.tracks.length ? " · " + esc(t.tracks.join("/")) : ""}</option>`)
    .join("");
  select.value = state.team;
}

function renderTrackChips() {
  const tracks = state.profile?.tracks || [];
  $("team-tracks").innerHTML = tracks.length
    ? tracks.map((t) => `<span class="chip">${esc(t)}</span>`).join("")
    : `<span class="chip grey">还没选题</span>`;
}

/* ------------------------------------------------------------------ 队伍 */

async function loadTeams() {
  const data = await api("/api/teams");
  state.teams = data.teams;
  if (state.team && !state.teams.some((t) => t.name === state.team)) state.team = "";
  renderTeams();
}

async function openTeam(name) {
  state.team = name;
  localStorage.setItem(TEAM_KEY, name);
  renderTeams();
  if (!name) { state.profile = null; renderTrackChips(); renderPicker(); renderProgress(); $("next-body").innerHTML = ""; return; }
  state.profile = await api(`/api/teams/${encodeURIComponent(name)}`);
  state.extra = {};
  renderTrackChips();
  renderPicker();
  renderProgress();
  loadNext();
}

/* ------------------------------------------------------------------ 检索 */

function renderSearchTracks() {
  $("search-tracks").innerHTML = state.tracks.map((t) =>
    `<label class="chip" style="cursor:pointer"><input type="checkbox" value="${esc(t.name)}" style="margin-right:4px">${esc(t.name)} ${t.chunks}</label>`
  ).join("");
}

function checkedTracks(id) {
  return [...document.querySelectorAll(`#${id} input:checked`)].map((i) => i.value);
}

function hitCard(hit, n) {
  const url = safeUrl(hit.source_url);
  const link = url ? `<a href="${esc(url)}" target="_blank" rel="noreferrer">原文</a>` : "";
  return `<div class="card">
    <header class="hit"><h4>${n ? `[${n}] ` : ""}${esc(hit.doc_title)}</h4>
      <span><span class="chip grey">${esc(hit.track)}</span> ${link}</span></header>
    <div class="crumbs">${esc(hit.path || "")}${hit.path && hit.heading ? " › " : ""}${esc(hit.heading || "")}
      ${hit.score ? ` · score ${Number(hit.score).toFixed(3)}` : ""}</div>
    <pre class="snippet">${esc(hit.snippet)}</pre>
  </div>`;
}

async function runSearch() {
  const q = $("search-q").value.trim();
  if (!q) return;
  const data = await api("/api/search", {
    method: "POST",
    body: { q, tracks: checkedTracks("search-tracks"), top_k: Number($("search-k").value) || 6 },
  });
  $("search-meta").textContent =
    `${data.search_mode === "hybrid" ? "混合检索" : "全文检索"} · ${data.hits.length} 条 · ${data.elapsed_ms}ms` +
    (data.tracks?.length ? ` · 限定 ${data.tracks.join("/")}` : "");
  $("search-hits").innerHTML = data.hits.length
    ? data.hits.map((h, i) => hitCard(h, i + 1)).join("")
    : `<div class="hint">没有命中。去掉赛道限制，或换成文档里的用词（如「提交材料」「验收」「算分」）。</div>`;
}

/* ------------------------------------------------------------------ 问答 */

async function runAsk() {
  const question = $("ask-q").value.trim();
  if (!question) return;
  const useTeam = $("ask-team").checked && state.team;
  const body = { question, top_k: 6 };
  if (useTeam) body.team = state.team;
  if ($("ask-tracks").checked && state.profile?.tracks?.length) body.tracks = state.profile.tracks;

  $("ask-meta").textContent = "检索并生成中…";
  $("ask-out").innerHTML = "";
  const data = await api("/api/ask", { method: "POST", body });
  const basis = { request: "按你勾选的赛道", question: "按问题里点名的赛道", team: `按队伍 ${useTeam ? esc(state.team) : "?"} 的赛道`, all: "全库" }[data.track_basis];
  $("ask-meta").innerHTML =
    `<span class="badge ${data.mode}">${data.mode === "llm" ? "生成式 " + esc(data.model || "") : "抽取式（未配置 LLM）"}</span> ` +
    `<span class="badge ${data.search_mode === "hybrid" ? "" : "offline"}">${data.search_mode === "hybrid" ? "混合检索" : "离线全文"}</span> ` +
    `· ${basis}${data.used_tracks.length ? " · 命中赛道 " + esc(data.used_tracks.join("/")) : ""} · ${data.elapsed_ms}ms`;
  $("ask-out").innerHTML = `
    <div class="card">
      <div class="body answer">${esc(data.answer)}</div>
      ${data.warning ? `<div class="meta" style="color:var(--warn)">提示：${esc(data.warning)}</div>` : ""}
    </div>
    <details ${data.sources.length ? "open" : ""}><summary class="meta">来源 ${data.sources.length} 条（点开核对原文）</summary>
      ${data.sources.map((s) => hitCard(s, s.n)).join("") || `<div class="meta">无</div>`}
    </details>`;
}

/* ------------------------------------------------------------------ 选题 */

const candidatesFor = (track) => (state.topicTitles[track] || []).filter((t) => t.startsWith("赛题"));

function selectedTopics(track) {
  return new Set([...(state.profile?.topics?.[track] || []), ...(state.extra[track] || [])]);
}

function renderPicker() {
  $("custom-track").innerHTML = state.tracks.map((t) => `<option>${esc(t.name)}</option>`).join("");
  if (!state.team) {
    $("prog-hint").innerHTML = `<span class="badge">先建档并选择队伍</span>`;
    $("picker").innerHTML = "";
    return;
  }
  $("prog-hint").innerHTML = `当前队伍 <b>${esc(state.team)}</b> · 上次更新 ${esc(state.profile?.updated_at?.slice(0, 16) || "—")}`;
  const rows = state.tracks.filter((t) => t.name).map((t) => {
    const track = t.name;
    const chosen = selectedTopics(track);
    const list = [...new Set([...candidatesFor(track), ...chosen])];
    if (!list.length) return "";
    const saved = (state.profile?.topics?.[track] || []).length;
    const body = list.length
      ? list.map((topic) => `<label class="topic">
          <input type="checkbox" data-track="${esc(track)}" data-topic="${esc(topic)}" ${chosen.has(topic) ? "checked" : ""}>
          <span>${esc(topic)}</span></label>`).join("")
      : `<div class="meta">该赛道没有独立的「赛题…」文档，用下面的输入框按任务名自建赛题。</div>`;
    return `<details ${saved || !list.length ? "open" : ""} class="track" data-track="${esc(track)}">
      <summary>${esc(track)} <span class="chip grey">${t.chunks} 块</span>
        <span class="meta">已选 ${saved} 项</span></summary>
      ${body}
    </details>`;
  }).join("");
  $("picker").innerHTML = rows || `<div class="hint">知识库里没有「赛题…」文档，请检查是否已导入。</div>`;
}

async function saveSelection() {
  if (!state.team) { toast("先选择队伍", true); return; }
  const wanted = {};
  document.querySelectorAll("#picker input:checked").forEach((cb) => {
    (wanted[cb.dataset.track] ||= new Set()).add(cb.dataset.topic);
  });
  const saved = state.profile?.topics || {};
  const key = (set) => [...set].sort().join("|");
  for (const track of new Set([...Object.keys(wanted), ...Object.keys(saved)])) {
    const topics = [...(wanted[track] || [])];
    if (key(topics) === key(saved[track] || [])) continue;
    await api(topicApi("/tracks"), { method: "PUT", body: { track, topics } });
  }
  await openTeam(state.team);
  toast("选题已保存");
}

/* ------------------------------------------------------------------ 进度 */

function statusOf(track, topic) {
  const records = (state.profile?.progress || []).filter(
    (r) => (r.track || "") === track && (r.topic || "") === topic);
  if (!records.length) return "todo";
  return records.map((r) => STATUS_RANK[r.status] >= 0 ? r.status : "todo")
    .sort((a, b) => STATUS_RANK[b] - STATUS_RANK[a])[0];
}

function noteOf(track, topic) {
  const rows = [...(state.profile?.progress || []), ...(state.profile?.notes || [])]
    .filter((r) => (r.track || "") === track && (r.topic || "") === topic)
    .map((r) => String(r.note || r.text || "")).filter(Boolean);
  return rows.at(-1) || "";
}

function openBlockers(track, topic) {
  return (state.profile?.blockers || []).filter(
    (r) => !r.resolved && (r.track || "") === track && (r.topic || "") === topic);
}

function renderProgress() {
  if (!state.team) { $("progress").innerHTML = ""; return; }
  const groups = (state.profile?.tracks || []).map((track) => {
    const topics = state.profile.topics[track] || [];
    if (!topics.length) return "";
    return `<div class="card"><h4>${esc(track)} <span class="meta">${topics.length} 个赛题</span></h4>
      <table class="status"><thead><tr><th>赛题</th><th>状态</th><th>备注</th><th>卡点</th><th></th></tr></thead>
      <tbody>${topics.map((topic) => {
        const st = statusOf(track, topic);
        const blockers = openBlockers(track, topic);
        return `<tr data-track="${esc(track)}" data-topic="${esc(topic)}">
          <td>${esc(topic)}</td>
          <td class="status-cell"><select class="st">${STATUS_ORDER.map((s) =>
            `<option value="${s}" ${s === st ? "selected" : ""}>${STATUS_LABEL[s]}</option>`).join("")}</select></td>
          <td class="note"><input class="nt" maxlength="500" placeholder="写到哪了" value="${esc(noteOf(track, topic))}"></td>
          <td>${blockers.map((b) => `<div>${esc(b.text)}
              <button class="ghost mini" data-act="resolve" data-id="${esc(b.id)}">解决</button></div>`).join("")}
            ${blockers.length ? "" : `<span class="meta">无</span>`}
            <input class="bk" maxlength="500" placeholder="记一个卡点"></td>
          <td><button class="mini" data-act="save">保存</button></td>
        </tr>`;
      }).join("")}</tbody></table></div>`;
  }).join("");
  $("progress").innerHTML = groups || `<div class="hint">上面勾选赛题并「保存选题」后，这里会出现进度表。</div>`;
}

async function saveRow(row) {
  const track = row.dataset.track, topic = row.dataset.topic;
  await api(topicApi("/progress"), {
    method: "PUT",
    body: { track, topic, status: row.querySelector(".st").value, note: row.querySelector(".nt").value.trim() },
  });
  const blocker = row.querySelector(".bk").value.trim();
  if (blocker) {
    await api(topicApi("/blockers"), { method: "POST", body: { track, topic, text: blocker } });
  }
  await openTeam(state.team);
  toast("已保存");
}

/* ------------------------------------------------------------------ 下一步 */

function fmtDate(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

async function loadNext() {
  if (!state.team) { $("next-body").innerHTML = `<div class="hint">先选择队伍。</div>`; $("next-meta").textContent = ""; return; }
  const data = await api(topicApi("/next-steps", "?top_k=3"));
  $("next-meta").innerHTML = data.overdue
    ? `<span class="count overdue">已过提交截止（${fmtDate(data.deadline)}）${-data.days_left} 天</span>`
    : `截止 ${fmtDate(data.deadline)} · 剩余 <b>${data.days_left}</b> 天`;
  const hints = [];
  if (data.no_track_selected) hints.push("还没选题：到「赛题进度」勾选赛题并保存。");
  if (data.tracks_without_topics?.length) hints.push(`这些赛道还没勾选题目：${data.tracks_without_topics.join("、")}`);
  const cards = data.items.map((item) => `
    <div class="card">
      <header class="hit"><h4>${esc(item.topic)}</h4>
        <span><span class="chip grey">${esc(item.track)}</span> <span class="badge ${item.status}">${STATUS_LABEL[item.status] || esc(item.status)}</span></span></header>
      ${item.note ? `<div class="meta">备注：${esc(item.note)}</div>` : ""}
      ${item.blockers.length ? `<div class="meta" style="color:var(--bad)">卡点：${item.blockers.map((b) => esc(b.text)).join("；")}</div>` : ""}
      ${item.todos.length ? `<ul class="tight">${item.todos.map((t) => {
        const url = safeUrl(t.source_url);
        return `<li>${esc(t.text)} <span class="crumbs">—— ${esc(t.doc_title)}${t.heading ? " › " + esc(t.heading) : ""}</span>
          ${url ? `<a href="${esc(url)}" target="_blank" rel="noreferrer">原文</a>` : ""}</li>`; }).join("")}</ul>`
        : `<div class="meta">文档里没抽到明确的待办，去「知识库检索」按赛题名查一下。</div>`}
    </div>`).join("");
  $("next-body").innerHTML =
    (hints.length ? `<div class="hint">${hints.join("<br>")}</div>` : "") +
    (cards || (data.no_track_selected ? "" : `<div class="hint">所有已选题都标记为「已完成」了。</div>`));
}

/* ------------------------------------------------------------------ 装配 */

function wire() {
  $("tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-tab]");
    if (!btn) return;
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b === btn));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("on", p.id === `tab-${btn.dataset.tab}`));
    if (btn.dataset.tab === "next") loadNext().catch((err) => toast(err.message, true));
  });

  $("team-select").addEventListener("change", (e) => guard(() => openTeam(e.target.value)));
  $("team-new").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("team-name").value.trim();
    if (!name) return;
    await guard(async () => {
      await api("/api/teams", { method: "POST", body: { name } });
      $("team-name").value = "";
      await loadTeams();
      await openTeam(name);
      toast(`已建档：${name}`);
    });
  });

  $("search-form").addEventListener("submit", (e) => { e.preventDefault(); guard(runSearch); });
  $("search-empty").addEventListener("click", () => {
    $("search-q").value = ""; $("search-hits").innerHTML = ""; $("search-meta").textContent = "";
    document.querySelectorAll("#search-tracks input").forEach((i) => { i.checked = false; });
  });
  $("ask-form").addEventListener("submit", (e) => { e.preventDefault(); guard(runAsk); });
  $("save-selection").addEventListener("click", () => guard(saveSelection));
  $("custom-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const track = $("custom-track").value, topic = $("custom-topic").value.trim();
    if (!track || !topic) { toast("先选赛道并填写赛题名", true); return; }
    (state.extra[track] ||= []).includes(topic) || state.extra[track].push(topic);
    $("custom-topic").value = "";
    renderPicker();
    document.querySelector(`details.track[data-track="${CSS.escape(track)}"]`)?.setAttribute("open", "");
    const box = [...document.querySelectorAll("#picker input")].find((i) => i.dataset.topic === topic);
    if (box) box.checked = true;
  });
  $("next-refresh").addEventListener("click", () => guard(() => loadNext()));
  $("progress").addEventListener("click", async (e) => {
    const row = e.target.closest("tr[data-topic]");
    if (!row) return;
    if (e.target.dataset.act === "save") {
      await guard(() => saveRow(row));
    } else if (e.target.dataset.act === "resolve") {
      await guard(async () => {
        await api(topicApi(`/blockers/${encodeURIComponent(e.target.dataset.id)}/resolve`, ""), { method: "POST" });
        await openTeam(state.team);
        toast("卡点已标记解决");
      });
    }
  });
}

async function boot() {
  wire();
  try {
    state.health = await api("/api/health");
    renderHealth();
    const data = await api("/api/tracks");
    state.tracks = data.tracks;
    state.topicTitles = Object.fromEntries(data.tracks.map((t) => [t.name, t.topics || []]));
    renderSearchTracks();
    await loadTeams();
    if (state.team) await openTeam(state.team);
    else renderPicker();
  } catch (err) {
    $("health").textContent = "后端不可用";
    toast(`初始化失败：${err.message}`, true);
  }
}

boot();
