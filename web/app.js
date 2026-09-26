/* imagegen studio frontend — vanilla JS, no build chain. */
"use strict";

const $ = (id) => document.getElementById(id);
let META = null;
let RECORDS = [];
let CURRENT = null; // record shown in the detail drawer
let MODE = "t2i";   // t2i | i2i
let REF_FILES = []; // File objects awaiting upload
let REF_PATHS = []; // library paths used as references
let SELECT_MODE = false;   // 多选模式
let SELECTED = new Set();  // 多选中的图片路径 (multi-select)
let LAST_PICK_INDEX = -1;  // 上次点选的卡片下标（Shift 区间选择用）

// 访问令牌：从 ?token= 取并记住，之后所有请求自动带上
const AUTH_TOKEN = (() => {
  const fromUrl = new URLSearchParams(location.search).get("token") || "";
  if (fromUrl) localStorage.setItem("imagegen-token", fromUrl);
  return fromUrl || localStorage.getItem("imagegen-token") || "";
})();
if (new URLSearchParams(location.search).get("token")) {
  history.replaceState(null, "", location.pathname + location.hash);
}

function withToken(url) {
  if (!AUTH_TOKEN) return url;
  return `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(AUTH_TOKEN)}`;
}

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

async function api(path, options) {
  const opts = { ...(options || {}) };
  if (AUTH_TOKEN) opts.headers = { ...(opts.headers || {}), "X-Auth-Token": AUTH_TOKEN };
  const res = await fetch(withToken(path), opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* keep */ }
    if (Array.isArray(detail)) detail = "请求参数不合法";
    if (res.status === 401) showAuthHint(detail);
    throw new Error(detail);
  }
  return res.json();
}

// 令牌缺失/失效时：页面本身仍能打开（静态资源免认证），这里给出修复入口
function showAuthHint(detail) {
  const hint = $("auth-hint");
  if (!hint) return;
  hint.classList.remove("hidden");
  hint.textContent = `需要访问令牌（${detail || "401"}）——请在下方「网关配置」里填入令牌后保存。`;
}

function toast(message, type = "info", action = null) {
  let box = document.getElementById("toast-box");
  if (!box) {
    box = document.createElement("div");
    box.id = "toast-box";
    document.body.append(box);
  }
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  const text = document.createElement("span");
  text.textContent = message;
  el.append(text);
  let timer = null;
  if (action && action.label) {
    // 带操作按钮的 toast（如删除后的「撤销」）停留更久
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "toast-action";
    btn.textContent = action.label;
    btn.addEventListener("click", () => {
      if (timer) clearTimeout(timer);
      el.remove();
      action.onClick();
    });
    el.append(btn);
  }
  box.append(el);
  requestAnimationFrame(() => el.classList.add("show"));
  timer = setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 320);
  }, action && action.label ? 12000 : 3400);
}

/* ---------- meta / profiles ---------- */

