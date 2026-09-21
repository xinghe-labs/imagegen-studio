/* imagegen studio frontend — vanilla JS, no build chain. */
"use strict";

const $ = (id) => document.getElementById(id);
let META = null;
let RECORDS = [];
let CURRENT = null; // record shown in the detail drawer
let MODE = "t2i";   // t2i | i2i
let REF_FILES = []; // File objects awaiting upload
let REF_PATHS = []; // library paths used as references
let CHARACTERS = []; // character registry entries
let PROMPTS = [];   // prompt library entries
let PROMPT_CATS = []; // prompt categories
let PL_CATEGORY = '';

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* keep */ }
    throw new Error(detail);
  }
  return res.json();
}

function toast(message, type = "info") {
  let box = document.getElementById("toast-box");
  if (!box) {
    box = document.createElement("div");
    box.id = "toast-box";
    document.body.append(box);
  }
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.textContent = message;
  box.append(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 320);
  }, 3400);
}

/* ---------- meta / profiles ---------- */

async function loadMeta() {
  META = await api("/api/meta");
  $("library-path").textContent = META.library;
  const profileSelect = $("profile-select");
  profileSelect.innerHTML = "";
  if (!META.profiles.length) {
    const opt = document.createElement("option");
    opt.textContent = "未配置 profiles（用环境变量）";
    profileSelect.append(opt);
    profileSelect.disabled = true;
  } else {
    profileSelect.disabled = false;
    for (const p of META.profiles) {
      const opt = document.createElement("option");
      opt.value = p.name;
      opt.textContent = `${p.name} · ${p.base_url}${p.has_key ? " ✓key" : ""}`;
      if (p.name === META.active) opt.selected = true;
      profileSelect.append(opt);
    }
  }
  const modelSelect = $("model");
  modelSelect.innerHTML = '<option value="">默认 gpt-image-2</option>';
  for (const m of META.models) {
    const opt = document.createElement("option");
    opt.value = m.model;
    opt.textContent = `${m.number}. ${m.model}（${m.vendor}）`;
    modelSelect.append(opt);
  }
}

/* ---------- generation ---------- */

function setMode(mode) {
  MODE = mode;
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("on", tab.dataset.mode === mode);
  }
  $("edit-panel").classList.toggle("hidden", mode !== "i2i");
}

function renderRefs() {
  const box = $("ref-thumbs");
  box.innerHTML = "";
  const chips = [];
  REF_PATHS.forEach((p, i) => {
    chips.push({ kind: "path", index: i, url: `/api/image?path=${encodeURIComponent(p)}` });
  });
  REF_FILES.forEach((f, i) => {
    if (!f._url) f._url = URL.createObjectURL(f);
    chips.push({ kind: "file", index: i, url: f._url });
  });
  box.innerHTML = chips
    .map((c) => `<span class="ref-chip"><img src="${c.url}" alt=""><button type="button" class="ref-remove" data-kind="${c.kind}" data-index="${c.index}">✕</button></span>`)
    .join("");
  for (const btn of box.querySelectorAll(".ref-remove")) {
    btn.addEventListener("click", () => {
      if (btn.dataset.kind === "path") REF_PATHS.splice(Number(btn.dataset.index), 1);
      else REF_FILES.splice(Number(btn.dataset.index), 1);
      renderRefs();
    });
  }
}

function profileField(form) {
  const profile = $("profile-select").value;
  if (profile && !$("profile-select").disabled) form.append("profile", profile);
}

