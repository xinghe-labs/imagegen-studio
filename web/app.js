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
  if ($("preset").value) payload.preset = $("preset").value;
  if ($("size").value.trim()) payload.size = $("size").value.trim();
  if ($("quality").value.trim()) payload.quality = $("quality").value.trim();
  if ($("format").value.trim()) payload.format = $("format").value.trim();
  const preset = presetChipValue();
  if (preset) payload.preset = preset;
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
    await loadHistory();
  } catch (e) {
    alert(`生成失败：${e.message}`);
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
        alert(e.message);
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
    alert(err.message);
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
      ${r.rating ? `<span class="star-badge">★${r.rating}</span>` : ""}
      <figcaption>${esc((r.prompt || "").slice(0, 70))}</figcaption>
    </figure>`;
  }).join("");
  for (const card of grid.querySelectorAll(".card")) {
    card.addEventListener("click", () => openDetail(RECORDS[Number(card.dataset.index)]));
  }
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
        alert(e.message);
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
    alert(err.message);
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
    if (e.key === "Escape") closeDetail();
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
    if (!CURRENT) return;
    setMode("t2i");
    $("prompt").value = CURRENT.prompt || "";
    if (CURRENT.model) $("model").value = CURRENT.model;
    const params = CURRENT.parameters || {};
    setPresetChip(params.preset || "");
    $("size").value = params.size || "";
    $("quality").value = params.quality || "";
    closeDetail();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  $("use-as-reference").addEventListener("click", () => {
    if (!CURRENT) return;
    if (!REF_PATHS.includes(CURRENT.image)) REF_PATHS.push(CURRENT.image);
    setMode("i2i");
    renderRefs();
    closeDetail();
    window.scrollTo({ top: 0, behavior: "smooth" });
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
      alert(e.message);
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
      alert(e.message);
    }
  });
}

init().catch((e) => alert(`初始化失败：${e.message}`));