async function loadMeta() {
  META = await api("/api/meta");
  if (META.version) {
    if (!PAGE_VERSION) PAGE_VERSION = META.version; // 页面资源来自首次加载的那一版
    SERVER_VERSION = META.version;
    if ($("app-version")) {
      $("app-version").textContent = `v${PAGE_VERSION}`;
      $("app-version").title = `页面 v${PAGE_VERSION} · 服务端 v${SERVER_VERSION} — 点击查看更新`;
    }
    document.title = `imagegen studio v${PAGE_VERSION}`;
  }
  if ($("cred-hint")) $("cred-hint").classList.toggle("hidden", Boolean(META.credentials));
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
      opt.textContent = `${p.name}${p.has_key ? " ✓key" : ""}`;
      opt.title = p.base_url;
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
    chips.push({ kind: "path", index: i, url: withToken(`/api/image?path=${encodeURIComponent(p)}`) });
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
  if (!REF_FILES.length && !REF_PATHS.length) throw new Error("图生图至少需要一张参考图");
  const form = new FormData();
  form.append("prompt", prompt);
  profileField(form);
  form.append("model", $("model").value || "gpt-image-2");
  const preset = presetChipValue();
  if (preset) form.append("preset", preset);
  else form.append("quality", "high");
  if ($("size").value.trim()) form.append("size", $("size").value.trim());
  if ($("format").value.trim()) form.append("output_format", $("format").value.trim());
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

/* ---------- 草稿与参数记忆（刷新不丢） ---------- */

const DRAFT_KEY = "imagegen-draft";

// 多用户下同一浏览器可能切换令牌：草稿按令牌分开存，避免串台
function draftKey() {
  return AUTH_TOKEN ? `${DRAFT_KEY}:${AUTH_TOKEN.slice(0, 10)}` : DRAFT_KEY;
}

function saveDraft() {
  try {
    const ratioBtn = document.querySelector("#ratio .on");
    localStorage.setItem(
      draftKey(),
      JSON.stringify({
        prompt: $("prompt").value,
        model: $("model").value,
        n: $("n").value,
        preset: presetChipValue(),
        ratio: ratioBtn ? ratioBtn.dataset.ratio : "",
        size: $("size").value,
        quality: $("quality").value,
        format: $("format").value,
        project: $("project").value,
        mode: MODE,
        refPaths: REF_PATHS,
      }),
    );
  } catch (e) {
    /* 隐私模式下 localStorage 可能不可用，忽略 */
  }
}

function loadDraft() {
  let draft = null;
  try {
    draft = JSON.parse(localStorage.getItem(draftKey()) || "null");
  } catch (e) {
    return;
  }
  if (!draft) return;
  if (draft.prompt) $("prompt").value = draft.prompt;
  if (draft.model) $("model").value = draft.model;
  if (draft.n) $("n").value = draft.n;
  setPresetChip(draft.preset || "");
  if (draft.ratio) {
    for (const btn of document.querySelectorAll("#ratio button")) {
      btn.classList.toggle("on", btn === document.querySelector(`#ratio button[data-ratio="${draft.ratio}"]`));
    }
  }
  $("size").value = draft.size || "";
  $("quality").value = draft.quality || "";
  $("format").value = draft.format || "";
  $("project").value = draft.project || "";
  REF_PATHS = Array.isArray(draft.refPaths) ? draft.refPaths.filter((p) => typeof p === "string") : [];
  renderRefs();
  if (draft.mode === "i2i") setMode("i2i");
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
  // 自动档不传预设：质量留空时默认 high（对应被精简掉的 quality 预设）
  if (!preset && !payload.quality) payload.quality = "high";
  if ($("project").value.trim()) payload.project = $("project").value.trim();
  const profile = $("profile-select").value;
  if (profile && !$("profile-select").disabled) payload.profile = profile;
  return payload;
}

let CURRENT_JOB = null; // 正在轮询的任务 id，用于「取消」

async function pollJob(jobId) {
  const started = Date.now();
  CURRENT_JOB = jobId;
  let delay = 1000;
  try {
    for (;;) {
      const job = await api(`/api/jobs/${jobId}`);
      const secs = Math.round((Date.now() - started) / 1000);
      const retried = Number(job.attempts || 1) > 1 ? `（已自动重试 ${job.attempts - 1} 次）` : "";
      $("job-text").textContent =
        job.status === "running" ? `生成中… ${secs}s${retried}` :
        job.status === "queued" ? "排队中…" : `完成，用时 ${job.elapsed}s`;
      if (job.status === "done") return job.result;
      if (job.status === "cancelled") {
        const e = new Error("已取消");
        e.cancelled = true;
        throw e;
      }
      if (job.status === "error") {
        const err = job.error || {};
        const e = new Error(err.brief || err.summary || err.category || "生成失败");
        e.detail = err.summary || "";
        throw e;
      }
      // 长任务退避：1s → 2s → 3s 封顶，减少无谓轮询
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay + 1000, 3000);
    }
  } finally {
    CURRENT_JOB = null;
  }
}

async function cancelJob() {
  if (!CURRENT_JOB) return;
  try {
    await api(`/api/jobs/${CURRENT_JOB}/cancel`, { method: "POST" });
    toast("已取消", "info");
  } catch (e) {
    toast(e.message, "error");
  }
}

async function onGenerate() {
  $("generate-btn").disabled = true;
  $("job-status").classList.remove("hidden");
  $("cancel-job").classList.remove("hidden");
  $("job-detail").classList.add("hidden");
  $("job-text").textContent = "排队中…";
  try {
    const job_id = MODE === "i2i" ? await submitEdit() : await submitT2I();
    const result = await pollJob(job_id);
    showLatest(result);
    toast(`生成完成 · ${result.model || ""}`, "success");
    await loadHistory();
    highlightFresh(result.saved || []);
  } catch (e) {
    if (e.cancelled) {
      toast("已取消", "info");
    } else {
      toast(`生成失败：${e.message}`, "error");
      if (e.detail) {
        $("job-detail").textContent = e.detail;
        $("job-detail").classList.remove("hidden");
      }
    }
  } finally {
    $("generate-btn").disabled = false;
    $("cancel-job").classList.add("hidden");
    if (!$("job-detail").classList.contains("hidden")) {
      // 失败详情保留几秒再收起，便于阅读
      setTimeout(() => $("job-status").classList.add("hidden"), 8000);
    } else {
      $("job-status").classList.add("hidden");
    }
  }
}

// 生成完成后把新图在图库里标出来并滚到可见处
function highlightFresh(savedPaths) {
  const wanted = new Set(savedPaths.map((p) => String(p).toLowerCase()));
  if (!wanted.size) return;
  const cards = [...document.querySelectorAll(".card")];
  const fresh = cards.filter((card) => {
    const record = RECORDS[Number(card.dataset.index)];
    return record && wanted.has(String(record.image).toLowerCase());
  });
  for (const card of fresh) {
    card.classList.add("fresh");
    setTimeout(() => card.classList.remove("fresh"), 4000);
  }
  if (fresh[0]) fresh[0].scrollIntoView({ block: "center", behavior: "smooth" });
}