async function submitT2I() {
  let payload;
  try {
    payload = collectForm();
  } catch (e) {
    throw e;
  }
  const { job_id } = await api("/api/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return job_id;
}

async function submitEdit() {
  const prompt = $("prompt").value.trim();
  if (!prompt) throw new Error("先写修改指令");
  if (!REF_FILES.length && !REF_PATHS.length) {
    const entry = CHARACTERS.find((c) => c.name === $("character").value);
    if (entry && entry.has_reference) REF_PATHS.push(entry.reference_image);
  }
  if (!REF_FILES.length && !REF_PATHS.length) throw new Error("图生图至少需要一张参考图");
  const form = new FormData();
  form.append("prompt", prompt);
  profileField(form);
  form.append("model", $("model").value || "gpt-image-2");
  const preset = presetChipValue();
  if (preset) form.append("preset", preset);
  form.append("n", $("n").value);
  for (const p of REF_PATHS) form.append("image_paths", p);
  for (const f of REF_FILES) form.append("images", f, f.name);
  const { job_id } = await api("/api/edit", { method: "POST", body: form });
  return job_id;
}

function presetChipValue() {
  const on = document.querySelector("#preset .on");
  return on ? on.dataset.v : "";
}

function setPresetChip(value) {
  for (const chip of document.querySelectorAll("#preset button")) {
    chip.classList.toggle("on", chip.dataset.v === value);
  }
}

function closeDetail() {
  $("detail").classList.add("hidden");
  $("detail-backdrop").classList.add("hidden");
}

function collectForm() {
  const payload = { prompt: $("prompt").value.trim(), n: Number($("n").value) };
  if (!payload.prompt) throw new Error("先写一句提示词");
  if ($("model").value) payload.model = $("model").value;
  const preset = presetChipValue();
  if (preset) payload.preset = preset;
  const ratio = document.querySelector("#ratio .on");
  if (ratio && ratio.dataset.ratio) {
    if (($("model").value || "").startsWith("grok")) payload.aspect_ratio = ratio.dataset.ratio;
    else payload.size = { "1:1": "1024x1024", "3:2": "1536x1024", "2:3": "1024x1536" }[ratio.dataset.ratio];
  }
  if ($("size").value.trim()) payload.size = $("size").value.trim();
  if ($("quality").value.trim()) payload.quality = $("quality").value.trim();
  if ($("format").value.trim()) payload.format = $("format").value.trim();
  if ($("character").value) payload.character = $("character").value;
  if ($("project").value.trim()) payload.project = $("project").value.trim();
  const profile = $("profile-select").value;
  if (profile && !$("profile-select").disabled) payload.profile = profile;
  return payload;
}

async function pollJob(jobId) {
  const started = Date.now();
  for (;;) {
    const job = await api(`/api/jobs/${jobId}`);
    const secs = Math.round((Date.now() - started) / 1000);
    $("job-text").textContent =
      job.status === "running" ? `生成中… ${secs}s` :
      job.status === "queued" ? "排队中…" : `完成，用时 ${job.elapsed}s`;
    if (job.status === "done") return job.result;
    if (job.status === "error") {
      const err = job.error || {};
      throw new Error(err.summary || err.category || "生成失败");
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
}

async function onGenerate() {
  $("generate-btn").disabled = true;
  $("job-status").classList.remove("hidden");
  $("job-text").textContent = "排队中…";
  try {
    const job_id = MODE === "i2i" ? await submitEdit() : await submitT2I();
    const result = await pollJob(job_id);
    showLatest(result);
    toast(`生成完成 · ${result.model || ""}`, "success");
    await loadHistory();
  } catch (e) {
    toast(`生成失败：${e.message}`, "error");
  } finally {
    $("generate-btn").disabled = false;
    $("job-status").classList.add("hidden");
  }
}

function showLatest(result) {
  const box = $("latest");
  const saved = result.saved || [];
  box.innerHTML = `<div class="latest-row">${saved
    .map((p) => `<img src="/api/image?path=${encodeURIComponent(p)}" alt="" loading="lazy">`)
    .join("")}</div><div class="muted">${esc(result.model)} · ${esc(result.selection_reason || "")}</div>`;
}

/* ---------- characters / projects / stats ---------- */

async function loadCharacters() {
  const data = await api("/api/characters");
  CHARACTERS = data.characters;
  const select = $("character");
  const current = select.value;
  select.innerHTML = '<option value="">不使用角色档案</option>' +
    CHARACTERS.map((c) => `<option value="${esc(c.name)}">${esc(c.name)}${c.has_reference ? " ◈" : ""}</option>`).join("");
  if (CHARACTERS.some((c) => c.name === current)) select.value = current;
}

function renderCharacters() {
  const list = $("characters-list");
  if (!CHARACTERS.length) {
    list.innerHTML = '<p class="muted">还没有角色——从图库详情「存为角色」，或用下面的表单手写身份块。</p>';
    return;
  }
  list.innerHTML = CHARACTERS
    .map(
      (c) => `
    <div class="profile-row">
      <span class="profile-name">${esc(c.name)}</span>
      <span class="muted">${c.has_reference ? "◈ 有参考图" : "纯文本"} · ${esc((c.identity_block || "").slice(0, 60))}…</span>
      <button type="button" data-act="delete" data-name="${esc(c.name)}">删除</button>
    </div>`
    )
    .join("");
  for (const btn of list.querySelectorAll("button[data-act]")) {
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/characters/${encodeURIComponent(btn.dataset.name)}`, { method: "DELETE" });
        await loadCharacters();
        renderCharacters();
      } catch (e) {
        toast(e.message, "error");
      }
    });
  }
}

async function onCharacterSubmit(e) {
  e.preventDefault();
  try {
    await api("/api/characters", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("ch-name").value.trim(),
        identity_block: $("ch-identity").value.trim(),
      }),
    });
    $("ch-name").value = "";
    $("ch-identity").value = "";
    await loadCharacters();
    renderCharacters();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function renderStats() {
  const s = await api("/api/stats");
  const mb = (s.bytes_total / 1048576).toFixed(1);
  const chips = [
    ["总图数", s.total],
    ["本月", s.this_month],
    ["收藏", s.favorites],
    ["容量", `${mb} MB`],
  ];
  const byModel = Object.entries(s.by_model).sort((a, b) => b[1] - a[1]);
  const modelText = byModel.map(([m, n]) => `${m}×${n}`).join(" · ");
  $("stats-line").innerHTML =
    chips.map(([k, v]) => `<span class="stat-chip"><span class="num">${esc(v)}</span><span class="lbl">${esc(k)}</span></span>`).join("") +
    (modelText ? `<span class="stat-models">${esc(modelText)}</span>` : "");
}

/* ---------- gallery / history ---------- */

async function loadHistory() {
  const params = new URLSearchParams();
  if ($("search").value.trim()) params.set("q", $("search").value.trim());
  if ($("filter-model").value) params.set("model", $("filter-model").value);
  if ($("filter-project").value) params.set("project", $("filter-project").value);
  if ($("fav-only").checked) params.set("favorites", "true");
  const data = await api(`/api/history?${params}`);
  RECORDS = data.records;
  renderGallery();
  refreshModelFilter();
  refreshProjectFilter();
  await renderStats();
}

function refreshProjectFilter() {
  const select = $("filter-project");
  const current = select.value;
  const projects = [...new Set(RECORDS.map((r) => r.project).filter(Boolean))].sort();
  select.innerHTML = '<option value="">全部项目</option>' +
    projects.map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join("");
  if (projects.includes(current)) select.value = current;
}

function refreshModelFilter() {
  const select = $("filter-model");
  const current = select.value;
  const models = [...new Set(RECORDS.map((r) => r.model).filter(Boolean))].sort();
  select.innerHTML = '<option value="">全部模型</option>' +
    models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
  if (models.includes(current)) select.value = current;
}

function renderGallery() {
  const grid = $("gallery");
  $("empty-hint").classList.toggle("hidden", RECORDS.length > 0);
  grid.innerHTML = RECORDS.map((r, i) => {
    const ratio = r.width && r.height ? ` style="aspect-ratio:${r.width}/${r.height}"` : "";
    return `
    <figure class="card"${ratio} data-index="${i}">
      <img src="/api/image?path=${encodeURIComponent(r.image)}" alt="" loading="lazy">
      <div class="quick-bar">
        <button type="button" data-quick="download" title="下载">⤓</button>
        <button type="button" data-quick="variant" title="以此发起变体">变</button>
        <button type="button" data-quick="reference" title="用作参考">参</button>
        <button type="button" data-quick="fav" title="收藏">${r.rating ? "★" : "☆"}</button>
      </div>
      ${r.rating ? `<span class="star-badge">★${r.rating}</span>` : ""}
      <figcaption>${esc((r.prompt || "").slice(0, 70))}</figcaption>
    </figure>`;
  }).join("");
  grid.onclick = (e) => {
    const quick = e.target.closest("[data-quick]");
    const cardEl = e.target.closest(".card");
    if (!cardEl) return;
    const record = RECORDS[Number(cardEl.dataset.index)];
    if (!record) return;
    if (quick) {
      e.stopPropagation();
      const act = quick.dataset.quick;
      if (act === "download") downloadImage(record);
      else if (act === "variant") variantFrom(record);
      else if (act === "reference") referenceFrom(record);
      else if (act === "fav") toggleFavorite(record);
      return;
    }
    openDetail(record);
  };
}

function downloadImage(record) {
  const a = document.createElement("a");
  a.href = `/api/image?path=${encodeURIComponent(record.image)}&download=1`;
  a.download = record.image.split(/[\\/]/).pop();
  document.body.append(a);
  a.click();
  a.remove();
}

function variantFrom(record) {
  setMode("t2i");
  $("prompt").value = record.prompt || "";
  if (record.model) $("model").value = record.model;
  const params = record.parameters || {};
  setPresetChip(params.preset || "");
  $("size").value = params.size || "";
  $("quality").value = params.quality || "";
  closeDetail();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function referenceFrom(record) {
  if (!REF_PATHS.includes(record.image)) REF_PATHS.push(record.image);
  setMode("i2i");
  renderRefs();
  closeDetail();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function toggleFavorite(record) {
  const next = record.rating ? 0 : 5;
  await api("/api/rate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ image: record.image, rating: next }),
  });
  record.rating = next;
  renderGallery();
}

/* ---------- detail drawer ---------- */

function starsHTML(rating) {
  return [1, 2, 3, 4, 5].map((n) =>
    `<span class="star ${n <= rating ? "on" : ""}" data-star="${n}">★</span>`).join("");
}

function openDetail(record) {
  CURRENT = record;
  $("detail-img").src = `/api/image?path=${encodeURIComponent(record.image)}`;
  $("detail-prompt").textContent = record.prompt || "";
  $("detail-stars").innerHTML = starsHTML(record.rating || 0);
  const params = record.parameters || {};
  const rows = [
    ["模型", record.model], ["编号选择", record.choice],
    ["预设", params.preset], ["尺寸", params.size], ["质量", params.quality],
    ["格式", params.output_format], ["张数", params.n],
    ["项目", record.project], ["角色", record.character],
    ["生成时间", record.created_at],
  ].filter(([, v]) => v !== null && v !== undefined && v !== "");
  $("detail-params").innerHTML = rows
    .map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("");
  $("detail-project").value = record.project || "";
  $("detail").classList.remove("hidden");
  $("detail-backdrop").classList.remove("hidden");
}

async function onRate(star) {
  if (!CURRENT) return;
  await api("/api/rate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ image: CURRENT.image, rating: star }),
  });
  CURRENT.rating = star;
  $("detail-stars").innerHTML = starsHTML(star);
  await loadHistory();
}

async function copyText(text, hint) {
  await navigator.clipboard.writeText(text);
  $("copy-hint").textContent = hint || "已复制到剪贴板";
  $("copy-hint").classList.remove("hidden");
  setTimeout(() => $("copy-hint").classList.add("hidden"), 1500);
}

/* ---------- prompt library ---------- */

async function loadPrompts() {
  const params = new URLSearchParams();
  if ($("pl-search").value.trim()) params.set("q", $("pl-search").value.trim());
  if (PL_CATEGORY) params.set("category", PL_CATEGORY);
  const data = await api(`/api/prompts?${params}`);
  PROMPTS = data.prompts;
  PROMPT_CATS = data.categories;
  renderPromptLibrary();
}

function openPromptLibrary() {
  $("prompt-library").classList.remove("hidden");
  loadPrompts();
  renderPromptSources();
}

function closePromptLibrary() {
  $("prompt-library").classList.add("hidden");
}

const PL_GRADIENTS = [
  ["#1e3a5f", "#38bdf8"], ["#312e81", "#818cf8"], ["#134e4a", "#2dd4bf"],
  ["#1e293b", "#64748b"], ["#4c1d95", "#c084fc"], ["#0c4a6e", "#22d3ee"],
  ["#3730a3", "#60a5fa"], ["#155e75", "#67e8f9"],
];
const PL_GLYPHS = ["✦", "◈", "◉", "◆", "▲", "●", "✧", "❖"];

function placeholderThumb(id) {
  let h = 0;
  for (const ch of String(id || "")) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  const [c1, c2] = PL_GRADIENTS[h % PL_GRADIENTS.length];
  const glyph = PL_GLYPHS[(h >>> 3) % PL_GLYPHS.length];
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 118">`
    + `<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">`
    + `<stop offset="0" stop-color="${c1}"/><stop offset="1" stop-color="${c2}"/></linearGradient></defs>`
    + `<rect width="300" height="118" fill="url(#g)"/>`
    + `<circle cx="${40 + (h % 120)}" cy="${20 + ((h >>> 4) % 60)}" r="52" fill="#fff" opacity="0.07"/>`
    + `<circle cx="${180 + ((h >>> 6) % 90)}" cy="${60 + ((h >>> 8) % 50)}" r="34" fill="#fff" opacity="0.05"/>`
    + `<text x="150" y="64" text-anchor="middle" font-size="30" fill="#fff" opacity="0.45">${glyph}</text>`
    + `</svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

function renderPromptLibrary() {
  $("pl-cats").innerHTML = ["", ...PROMPT_CATS]
    .map((c) => `<button type="button" class="${(PL_CATEGORY || "") === c ? "on" : ""}" data-cat="${esc(c)}">${c || "全部"}</button>`)
    .join("");
  for (const btn of document.querySelectorAll("#pl-cats button")) {
    btn.addEventListener("click", () => {
      PL_CATEGORY = btn.dataset.cat;
      loadPrompts();
    });
  }
  $("pl-list").innerHTML = PROMPTS.map((p) => {
    const thumb = p.image
      ? (String(p.image).startsWith("http")
          ? p.image
          : `/api/image?path=${encodeURIComponent(p.image)}`)
      : placeholderThumb(p.id);
    return `
    <div class="pl-card" data-id="${esc(p.id)}">
      <img class="pl-thumb" src="${esc(thumb)}" alt="" loading="lazy">
      <div class="pl-title">${esc(p.title_zh || "")}</div>
      <div class="pl-text">${esc(p.prompt)}</div>
      <div class="pl-meta">
        <span>${esc(p.category || "")} · ${esc((p.source || "").replace("builtin", "内置"))}</span>
        ${p.source !== "builtin" ? `<button type="button" class="pl-del" data-del="${esc(p.id)}">删</button>` : ""}
      </div>
    </div>`;
  }).join("");
  for (const card of document.querySelectorAll(".pl-card")) {
    card.addEventListener("click", (e) => {
      if (e.target.closest(".pl-del")) return;
      const entry = PROMPTS.find((x) => x.id === card.dataset.id);
      if (!entry) return;
      $("prompt").value = entry.prompt;
      closePromptLibrary();
      toast("已填入提示词", "success");
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }
  for (const del of document.querySelectorAll(".pl-del")) {
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await api(`/api/prompts/${encodeURIComponent(del.dataset.del)}`, { method: "DELETE" });
        await loadPrompts();
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }
}

async function renderPromptSources() {
  const data = await api("/api/prompts/sources");
  const list = $("pl-sources-list");
  list.innerHTML = data.sources.length
    ? data.sources.map((s) => `
      <div class="profile-row">
        <span class="profile-name">${esc(s.name)}</span>
        <span class="muted">${esc(s.url)} · ${esc(s.format)}</span>
        <button type="button" data-del="${esc(s.name)}">删除</button>
      </div>`).join("")
    : '<p class="muted">还没有配置源——添加 GitHub raw 地址后点「同步」。</p>';
  for (const btn of list.querySelectorAll("button[data-del]")) {
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/prompts/sources/${encodeURIComponent(btn.dataset.del)}`, { method: "DELETE" });
        await renderPromptSources();
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }
}

async function onPromptSourceSubmit(e) {
  e.preventDefault();
  const form = new FormData();
  form.append("name", $("pl-src-name").value.trim());
  form.append("url", $("pl-src-url").value.trim());
  form.append("format", $("pl-src-format").value);
  try {
    await api("/api/prompts/sources", { method: "POST", body: form });
    $("pl-src-name").value = "";
    $("pl-src-url").value = "";
    await renderPromptSources();
    toast("源已添加，点「同步」拉取", "success");
  } catch (err) {
    toast(err.message, "error");
  }
}

async function onPromptSync() {
  $("pl-sync").disabled = true;
  try {
    const result = await api("/api/prompts/sync", { method: "POST" });
    const parts = Object.entries(result.synced).map(([k, v]) => `${k} ${v}`);
    toast(parts.length ? `同步完成：${parts.join("，")}` : "没有配置源，先添加 GitHub 源", parts.length ? "success" : "error");
    await loadPrompts();
    await renderPromptSources();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    $("pl-sync").disabled = false;
  }
}

/* ---------- profiles panel ---------- */

function renderProfiles() {
  const list = $("profiles-list");
  if (!META.profiles.length) {
    list.innerHTML = '<p class="muted">还没有 profile——用下面的表单添加，或继续用环境变量里的配置。</p>';
    return;
  }
  list.innerHTML = META.profiles
    .map(
      (p) => `
    <div class="profile-row">
      <span class="profile-name">${p.name === META.active ? "● " : "○ "}${esc(p.name)}</span>
      <span class="muted">${esc(p.base_url)}${p.has_key ? " · ✓key" : " · 无key"}</span>
      <button type="button" data-act="activate" data-name="${esc(p.name)}" ${p.name === META.active ? "disabled" : ""}>设为默认</button>
      <button type="button" data-act="delete" data-name="${esc(p.name)}">删除</button>
    </div>`
    )
    .join("");
  for (const btn of list.querySelectorAll("button[data-act]")) {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.name;
      try {
        if (btn.dataset.act === "activate") {
          await api("/api/profiles/activate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name }),
          });
        } else {
          await api(`/api/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
        }
        await loadMeta();
        renderProfiles();
      } catch (e) {
        toast(e.message, "error");
      }
    });
  }
}

async function onProfileSubmit(e) {
  e.preventDefault();
  const form = new FormData();
  form.append("name", $("pf-name").value.trim());
  form.append("base_url", $("pf-url").value.trim());
  form.append("api_key", $("pf-key").value);
  try {
    await api("/api/profiles", { method: "POST", body: form });
    $("pf-name").value = "";
    $("pf-url").value = "";
    $("pf-key").value = "";
    await loadMeta();
    renderProfiles();
  } catch (err) {
    toast(err.message, "error");
  }
}

/* ---------- wiring ---------- */

async function init() {
  await loadMeta();
  renderProfiles();
  await loadCharacters();
  renderCharacters();
  await loadHistory();

  $("generate-btn").addEventListener("click", onGenerate);
  $("prompt").addEventListener("keydown", (e) => {
    if (e.ctrlKey && e.key === "Enter") onGenerate();
  });
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => setMode(tab.dataset.mode));
  }
  $("ref-files").addEventListener("change", (e) => {
    for (const f of e.target.files) REF_FILES.push(f);
    e.target.value = "";
    renderRefs();
  });
  $("profile-form").addEventListener("submit", onProfileSubmit);
  $("character-form").addEventListener("submit", onCharacterSubmit);
  $("refresh").addEventListener("click", loadHistory);
  $("search").addEventListener("change", loadHistory);
  $("filter-model").addEventListener("change", loadHistory);
  $("filter-project").addEventListener("change", loadHistory);
  $("fav-only").addEventListener("change", loadHistory);
  $("close-detail").addEventListener("click", closeDetail);
  $("detail-backdrop").addEventListener("click", closeDetail);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("prompt-library").classList.contains("hidden")) closePromptLibrary();
    else closeDetail();
  });
  $("detail-stars").addEventListener("click", (e) => {
    const star = e.target.dataset.star;
    if (star) onRate(Number(star));
  });
  $("copy-cmd").addEventListener("click", async () => {
    if (!CURRENT) return;
    const { command } = await api("/api/reproduce", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image: CURRENT.image }),
    });
    await copyText(command, "复现命令已复制");
  });
  $("open-folder").addEventListener("click", async () => {
    if (!CURRENT) return;
    await api("/api/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image: CURRENT.image }),
    });
  });
  $("use-as-variant").addEventListener("click", () => {
    if (CURRENT) variantFrom(CURRENT);
  });
  $("use-as-reference").addEventListener("click", () => {
    if (CURRENT) referenceFrom(CURRENT);
  });
  $("open-prompt-library").addEventListener("click", openPromptLibrary);
  $("pl-close").addEventListener("click", closePromptLibrary);
  $("pl-sync").addEventListener("click", onPromptSync);
  $("pl-search").addEventListener("change", loadPrompts);
  $("pl-source-form").addEventListener("submit", onPromptSourceSubmit);
  $("save-prompt").addEventListener("click", async () => {
    if (!CURRENT || !CURRENT.prompt) return;
    const title = window.prompt("收藏标题（中文，可留空自动截取）：") || "";
    try {
      await api("/api/prompts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title_zh: title, prompt: CURRENT.prompt, category: "我的收藏" }),
      });
      toast("已存入提示词库", "success");
    } catch (err) {
      toast(err.message, "error");
    }
  });
  $("download-img").addEventListener("click", () => {
    if (CURRENT) downloadImage(CURRENT);
  });
  $("ratio").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-ratio]");
    if (!btn) return;
    for (const b of document.querySelectorAll("#ratio button")) b.classList.toggle("on", b === btn);
  });
  $("save-as-character").addEventListener("click", async () => {
    if (!CURRENT) return;
    const name = window.prompt("角色名（用于拼进后续提示词）：");
    if (!name || !name.trim()) return;
    try {
      await api("/api/characters", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), from_image: CURRENT.image }),
      });
      await loadCharacters();
      renderCharacters();
      await copyText("已存为角色档案", "已存为角色档案");
    } catch (e) {
      toast(e.message, "error");
    }
  });
  $("save-project").addEventListener("click", async () => {
    if (!CURRENT) return;
    try {
      await api("/api/project", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image: CURRENT.image, project: $("detail-project").value.trim() }),
      });
      CURRENT.project = $("detail-project").value.trim();
      await loadHistory();
      openDetail(CURRENT);
    } catch (e) {
      toast(e.message, "error");
    }
  });
}

init().catch((e) => toast(`初始化失败：${e.message}`), "error");