function showLatest(result) {
  const box = $("latest");
  const saved = result.saved || [];
  box.innerHTML = `<div class="latest-row">${saved
    .map((p) => `<img src="${withToken(`/api/image?path=${encodeURIComponent(p)}`)}" alt="" loading="lazy">`)
    .join("")}</div><div class="muted">${esc(result.model)} · ${esc(result.selection_reason || "")}</div>`;
}

/* ---------- projects / stats ---------- */

async function renderStats() {
  const s = await api("/api/stats");
  const mb = (s.bytes_total / 1048576).toFixed(1);
  const chips = [
    ["总图数", s.total],
    ["本月", s.this_month],
    ["收藏", s.favorites],
    ["容量", `${mb} MB`],
  ];
  $("stats-line").innerHTML = chips
    .map(([k, v]) => `<span class="stat-chip"><span class="num">${esc(v)}</span><span class="lbl">${esc(k)}</span></span>`)
    .join("");
}

/* ---------- gallery / history ---------- */

async function loadHistory() {
  const params = new URLSearchParams();
  if ($("search").value.trim()) params.set("q", $("search").value.trim());
  if ($("filter-model").value) params.set("model", $("filter-model").value);
  if ($("filter-project").value) params.set("project", $("filter-project").value);
  const days = Number($("filter-days").value || 0);
  if (days > 0) {
    const since = new Date(Date.now() - days * 86400000);
    const pad = (n) => String(n).padStart(2, "0");
    params.set("since", `${since.getFullYear()}-${pad(since.getMonth() + 1)}-${pad(since.getDate())}`);
  }
  if ($("fav-only").checked) params.set("favorites", "true");
  const data = await api(`/api/history?${params}`);
  RECORDS = data.records;
  renderGallery();
  refreshModelFilter();
  refreshProjectFilter();
  refreshProjectDatalist();
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
  grid.classList.toggle("selecting", SELECT_MODE);
  grid.innerHTML = RECORDS.map((r, i) => {
    const ratio = r.width && r.height ? ` style="aspect-ratio:${r.width}/${r.height}"` : "";
    const picked = SELECTED.has(r.image);
    return `
    <figure class="card${picked ? " picked" : ""}"${ratio} data-index="${i}">
      <img src="${withToken(`/api/image?path=${encodeURIComponent(r.image)}`)}" alt="" loading="lazy">
      ${SELECT_MODE ? `<span class="pick">${picked ? "✓" : ""}</span>` : ""}
      <div class="quick-bar">
        <button type="button" data-quick="download" title="下载">⤓</button>
        <button type="button" data-quick="copy" title="复制提示词">⧉</button>
        <button type="button" data-quick="variant" title="以此发起变体">变</button>
        <button type="button" data-quick="reference" title="用作参考">参</button>
        <button type="button" data-quick="fav" title="收藏">${r.rating ? "★" : "☆"}</button>
      </div>
      ${r.rating ? `<span class="star-badge">★${r.rating}</span>` : ""}
      <figcaption>${esc((r.prompt || "").slice(0, 70))}</figcaption>
    </figure>`;
  }).join("");
  updateBatchBar();
  attachImageRetry();
  grid.onclick = (e) => {
    const quick = e.target.closest("[data-quick]");
    const cardEl = e.target.closest(".card");
    if (!cardEl) return;
    const index = Number(cardEl.dataset.index);
    const record = RECORDS[index];
    if (!record) return;
    if (SELECT_MODE) {
      e.stopPropagation();
      if (e.shiftKey && LAST_PICK_INDEX >= 0 && LAST_PICK_INDEX !== index) {
        // Shift 区间选择
        const [from, to] = [Math.min(LAST_PICK_INDEX, index), Math.max(LAST_PICK_INDEX, index)];
        for (let i = from; i <= to; i += 1) if (RECORDS[i]) SELECTED.add(RECORDS[i].image);
      } else if (SELECTED.has(record.image)) {
        SELECTED.delete(record.image);
      } else {
        SELECTED.add(record.image);
      }
      LAST_PICK_INDEX = index;
      renderGallery();
      return;
    }
    if (quick) {
      e.stopPropagation();
      const act = quick.dataset.quick;
      if (act === "download") downloadImage(record);
      else if (act === "copy") copyPrompt(record.prompt || "");
      else if (act === "variant") variantFrom(record);
      else if (act === "reference") referenceFrom(record);
      else if (act === "fav") toggleFavorite(record);
      return;
    }
    openDetail(record);
  };
}

function setSelectMode(on) {
  SELECT_MODE = on;
  SELECTED.clear();
  $("select-mode").classList.toggle("on", on);
  renderGallery();
}

function updateBatchBar() {
  const bar = $("batch-bar");
  const count = SELECTED.size;
  bar.classList.toggle("hidden", !SELECT_MODE || count === 0);
  $("batch-count").textContent = String(count);
}

function selectAllVisible() {
  for (const record of RECORDS) SELECTED.add(record.image);
  LAST_PICK_INDEX = RECORDS.length - 1;
  renderGallery();
}

/* ---------- 访问令牌（部署到带认证的服务器时用） ---------- */

function renderTokenState() {
  const state = $("token-state");
  if (!state) return;
  const has = Boolean(AUTH_TOKEN);
  state.textContent = has ? `当前已设置令牌（${AUTH_TOKEN.slice(0, 6)}…），存于本浏览器` : "当前未设置令牌（本地直连模式）";
  const invite = $("token-invite");
  if (invite) invite.classList.toggle("hidden", !has);
}

function copyInviteLink() {
  if (!AUTH_TOKEN) return;
  copyText(`${location.origin}/?token=${AUTH_TOKEN}`, "邀请链接");
}

function saveToken() {
  const value = $("token-input").value.trim();
  try {
    if (value) localStorage.setItem("imagegen-token", value);
    else localStorage.removeItem("imagegen-token");
  } catch (e) {
    toast("浏览器不允许保存令牌（隐私模式？）", "error");
    return;
  }
  location.reload();
}

function clearToken() {
  try {
    localStorage.removeItem("imagegen-token");
  } catch (e) {
    /* ignore */
  }
  location.reload();
}

/* ---------- 项目名补全 ---------- */

function refreshProjectDatalist() {
  let list = $("project-list");
  if (!list) {
    list = document.createElement("datalist");
    list.id = "project-list";
    document.body.append(list);
    for (const id of ["project", "detail-project"]) {
      const input = $(id);
      if (input) input.setAttribute("list", "project-list");
    }
  }
  const projects = [...new Set(RECORDS.map((r) => r.project).filter(Boolean))].sort();
  list.innerHTML = projects.map((p) => `<option value="${esc(p)}"></option>`).join("");
}

async function batchDownload() {
  if (!SELECTED.size) return;
  const res = await fetch(withToken("/api/zip"), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(AUTH_TOKEN ? { "X-Auth-Token": AUTH_TOKEN } : {}) },
    body: JSON.stringify({ images: [...SELECTED] }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    toast(err.detail || "打包下载失败", "error");
    return;
  }
  const blob = await res.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `imagegen-${SELECTED.size}.zip`;
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
  toast(`已打包 ${SELECTED.size} 张`, "success");
}

async function batchDelete() {
  const count = SELECTED.size;
  if (!count) return;
  if (!window.confirm(`删除所选的 ${count} 张图片（移入回收站，可撤销）？`)) return;
  try {
    const res = await api("/api/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ images: [...SELECTED] }),
    });
    offerRestoreUndo(res, `已删除 ${(res.deleted || []).length || res.count} 张`);
    SELECTED.clear();
    setSelectMode(false);
    await loadHistory();
  } catch (err) {
    toast(err.message, "error");
  }
}

// 删除后给一次「撤销」机会（服务端是移入回收站，可原样放回）
function offerRestoreUndo(res, message) {
  const moved = (res && res.moved) || [];
  if (!moved.length) {
    toast(message, "success");
    return;
  }
  toast(message, "success", {
    label: "撤销",
    onClick: async () => {
      try {
        const restored = await api("/api/restore", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ moved }),
        });
        toast(`已恢复 ${restored.count} 个文件`, "success");
        await loadHistory();
      } catch (err) {
        toast(`恢复失败：${err.message}`, "error");
      }
    },
  });
}

async function deleteCurrent() {
  if (!CURRENT) return;
  const name = CURRENT.image.split(/[\\/]/).pop();
  if (!window.confirm(`删除这张图片及其账本记录？\n${name}\n（移入回收站，可撤销，7 天后自动清理）`)) return;
  try {
    const res = await api("/api/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ images: [CURRENT.image] }),
    });
    closeDetail();
    await loadHistory();
    offerRestoreUndo(res, "已删除");
  } catch (err) {
    toast(err.message, "error");
  }
}

// 图片加载失败（网关/C 端瞬时故障、请求被取消）时自动重试一次，失败则标记出来
function attachImageRetry() {
  if (attachImageRetry.done) return;
  attachImageRetry.done = true;
  document.addEventListener(
    "error",
    (e) => {
      const img = e.target;
      if (!img || img.tagName !== "IMG") return;
      const tries = Number(img.dataset.retry || 0);
      if (tries >= 1) {
        const card = img.closest(".card");
        if (card) card.classList.add("img-failed");
        return;
      }
      img.dataset.retry = String(tries + 1);
      const base = img.src.split("&_r=")[0];
      setTimeout(() => {
        img.src = `${base}&_r=${Date.now()}`;
      }, 600);
    },
    true,
  );
}

function downloadImage(record) {
  const a = document.createElement("a");
  a.href = withToken(`/api/image?path=${encodeURIComponent(record.image)}&download=1`);
  a.download = record.image.split(/[\\/]/).pop();
  document.body.append(a);
  a.click();
  a.remove();
}

function variantFrom(record) {
  const draft = $("prompt").value.trim();
  const target = (record.prompt || "").trim();
  if (draft && draft !== target) {
    if (!window.confirm("提示词框里还有内容，用这张图的提示词覆盖它？\n（取消则先复制走原草稿）")) return;
  }
  setMode("t2i");
  $("prompt").value = record.prompt || "";
  if (record.model) $("model").value = record.model;
  const params = record.parameters || {};
  setPresetChip(params.preset || "");
  $("size").value = params.size || "";
  $("quality").value = params.quality || "";
  saveDraft();
  closeDetail();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function copyText(text, label = "内容") {
  try {
    await navigator.clipboard.writeText(text);
    toast(`${label}已复制`, "success");
    return;
  } catch (e) {
    /* 无用户手势 / 权限被拒时回退到 execCommand */
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.cssText = "position:fixed;opacity:0;";
    document.body.append(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    toast(ok ? `${label}已复制` : "复制失败（浏览器限制剪贴板）", ok ? "success" : "error");
  } catch (e) {
    toast("复制失败（浏览器限制剪贴板）", "error");
  }
}

async function copyPrompt(text) {
  if (!text) return;
  await copyText(text, "提示词");
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

function renderFavButton() {
  const btn = $("detail-fav");
  const on = Boolean(CURRENT && CURRENT.rating);
  btn.textContent = on ? "★ 已收藏" : "☆ 收藏";
  btn.classList.toggle("on", on);
}

function openDetail(record) {
  CURRENT = record;
  $("detail-img").src = withToken(`/api/image?path=${encodeURIComponent(record.image)}`);
  $("detail-prompt").textContent = record.prompt || "";
  renderFavButton();
  const params = record.parameters || {};
  const rows = [
    ["模型", record.model], ["编号选择", record.choice],
    ["预设", params.preset], ["尺寸", params.size], ["质量", params.quality],
    ["格式", params.output_format], ["张数", params.n],
    ["项目", record.project],
    ["生成时间", record.created_at],
  ].filter(([, v]) => v !== null && v !== undefined && v !== "");
  $("detail-params").innerHTML = rows
    .map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("");
  $("detail-project").value = record.project || "";
  $("detail").classList.remove("hidden");
  $("detail-backdrop").classList.remove("hidden");
}

async function onToggleFav() {
  if (!CURRENT) return;
  const next = CURRENT.rating ? 0 : 5;
  await api("/api/rate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ image: CURRENT.image, rating: next }),
  });
  CURRENT.rating = next;
  renderFavButton();
  renderGallery();
}

/* ---------- lightbox ---------- */

let LB_INDEX = -1; // 放大视图当前在图库里的下标，用于 ←/→ 翻页

function renderLightbox() {
  const record = RECORDS[LB_INDEX];
  if (!record) return;
  $("lightbox-img").src = withToken(`/api/image?path=${encodeURIComponent(record.image)}`);
  $("lightbox-counter").textContent = `${LB_INDEX + 1} / ${RECORDS.length}`;
}

function openLightbox() {
  if (!CURRENT) return;
  const index = RECORDS.findIndex((r) => r.image === CURRENT.image);
  LB_INDEX = index >= 0 ? index : 0;
  renderLightbox();
  $("lightbox").classList.remove("hidden");
  showLightboxHint();
}

// 首次打开放大视图时提示一次键位（之后不再打扰）
function showLightboxHint() {
  let seen = false;
  try {
    seen = localStorage.getItem("imagegen-lightbox-hint") === "1";
  } catch (e) {
    seen = true;
  }
  if (seen) return;
  const hint = document.createElement("div");
  hint.className = "toast toast-info show";
  hint.textContent = "← / → 翻页 · Esc 或点背景关闭";
  hint.style.cssText = "position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:82;";
  document.body.append(hint);
  try {
    localStorage.setItem("imagegen-lightbox-hint", "1");
  } catch (e) {
    /* ignore */
  }
  setTimeout(() => hint.remove(), 3200);
}

function stepLightbox(delta) {
  if (LB_INDEX < 0 || !RECORDS.length) return;
  LB_INDEX = (LB_INDEX + delta + RECORDS.length) % RECORDS.length;
  CURRENT = RECORDS[LB_INDEX];
  renderLightbox();
  renderFavButton();
}

function closeLightbox() {
  $("lightbox").classList.add("hidden");
  $("lightbox-img").src = "";
  $("lightbox-counter").textContent = "";
  LB_INDEX = -1;
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

/* ---------- 版本与更新面板：服务端升级后，徽章亮橙点、面板可手动检查/一键刷新 ---------- */

const CHANGELOG_URL = "https://github.com/xinghe-labs/imagegen-studio/blob/main/CHANGELOG.md";
let PAGE_VERSION = ""; // 页面静态资源来自的版本（首次加载时定格）
let SERVER_VERSION = ""; // 服务端当前运行的版本（轮询/手动检查刷新）
let updatePromptShown = false;

function compareVersions(a, b) {
  const pa = String(a).replace(/^v/, "").split(".").map(Number);
  const pb = String(b).replace(/^v/, "").split(".").map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i += 1) {
    const da = pa[i] || 0;
    const db = pb[i] || 0;
    if (da !== db) return da - db;
  }
  return 0;
}

function renderUpdateState() {
  const newer = SERVER_VERSION && compareVersions(SERVER_VERSION, PAGE_VERSION) > 0;
  const differs = Boolean(SERVER_VERSION && PAGE_VERSION && SERVER_VERSION !== PAGE_VERSION);
  if ($("update-dot")) $("update-dot").classList.toggle("hidden", !newer);
  if ($("update-current")) $("update-current").textContent = `v${PAGE_VERSION}`;
  if ($("update-latest")) {
    $("update-latest").textContent = SERVER_VERSION ? `服务端 v${SERVER_VERSION}` : "";
  }
  const status = $("update-status");
  if (status) {
    status.classList.remove("ok", "newer", "drift");
    status.classList.toggle("hidden", !SERVER_VERSION);
    if (SERVER_VERSION && !differs) {
      status.classList.add("ok");
      status.textContent = "✓ 已是最新";
    } else if (newer) {
      status.classList.add("newer");
      status.textContent = `⬇ 服务端有新版 v${SERVER_VERSION}`;
    } else if (differs) {
      status.classList.add("drift");
      status.textContent = `服务端版本 v${SERVER_VERSION}，低于页面`;
    }
  }
  if ($("update-apply")) {
    $("update-apply").classList.toggle("hidden", !differs);
    $("update-apply").textContent = newer ? "刷新加载新版" : "刷新页面对齐版本";
  }
}

async function checkForUpdate() {
  if (!META.version) return;
  try {
    const m = await api("/api/meta");
    SERVER_VERSION = m.version || SERVER_VERSION;
    renderUpdateState();
    if (SERVER_VERSION && PAGE_VERSION && SERVER_VERSION !== PAGE_VERSION && !updatePromptShown) {
      updatePromptShown = true; // 每次页面加载只提示一次；不点也行，下次刷新自然拿到新版
      toast(`服务端已更新到 v${SERVER_VERSION}`, "info", {
        label: "刷新",
        onClick: () => location.reload(),
      });
    }
  } catch (e) {
    /* 服务端重启中 / 暂不可达：下个周期再看 */
  }
}

function watchVersion() {
  setInterval(checkForUpdate, 5 * 60 * 1000);
  window.addEventListener("focus", checkForUpdate); // 切回标签页时立即查一次
}

function toggleUpdatePanel(force) {
  const panel = $("update-panel");
  const show = force !== undefined ? force : panel.classList.contains("hidden");
  panel.classList.toggle("hidden", !show);
  if (show) {
    renderUpdateState();
    checkUpstream(false); // 面板打开时顺手查一次上游（服务端有 1h 缓存）
  }
}

/* ---------- 用户与令牌管理（管理员，users 模式） ---------- */

function maskToken(token) {
  return token.length > 12 ? `${token.slice(0, 6)}…${token.slice(-4)}` : "…";
}

async function renderUsers() {
  const wrap = $("users-admin");
  if (!wrap) return;
  wrap.classList.toggle("hidden", !META.is_admin);
  if (!META.is_admin) return;
  const list = $("users-list");
  try {
    const data = await api("/api/users");
    list.innerHTML = data.users.map((u) => `
      <div class="user-row" data-name="${esc(u.name)}">
        <span class="user-name">${esc(u.name)}${u.admin ? " · 管理员" : ""}${u.is_me ? "（我）" : ""}</span>
        <span class="muted user-token">token ${maskToken(u.token)}</span>
        <span class="muted">${esc(u.library || `默认图库/${u.name}`)}</span>
        <span class="user-actions">
          <button type="button" data-act="reveal" data-token="${esc(u.token)}">显示</button>
          <button type="button" data-act="link" data-token="${esc(u.token)}">邀请链接</button>
          <button type="button" data-act="rotate" data-name="${esc(u.name)}">换发</button>
          ${u.is_me ? "" : `<button type="button" data-act="remove" data-name="${esc(u.name)}">移除</button>`}
        </span>
      </div>`).join("");
    for (const btn of list.querySelectorAll("button[data-act]")) {
      btn.addEventListener("click", () => onUserAction(btn));
    }
  } catch (e) {
    list.innerHTML = `<p class="muted">用户列表加载失败：${esc(e.message)}</p>`;
  }
}

async function onUserAction(btn) {
  const act = btn.dataset.act;
  try {
    if (act === "reveal") {
      const span = btn.closest(".user-row").querySelector(".user-token");
      const shown = span.dataset.full === "1";
      span.textContent = shown ? `token ${maskToken(btn.dataset.token)}` : `token ${btn.dataset.token}`;
      span.dataset.full = shown ? "" : "1";
      btn.textContent = shown ? "显示" : "隐藏";
    } else if (act === "link") {
      await copyText(`${location.origin}/?token=${btn.dataset.token}`, "邀请链接");
    } else if (act === "rotate") {
      if (!window.confirm(`换发 ${btn.dataset.name} 的令牌？旧令牌立即失效。`)) return;
      const r = await api(`/api/users/${encodeURIComponent(btn.dataset.name)}/rotate`, { method: "POST" });
      showUserResult(`${r.name} 的新令牌已生成`, r.link);
      await loadMeta();
      await renderUsers();
    } else if (act === "remove") {
      if (!window.confirm(`移除用户 ${btn.dataset.name}？（其图库文件不动）`)) return;
      await api(`/api/users/${encodeURIComponent(btn.dataset.name)}`, { method: "DELETE" });
      toast(`已移除 ${btn.dataset.name}`, "success");
      await renderUsers();
    }
  } catch (e) {
    toast(e.message, "error");
  }
}

function showUserResult(title, link) {
  const box = $("user-result");
  box.classList.remove("hidden");
  box.innerHTML = `
    <b>${esc(title)}</b>
    <div class="user-result-link">${esc(link)}</div>
    <button type="button" id="user-copy" class="btn-small">复制邀请链接</button>
    <span class="muted">令牌也可随时在上方列表点「显示」查看</span>`;
  $("user-copy").addEventListener("click", () => copyText(link, "邀请链接"));
}

let UPSTREAM_VERSION = ""; // GitHub 上的最新 tag
let UPSTREAM_CHECK = null; // 最近一次上游检查的原始结果（含失败原因）

async function checkUpstream(force) {
  try {
    const m = await api(`/api/version-check${force ? "?force=1" : ""}`);
    UPSTREAM_CHECK = m;
    UPSTREAM_VERSION = m.ok ? m.latest_upstream || "" : "";
  } catch (e) {
    UPSTREAM_CHECK = { ok: false, detail: e.message };
    UPSTREAM_VERSION = "";
  }
  renderUpstreamState();
}

function renderUpstreamState() {
  const el = $("update-upstream");
  const behind = Boolean(
    UPSTREAM_VERSION && SERVER_VERSION && compareVersions(UPSTREAM_VERSION, SERVER_VERSION) > 0
  );
  if (el) {
    const failed = Boolean(UPSTREAM_CHECK && !UPSTREAM_CHECK.ok);
    el.classList.toggle("hidden", !behind && !failed);
    el.classList.toggle("upstream-error", failed && !behind);
    el.textContent = "";
    if (behind) {
      el.append(`GitHub 最新 v${UPSTREAM_VERSION} · `);
      const a = document.createElement("a");
      a.href = "https://github.com/xinghe-labs/imagegen-studio/blob/main/DEPLOY.md";
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = "部署文档";
      el.append(a);
    } else if (failed) {
      el.title = UPSTREAM_CHECK.detail || "";
      el.append("GitHub 检查失败（服务器连不上 GitHub？）");
    }
  }
  const btn = $("update-selfupdate");
  if (btn) btn.classList.toggle("hidden", !(behind && META.self_update_allowed));
}

async function selfUpdateServer() {
  if (!window.confirm("从 GitHub 拉取最新版本并重启服务端？\n正在进行的生成任务会中断。")) return;
  const btn = $("update-selfupdate");
  btn.disabled = true;
  try {
    const r = await api("/api/self-update", { method: "POST" });
    if (!r.updating) {
      btn.disabled = false;
      toast(r.reason || "已是上游最新", "info");
      return;
    }
    toast("服务端更新中，正在重启……", "success");
    // 重启期间 meta 会暂时不可达；轮询直到服务端带着新版本回来
    const oldServerVersion = SERVER_VERSION;
    const deadline = Date.now() + 120_000;
    const timer = setInterval(async () => {
      if (Date.now() > deadline) {
        clearInterval(timer);
        btn.disabled = false;
        return;
      }
      try {
        const m = await api("/api/meta");
        if (m.version && m.version !== oldServerVersion) {
          clearInterval(timer);
          SERVER_VERSION = m.version;
          renderUpdateState();
          renderUpstreamState();
          btn.disabled = false;
          toast(`服务端已更新到 v${m.version}`, "success", {
            label: "刷新",
            onClick: () => location.reload(),
          });
        }
      } catch (e) {
        /* 重启中，继续等 */
      }
    }, 3000);
  } catch (e) {
    btn.disabled = false;
    toast(e.message, "error");
  }
}

/* ---------- wiring ---------- */

async function init() {
  // 先绑事件再拉数据：无令牌首访时 loadMeta 会 401，
  // 令牌面板的保存/清除按钮必须已可用，否则认证提示引导的是一条死路
  bindEvents();
  loadDraft();
  renderTokenState();
  try {
    await loadMeta();
    renderProfiles();
    renderUsers();
    watchVersion();
    await loadHistory();
    refreshProjectDatalist();
  } catch (e) {
    toast(`初始化失败：${e.message}`, "error");
  }
}

function bindEvents() {
  $("generate-btn").addEventListener("click", onGenerate);
  $("cancel-job").addEventListener("click", cancelJob);
  $("token-save").addEventListener("click", saveToken);
  $("token-clear").addEventListener("click", clearToken);
  $("token-invite").addEventListener("click", copyInviteLink);
  $("user-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("user-name").value.trim();
    const library = $("user-library").value.trim();
    try {
      const r = await api("/api/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, library: library || null }),
      });
      $("user-name").value = "";
      $("user-library").value = "";
      showUserResult(`已添加 ${r.name}`, r.link);
      await loadMeta();
      await renderUsers();
    } catch (err) {
      toast(err.message, "error");
    }
  });
  // 版本徽章与更新面板
  $("app-version").addEventListener("click", (e) => {
    e.stopPropagation();
    toggleUpdatePanel();
  });
  $("update-check").addEventListener("click", async (e) => {
    e.stopPropagation();
    const btn = $("update-check");
    btn.classList.add("spinning");
    await Promise.allSettled([checkForUpdate(), checkUpstream(true)]);
    btn.classList.remove("spinning");
  });
  $("update-selfupdate").addEventListener("click", selfUpdateServer);
  $("update-apply").addEventListener("click", () => location.reload());
  document.addEventListener("click", (e) => {
    const panel = $("update-panel");
    if (!panel.classList.contains("hidden") && !panel.contains(e.target)) {
      panel.classList.add("hidden");
    }
  });
  // 输入变化即存草稿（提示词用防抖）
  let draftTimer = null;
  const scheduleDraft = () => {
    if (draftTimer) clearTimeout(draftTimer);
    draftTimer = setTimeout(saveDraft, 400);
  };
  $("prompt").addEventListener("input", scheduleDraft);
  window.addEventListener("beforeunload", saveDraft);
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
  $("refresh").addEventListener("click", loadHistory);
  $("search").addEventListener("change", loadHistory);
  $("filter-model").addEventListener("change", loadHistory);
  $("filter-project").addEventListener("change", loadHistory);
  $("filter-days").addEventListener("change", loadHistory);
  $("fav-only").addEventListener("change", loadHistory);
  $("select-mode").addEventListener("click", () => setSelectMode(!SELECT_MODE));
  $("batch-all").addEventListener("click", selectAllVisible);
  $("batch-zip").addEventListener("click", () => batchDownload().catch((e) => toast(e.message, "error")));
  $("batch-delete").addEventListener("click", batchDelete);
  $("batch-clear").addEventListener("click", () => { SELECTED.clear(); LAST_PICK_INDEX = -1; renderGallery(); });
  $("delete-img").addEventListener("click", deleteCurrent);
  $("close-detail").addEventListener("click", closeDetail);
  $("detail-backdrop").addEventListener("click", closeDetail);
  document.addEventListener("keydown", (e) => {
    const lightboxOpen = !$("lightbox").classList.contains("hidden");
    if (e.key === "Escape") {
      if (!$("update-panel").classList.contains("hidden")) {
        $("update-panel").classList.add("hidden"); // 先关更新面板
        return;
      }
      if (lightboxOpen) closeLightbox();
      else closeDetail();
      return;
    }
    if (!lightboxOpen) return;
    if (e.key === "ArrowRight") stepLightbox(1);
    else if (e.key === "ArrowLeft") stepLightbox(-1);
  });
  $("detail-fav").addEventListener("click", onToggleFav);
  $("detail-img").addEventListener("click", openLightbox);
  $("lightbox-close").addEventListener("click", closeLightbox);
  $("lightbox").addEventListener("click", (e) => {
    if (e.target === $("lightbox")) closeLightbox();
  });
  // 参数控件变化即记住（模型/张数/预设/比例/高级字段/项目/模式）
  for (const el of document.querySelectorAll("#model, #n, #size, #quality, #format, #project")) {
    el.addEventListener("change", saveDraft);
  }
  $("preset").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-v]");
    if (!btn) return;
    for (const b of document.querySelectorAll("#preset button")) b.classList.toggle("on", b === btn);
    setTimeout(saveDraft, 0);
  });
  document.querySelector("#ratio").addEventListener("click", () => setTimeout(saveDraft, 0));
  for (const tab of document.querySelectorAll(".tab")) tab.addEventListener("click", () => setTimeout(saveDraft, 0));
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
  $("download-img").addEventListener("click", () => {
    if (CURRENT) downloadImage(CURRENT);
  });
  $("ratio").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-ratio]");
    if (!btn) return;
    for (const b of document.querySelectorAll("#ratio button")) b.classList.toggle("on", b === btn);
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

init().catch((e) => toast(`初始化失败：${e.message}`, "error"));
