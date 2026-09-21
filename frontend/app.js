function ensureCuteTheme() {
  const old = document.getElementById("cute-theme");
  if (old) old.remove();
  if (document.getElementById("cute-theme-v2")) return;
  const s = document.createElement("style");
  s.id = "cute-theme-v2";
  s.textContent = `
    body { background: #f3fbf7 !important; }
    .navbar, nav.navbar, .navbar-dark { background: linear-gradient(90deg,#3db8a0,#7fd3c3) !important; }
    .card { border-radius: 18px !important; border: none !important; box-shadow: 0 8px 20px rgba(61,184,160,.12) !important; }
    .metric-card { border-radius: 16px !important; background: #fff !important; }
    #summaryText { background: #e8f8f3 !important; border-color: #b7eadc !important; color: #35514a !important; }
    .btn-primary { background: #3db8a0 !important; border-color: #3db8a0 !important; border-radius: 999px !important; }
    .btn-outline-primary { color: #2a8f7c !important; border-color: #7fd3c3 !important; border-radius: 999px !important; }
  `;
  document.head.appendChild(s);
}

const API = "";  // same origin
let token = localStorage.getItem("babymomo_token") || "";
let currentUser = null;
let currentPage = 1;
let dupPage = 1;
const PAGE_SIZE = 30;

// ---------- helpers ----------
async function api(path, options = {}) {
  const headers = options.headers || {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (!(options.body instanceof FormData)) {
    headers["Content-Type"] = headers["Content-Type"] || "application/json";
  }
  const res = await fetch(API + path, { ...options, headers });
  if (res.status === 401) {
    doLogout();
    throw new Error("登入已過期");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText || "請求失敗");
  return data;
}

function stageClass(stage) {
  if (!stage) return "bg-secondary";
  if (String(stage).includes("嚴重")) return "bg-danger";
  if (stage === "肌少症") return "bg-warning text-dark";
  if (String(stage).includes("前期")) return "bg-info text-dark";
  return "bg-success";
}

function alertProfile(a, v) {
  const msg = String(a.message || "") + " " + String(a.title || "");
  let gender = a.gender || v.gender || "";
  if (!gender) {
    if (/女/.test(msg) || /gender[：: ]*F/i.test(msg)) gender = "F";
    else if (/男/.test(msg) || /gender[：: ]*M/i.test(msg)) gender = "M";
  }
  if (gender === "男") gender = "M";
  if (gender === "女") gender = "F";
  let age = a.age || v.age;
  if (age == null) {
    const m = msg.match(/(\d{2,3})\s*歲/);
    if (m) age = Number(m[1]);
  }
  age = age != null ? Number(age) : null;
  return { gender, age };
}

const FFM_M = {61:[53,60],62:[52,59],63:[52,58],64:[51,57],65:[50,57],66:[50,56],67:[49,55],68:[48,55],69:[48,54],70:[47,53],71:[46,53],72:[46,52],73:[46,52],74:[46,51],75:[45,51],76:[45,51],77:[45,50],78:[44,50],79:[44,50],80:[44,50],81:[44,50],82:[42,48],83:[37,42],84:[37,42],85:[37,42],86:[42,47],87:[37,42],88:[41,46],89:[44,50],90:[48,54],91:[51,58],92:[55,62],93:[58,66],94:[62,70],95:[65,74],96:[69,78]};
const FFM_F = {61:[33,35],62:[33,35],63:[33,35],64:[33,35],65:[33,35],66:[33,35],67:[33,35],68:[33,35],69:[33,35],70:[33,35],71:[33,35],72:[33,35],73:[33,35],74:[33,35],75:[31,33],76:[32,34],77:[32,34],78:[33,35],79:[34,36],80:[34,36],81:[33,35],82:[32,34],83:[31,33],84:[33,35],85:[34,36],86:[34,36],87:[34,36],88:[34,36],89:[34,36],90:[34,36],91:[33,35],92:[33,35],93:[33,35],94:[33,35],95:[32,34],96:[32,34]};
const WALK_M15 = [70,71,74,75,76,80,82,88,89];
const WALK_F15 = [61,62,63,67,68,69,71,73,75,78,80,81,84,88,89];

function walkCutoff(age, male) {
  const a = age || 70;
  if (male) return WALK_M15.includes(a) ? 15 : 20;
  return WALK_F15.includes(a) ? 15 : 20;
}

function ageSexNorm(age, gender) {
  const male = gender === "M";
  const a = age || 70;
  const ffm = (male ? FFM_M : FFM_F)[a] || (male ? [44, 50] : [33, 35]);
  return {
    grip: male ? 28 : 18,
    chair: 12,
    walk: walkCutoff(a, male),
    smi: male ? 7.0 : 5.7,
    bmiLow: 18.5,
    bmiHigh: 24,
    sysLow: 100,
    sysHigh: 120,
    diaLow: 60,
    diaHigh: 80,
    pulseLow: 60,
    pulseHigh: 100,
    ffmLow: ffm[0],
    ffmHigh: ffm[1],
  };
}

function pickNum(obj, keys) {
  if (!obj) return null;
  for (const k of keys) {
    const val = obj[k];
    if (val !== undefined && val !== null && val !== "" && val !== "-") {
      const n = Number(val);
      if (!Number.isNaN(n)) return n;
    }
  }
  return null;
}

const FFM_BY_ID = {
  M120478603: 63.99, F103371584: 47.52, F203437310: 35.87, A203748671: 34.56,
  A210527799: 32.43, A101673222: 48.06, A101391305: 43.56, L200749693: 38.24,
  A100956115: 53.63, A201221695: 34.63, F200581946: 33.08, D100453238: 41.93,
  A102177792: 41.98, V200264434: 40.16, A103246983: 44.66, F201321747: 30.81,
  F201477674: 35.46, A104160268: 39.48, L101053210: 44.07, N101815070: 55.75,
};

function resolveFfm(a, v) {
  const fromV = pickNum(v, ["ffm", "fat_free_mass", "lean_mass", "smi", "除脂肪量"]);
  if (fromV != null && fromV > 15) return fromV;
  const fromMsg = parseMsgNum(a && a.message, ["除脂肪量"]);
  if (fromMsg != null && fromMsg > 15) return fromMsg;
  const id = String((a && a.id_card) || "").replace(/\s+/g, "").toUpperCase();
  if (FFM_BY_ID[id]) return FFM_BY_ID[id];
  const nameMap = {"洪讚生":63.99,"蘇正義":47.52,"黃麗雲":35.87,"朱美智":34.56,"鮑露":32.43,"范陽福":48.06,"林天助":43.56,"林洪雪":38.24,"劉衛中":53.63,"廖素嶺":34.63,"張玉慧":33.08,"許雅智":41.93,"黃峻金":41.98,"潘月琴":40.16,"蘇怡仁":44.66,"陳妃妃":30.81,"黃林昭":35.46,"高鴻模":39.48,"吳柏賢":44.07,"梁萬興":55.75};
  const nm = String((a && a.user_name) || "").trim();
  if (nameMap[nm]) return nameMap[nm];
  const w = pickNum(v, ["weight"]);
  const fat = pickNum(v, ["fat_mass", "body_fat_mass", "fat_kg"]);
  if (w != null && fat != null) return Math.round((w - fat) * 100) / 100;
  return fromV;
}

function parseMsgNum(msg, labels) {
  const text = String(msg || "");
  for (const lab of labels) {
    const m = text.match(new RegExp(lab + "[：:\\s]+([0-9]+(?:\\.[0-9]+)?)"));
    if (m) return Number(m[1]);
  }
  return null;
}

function stageBadge(stage) {
  const map = {
    "正常": "badge-normal",
    "肌少症前期": "badge-pre",
    "肌少症": "badge-sarco",
    "嚴重肌少症": "badge-severe",
  };
  return `<span class="badge ${map[stage] || "bg-secondary"}">${stage || "-"}</span>`;
}

// ---------- Auth ----------
async function doLogin() {
  const username = document.getElementById("loginUsername").value.trim();
  const password = document.getElementById("loginPassword").value;
  const errBox = document.getElementById("loginError");
  errBox.classList.add("d-none");
  try {
    const form = new URLSearchParams();
    form.append("username", username);
    form.append("password", password);
    const res = await fetch(API + "/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "登入失敗");
    token = data.access_token;
    currentUser = data.user;
    localStorage.setItem("babymomo_token", token);
    localStorage.setItem("babymomo_user", JSON.stringify(currentUser));
    enterApp();
  } catch (e) {
    errBox.textContent = e.message;
    errBox.classList.remove("d-none");
  }
}

async function doRegister() {
  const username = document.getElementById("regUsername").value.trim();
  const display_name = document.getElementById("regDisplayName").value.trim() || username;
  const password = document.getElementById("regPassword").value;
  const errBox = document.getElementById("regError");
  errBox.classList.add("d-none");
  try {
    await api("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, password, display_name, role: "staff" }),
    });
    alert("註冊成功，請登入");
    document.querySelector('[data-bs-target="#tabLogin"]').click();
  } catch (e) {
    errBox.textContent = e.message;
    errBox.classList.remove("d-none");
  }
}

function doLogout() {
  token = "";
  currentUser = null;
  localStorage.removeItem("babymomo_token");
  localStorage.removeItem("babymomo_user");
  document.getElementById("authOverlay").style.display = "flex";
  document.getElementById("app").style.display = "none";
}

function enterApp() {
  document.getElementById("authOverlay").style.display = "none";
  document.getElementById("app").style.display = "block";
  document.getElementById("navUser").textContent =
    `${currentUser.display_name}（${currentUser.role}）`;
  setRange(90);
  loadStats();
  loadAlertCount();
  showView("dashboard");
  // 每 60 秒更新未讀通報數
  if (!window._alertTimer) {
    window._alertTimer = setInterval(loadAlertCount, 60000);
  }
}

// ---------- Views ----------
function showView(name) {
  ["dashboard", "records", "duplicates", "alerts", "import", "api"].forEach((v) => {
    const el = document.getElementById(`view-${v}`);
    if (el) el.style.display = v === name ? "block" : "none";
  });
  document.querySelectorAll(".sidebar .nav-link").forEach((a) => a.classList.remove("active"));
  if (name === "records") loadRecords();
  if (name === "duplicates") loadDuplicates();
  if (name === "alerts") loadAlerts();
}

// ---------- Stats ----------
function setRange(days) {
  const end = new Date();
  const start = new Date();
  start.setDate(end.getDate() - days);
  document.getElementById("statStart").value = start.toISOString().slice(0, 10);
  document.getElementById("statEnd").value = end.toISOString().slice(0, 10);
  loadStats();
}

async function loadStats() {
  const start = document.getElementById("statStart").value;
  const end = document.getElementById("statEnd").value;
  let url = "/api/stats?";
  if (start) url += `start_date=${start}&`;
  if (end) url += `end_date=${end}&`;
  try {
    const s = await api(url);
    document.getElementById("sTotal").textContent = s.total_records;
    document.getElementById("sUsers").textContent = s.unique_users;
    document.getElementById("sGrip").textContent = s.avg_grip + " kg";
    document.getElementById("sMulti").textContent = s.multi_abnormal_rate + "%";
    document.getElementById("summaryText").textContent = s.summary_text;

    ensureCuteTheme();
    const pieColors = {
      "正常": "#3db8a0",
      "肌少症前期": "#f4c95d",
      "肌少症": "#f08a5d",
      "嚴重肌少症": "#d65a7a",
    };
    const pie = echarts.init(document.getElementById("pieChart"));
    const pieRows = (s.sarcopenia_pie || []).slice().reverse();
    pie.setOption({
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, borderRadius: 12 },
      grid: { left: 90, right: 28, top: 12, bottom: 24 },
      xAxis: { type: "value", splitLine: { lineStyle: { color: "#ffe8f0" } } },
      yAxis: { type: "category", data: pieRows.map((d) => d.name), axisLine: { show: false }, axisTick: { show: false } },
      series: [{
        type: "bar",
        barWidth: 18,
        label: { show: true, position: "right", formatter: "{c} 人", color: "#5a4a4a" },
        data: pieRows.map((d) => ({
          value: d.value,
          itemStyle: { color: pieColors[d.name] || "#cdb4db", borderRadius: [0, 16, 16, 0] },
        })),
      }],
    });

    const trend = echarts.init(document.getElementById("trendChart"));
    const m = s.monthly_trends;
    trend.setOption({
      color: ["#8ec5ff", "#ff8fab"],
      tooltip: { trigger: "axis", borderRadius: 12 },
      legend: { data: ["檢測次數", "平均握力"], textStyle: { color: "#5a4a4a" } },
      grid: { left: 40, right: 40, top: 40, bottom: 30 },
      xAxis: { type: "category", data: m.months, axisLine: { lineStyle: { color: "#f0c8d8" } } },
      yAxis: [
        { type: "value", name: "次數", splitLine: { lineStyle: { color: "#ffe8f0" } } },
        { type: "value", name: "kg", splitLine: { show: false } },
      ],
      series: [
        {
          name: "檢測次數",
          type: "bar",
          data: m.counts,
          barWidth: 22,
          itemStyle: { color: "#3db8a0", borderRadius: [10, 10, 4, 4] },
        },
        {
          name: "平均握力",
          type: "bar",
          yAxisIndex: 1,
          data: m.avg_grip,
          barWidth: 22,
          itemStyle: { color: "#7b9cff", borderRadius: [10, 10, 4, 4] },
        },
      ],
    });
  } catch (e) {
    document.getElementById("summaryText").textContent = "載入失敗：" + e.message;
  }
}

// ---------- Records ----------
async function loadRecords() {
  const q = document.getElementById("searchQ").value.trim();
  const start = document.getElementById("recStart").value;
  const end = document.getElementById("recEnd").value;
  let url = `/api/measurements?page=${currentPage}&page_size=${PAGE_SIZE}`;
  if (q) url += `&q=${encodeURIComponent(q)}`;
  if (start) url += `&start_date=${start}`;
  if (end) url += `&end_date=${end}`;

  try {
    const data = await api(url);
    const tbody = document.getElementById("recordsBody");
    tbody.innerHTML = "";
    data.items.forEach((r) => {
      const tr = document.createElement("tr");
      tr.style.cursor = "pointer";
      tr.onclick = () => openCase(r.id_card);
      tr.innerHTML = `
        <td class="text-primary">${r.id_card}</td>
        <td>${r.user_name}</td>
        <td>${r.gender === "M" ? "男" : "女"} / ${r.age || "-"}</td>
        <td>${r.measure_time || r.measure_date || "-"}</td>
        <td>${r.grip_strength != null ? r.grip_strength : "-"}</td>
        <td>${r.chair_stand_time != null ? r.chair_stand_time : "-"}</td>
        <td>${r.walking_time != null ? r.walking_time : "-"}</td>
        <td>${r.smi != null ? r.smi : "-"}</td>
        <td>${stageBadge(r.sarcopenia_stage)}</td>
        <td>${r.abnormal_count || 0}</td>
      `;
      tbody.appendChild(tr);
    });
    document.getElementById("recTotal").textContent = `共 ${data.total} 筆`;
    document.getElementById("pageInfo").textContent = `${data.page} / ${Math.max(1, Math.ceil(data.total / PAGE_SIZE))}`;
  } catch (e) {
    alert(e.message);
  }
}

function changePage(delta) {
  currentPage = Math.max(1, currentPage + delta);
  loadRecords();
}

async function openCase(idCard) {
  try {
    const data = await api(`/api/cases/${encodeURIComponent(idCard)}`);
    const p = data.profile;
    const latest = data.latest;
    document.getElementById("casePanel").style.display = "block";
    document.getElementById("caseName").innerHTML =
      `${p.user_name} ${stageBadge(latest.sarcopenia_stage)}`;
    document.getElementById("caseMeta").textContent =
      `${p.id_card} · ${p.gender === "M" ? "男" : "女"} · ${p.age || "-"}歲 · 身高 ${p.height || "-"}cm · 體重 ${p.weight || "-"}kg · BMI ${p.bmi || "-"} · 共 ${data.total_records} 筆紀錄`;

    const isMale = p.gender === "M";
    const metrics = [
      { label: "握力", val: latest.grip_strength, unit: "kg", std: isMale ? 28 : 18, higherBetter: true },
      { label: "五次坐站", val: latest.chair_stand_time, unit: "秒", std: 12, higherBetter: false },
      { label: "走路時間", val: latest.walking_time, unit: "秒", std: 20, higherBetter: false },
      { label: "除脂肪量", val: latest.smi, unit: "kg", std: isMale ? 7.0 : 5.7, higherBetter: true },
      { label: "血壓", val: latest.systolic ? `${latest.systolic}/${latest.diastolic || "-"}` : null, unit: "mmHg" },
      { label: "脈搏", val: latest.pulse, unit: "bpm" },
    ];
    const box = document.getElementById("caseMetrics");
    box.innerHTML = metrics.map((m) => {
      let cls = "metric-card";
      let status = "";
      if (m.std != null && m.val != null) {
        const pass = m.higherBetter ? m.val >= m.std : m.val < m.std;
        cls += pass ? " pass" : " fail";
        status = pass ? '<span class="text-success small">達標</span>' : '<span class="text-danger small">未達標</span>';
      }
      return `<div class="col-6 col-md-4 col-lg-2"><div class="${cls}">
        <div class="text-muted small">${m.label} ${status}</div>
        <div class="fs-5 fw-bold">${m.val != null ? m.val : "-"} <small class="text-muted">${m.unit || ""}</small></div>
      </div></div>`;
    }).join("");

    const hist = [...data.history].reverse();
    const latestH = hist[hist.length - 1] || {};
    const chart = echarts.init(document.getElementById("caseTrendChart"));
    chart.setOption({
      tooltip: { trigger: "axis", borderRadius: 12 },
      grid: { left: 70, right: 24, top: 12, bottom: 24 },
      xAxis: { type: "value", splitLine: { lineStyle: { color: "#e8f8f3" } } },
      yAxis: { type: "category", data: ["走路 秒", "坐站 秒", "握力 kg"], axisTick: { show: false } },
      series: [{
        type: "bar",
        barWidth: 16,
        label: { show: true, position: "right", color: "#35514a" },
        data: [
          { value: latestH.walking_time, itemStyle: { color: "#7b9cff", borderRadius: [0, 12, 12, 0] } },
          { value: latestH.chair_stand_time, itemStyle: { color: "#f4c95d", borderRadius: [0, 12, 12, 0] } },
          { value: latestH.grip_strength, itemStyle: { color: "#3db8a0", borderRadius: [0, 12, 12, 0] } },
        ],
      }],
    });
  } catch (e) {
    alert(e.message);
  }
}

function closeCase() {
  document.getElementById("casePanel").style.display = "none";
}

// ---------- Import ----------
async function doImport() {
  const fileInput = document.getElementById("importFile");
  if (!fileInput.files.length) {
    alert("請選擇檔案");
    return;
  }
  const fd = new FormData();
  fd.append("file", fileInput.files[0]);
  const box = document.getElementById("importResult");
  box.innerHTML = '<div class="alert alert-info">匯入中...</div>';
  try {
    const res = await fetch(API + "/api/import", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: fd,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "匯入失敗");
    box.innerHTML = `
      <div class="alert alert-success">
        成功匯入 <strong>${data.success}</strong> 筆，
        略過（重複） <strong>${data.skipped}</strong> 筆，
        失敗 <strong>${data.failed}</strong> 筆
      </div>
      ${data.messages.length ? `<ul class="small text-danger">${data.messages.map((m) => `<li>${m}</li>`).join("")}</ul>` : ""}
    `;
    loadStats();
  } catch (e) {
    box.innerHTML = `<div class="alert alert-danger">${e.message}</div>`;
  }
}

// ---------- Export ----------
function exportCSV() {
  const q = document.getElementById("searchQ").value.trim();
  const start = document.getElementById("recStart").value;
  const end = document.getElementById("recEnd").value;
  let url = API + "/api/export/csv?";
  if (q) url += `q=${encodeURIComponent(q)}&`;
  if (start) url += `start_date=${start}&`;
  if (end) url += `end_date=${end}&`;
  // use token via fetch + blob
  fetch(url, { headers: { Authorization: `Bearer ${token}` } })
    .then((r) => r.blob())
    .then((blob) => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "babymomo_export.csv";
      a.click();
    });
}

// ---------- Init ----------
(async function init() {
  if (token) {
    try {
      currentUser = await api("/api/auth/me");
      enterApp();
      return;
    } catch {
      doLogout();
    }
  }
})();

// Enter key login
document.getElementById("loginPassword")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") doLogin();
});


// ---------- Alerts 異常通報 ----------
async function loadAlertCount() {
  try {
    const data = await api("/api/alerts?only_unhandled=true&page_size=1");
    const n = data.unhandled || 0;
    ["alertBadge", "navAlertBadge"].forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (n > 0) {
        el.style.display = "";
        el.textContent = n > 99 ? "99+" : n;
      } else {
        el.style.display = "none";
      }
    });
  } catch (e) {
    /* ignore */
  }
}

function ensureAlertSearchBar() {
  if (document.getElementById("alertQ")) return;
  const cb = document.getElementById("alertOnlyUnhandled");
  const view = document.getElementById("view-alerts");
  if (!view || !cb) return;
  const bar = document.createElement("span");
  bar.className = "d-inline-flex flex-wrap gap-2 align-items-center";
  bar.innerHTML = `
    <input type="text" id="alertQ" class="form-control form-control-sm" style="max-width:180px" placeholder="身分證 / 姓名" />
    <input type="date" id="alertStart" class="form-control form-control-sm" style="max-width:140px" />
    <input type="date" id="alertEnd" class="form-control form-control-sm" style="max-width:140px" />
    <select id="alertStage" class="form-select form-select-sm" style="max-width:140px">
      <option value="">全部分期</option>
      <option>正常</option><option>肌少症前期</option><option>肌少症</option><option>嚴重肌少症</option>
    </select>
    <button class="btn btn-primary btn-sm" type="button" id="alertSearchBtn">查詢</button>
  `;
  cb.parentElement.insertAdjacentElement("beforebegin", bar);
  document.getElementById("alertSearchBtn").onclick = () => loadAlerts();
  document.getElementById("alertQ").addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadAlerts();
  });
}

function filterAlertItems(items) {
  const q = (document.getElementById("alertQ")?.value || "").trim().toLowerCase();
  const start = document.getElementById("alertStart")?.value || "";
  const end = document.getElementById("alertEnd")?.value || "";
  const stage = document.getElementById("alertStage")?.value || "";
  return (items || []).filter((a) => {
    if (q) {
      const blob = `${a.user_name || ""} ${a.id_card || ""} ${a.message || ""}`.toLowerCase();
      if (!blob.includes(q)) return false;
    }
    if (stage && String(a.sarcopenia_stage || "") !== stage) return false;
    const dt = String(a.created_at || a.measure_time || "").slice(0, 10);
    if (start && dt && dt < start) return false;
    if (end && dt && dt > end) return false;
    return true;
  });
}

async function loadAlerts() {
  ensureAlertSearchBar();
  const onlyUnhandled = document.getElementById("alertOnlyUnhandled")?.checked;
  let url = "/api/alerts?page_size=200";
  if (onlyUnhandled) url += "&only_unhandled=true";
  const box = document.getElementById("alertsList");
  if (!box) return;
  box.innerHTML = '<div class="text-muted">載入中...</div>';
  try {
    const data = await api(url);
    loadAlertCount();
    const items = filterAlertItems(data.items || []);
    if (!items.length) {
      box.innerHTML = '<div class="alert alert-success">沒有符合查詢的異常通報。</div>';
      return;
    }
    box.innerHTML = items.map((a) => {
      const sevClass = a.severity === "critical" ? "border-danger" : a.severity === "warning" ? "border-warning" : "border-info";
      const sevBadge = a.severity === "critical" ? "bg-danger" : a.severity === "warning" ? "bg-warning text-dark" : "bg-info";
      const stageBadge = stageClass(a.sarcopenia_stage);
      const v = a.vitals || {};
      const pf = alertProfile(a, v);
      const nrm = ageSexNorm(pf.age, pf.gender);
      const handled = a.is_handled
        ? `<span class="badge bg-success">已處理 by ${a.handled_by || "-"}</span>`
        : `<button class="btn btn-sm btn-primary" onclick="handleAlert(${a.id})">標記已關懷處理</button>`;
      return `<div class="card mb-3 ${sevClass}" style="border-left-width:5px">
        <div class="card-body">
          <div class="d-flex justify-content-between align-items-start gap-2 flex-wrap mb-2">
            <div>
              <span class="badge ${sevBadge} me-1">${a.severity === "critical" ? "緊急" : a.severity === "warning" ? "注意" : "提醒"}</span>
              <span class="badge ${stageBadge}">${a.sarcopenia_stage || "-"}</span>
              <strong class="ms-1">${a.user_name}</strong>
              <span class="text-muted small">（${a.id_card}）</span>
              <div class="small text-muted mt-1">${a.created_at ? a.created_at.replace("T", " ").slice(0, 19) : ""} · 異常 ${a.abnormal_count || 0} 項
                · 標準依據：${pf.gender === "F" ? "女" : pf.gender === "M" ? "男" : "-"} ${pf.age || "-"}歲</div>
            </div>
            <div class="d-flex gap-2">
              <button class="btn btn-sm btn-outline-success" onclick="pushAlertLine(${a.id})">傳 LINE</button>
              ${handled}
            </div>
          </div>
          <div class="row g-2 mb-2">
            ${vitalCard("握力", v.grip_strength, "kg", "≧ " + nrm.grip, v.grip_strength != null && Number(v.grip_strength) < nrm.grip)}
            ${vitalCard("五次坐站", (pickNum(v, ["chair_stand_time","chair_stand","sit_stand","chair_count"]) ?? parseMsgNum(a.message, ["五次坐站","坐站"])), "秒", "< " + nrm.chair, null)}
            ${vitalCard("走路時間", v.walking_time, "秒", "< " + nrm.walk, v.walking_time != null && Number(v.walking_time) >= nrm.walk)}
            ${vitalCard("除脂肪量", resolveFfm(a, v), "kg", nrm.ffmLow + "～" + nrm.ffmHigh, null)}
            ${vitalCard("血壓", (v.systolic && v.diastolic) ? (v.systolic + "/" + v.diastolic) : "-", "mmHg", nrm.sysLow + "-" + nrm.sysHigh + "/" + nrm.diaLow + "-" + nrm.diaHigh, v.systolic != null && (Number(v.systolic) < nrm.sysLow || Number(v.systolic) > nrm.sysHigh))}
            ${vitalCard("脈搏", v.pulse, "bpm", nrm.pulseLow + "-" + nrm.pulseHigh, v.pulse != null && (Number(v.pulse) < nrm.pulseLow || Number(v.pulse) > nrm.pulseHigh))}
            ${vitalCard("BMI", v.bmi, "", nrm.bmiLow + "～" + nrm.bmiHigh, v.bmi != null && (Number(v.bmi) < nrm.bmiLow || Number(v.bmi) >= nrm.bmiHigh))}
            ${vitalCard("身高/體重", (v.height || "-") + " / " + (v.weight || "-"), "cm/kg")}
          </div>
          <div class="small" style="white-space:pre-line">${a.message || ""}</div>
          ${renderZhenmaoAdvice(a)}
          ${a.handle_note ? `<div class="small text-success mt-2">處理備註：${a.handle_note}</div>` : ""}
        </div>
      </div>`;
    }).join("");
  } catch (e) {
    box.innerHTML = `<div class="alert alert-danger">${e.message}</div>`;
  }
}

function renderZhenmaoAdvice(a) {
  const v = a.vitals || {};
  const items = zhenmaoAdviceItems(a, v);
  if (!items.length) return "";
  const cards = items.map((it) => `
    <div class="col-12 col-md-6">
      <div class="border rounded p-2 h-100 bg-light">
        <div class="fw-semibold">${it.machine}</div>
        <div class="small text-muted">${it.why}</div>
        <div class="small mt-1">${it.how}</div>
      </div>
    </div>`).join("");
  return `
    <div class="mt-3 p-3 rounded" style="background:#f0f7f4;border:1px solid #c5ddd2">
      <div class="fw-semibold mb-1">真茂科技運動輔具建議</div>
      <div class="small text-muted mb-2">依本次異常項目對應館內器材。須有人員在旁、以輕負荷為主，有胸悶、暈眩或血壓明顯偏高時停止。</div>
      <div class="row g-2">${cards}</div>
    </div>`;
}

function zhenmaoAdviceItems(a, v) {
  const stage = a.sarcopenia_stage || "";
  const msg = (a.message || "") + (a.title || "");
  const pf = alertProfile(a, v);
  const nrm = ageSexNorm(pf.age, pf.gender);
  const gripLow = /握力/.test(msg) || (v.grip_strength != null && Number(v.grip_strength) < nrm.grip);
  const chairSlow = /坐站/.test(msg) || (v.chair_stand_time != null && Number(v.chair_stand_time) >= nrm.chair);
  const walkSlow = /走路|步速/.test(msg) || (v.walking_time != null && Number(v.walking_time) >= nrm.walk);
  const smiLow = /SMI/.test(msg) || (v.smi != null && Number(v.smi) < nrm.smi);
  const highBp = /血壓偏高|血壓異常/.test(msg) || (v.systolic != null && Number(v.systolic) >= 140);
  const severe = String(stage).includes("嚴重") || a.severity === "critical";
  const sets = severe ? "1 組 × 6～8 下" : "1～2 組 × 8～12 下";
  const pace = "節奏放慢、吐氣出力、不要憋氣。感覺還能再做 3 下再停。";
  const out = [];
  if (gripLow || smiLow || /肌少/.test(stage) || /上肢|握力/.test(msg)) {
    out.push({
      machine: "划船健身機",
      why: "改善上背與握力，對握力不足、肌少分期較有幫助。",
      how: `${sets}。雙手輕握把手、背部打直，往胸口方向拉。${pace}`,
    });
    out.push({
      machine: "擴胸蝴蝶機",
      why: "訓練胸肌與上肢推的力量，協助維持上半身肌量。",
      how: `${sets}。雙手打開再往中間合攏，肩頸放鬆。${pace}`,
    });
    out.push({
      machine: "上臂肩推機",
      why: "強化肩膀與上臂，日常舉手、拿物品較穩。",
      how: highBp
        ? "血壓偏高時先不做肩推，改用划船或蝴蝶機輕負荷。"
        : `${sets}。座椅調到手肘約與肩同高，向上推到快伸直即停。${pace}`,
    });
  }
  if (chairSlow || walkSlow || smiLow || /肌少/.test(stage)) {
    out.push({
      machine: "蹬腿機",
      why: "強化大腿與臀部，對坐站偏慢、步速偏慢最直接。",
      how: `${sets}。雙腳與肩同寬，膝蓋朝腳尖方向，不要完全鎖死。${pace}`,
    });
    out.push({
      machine: "屈伸腿機",
      why: "訓練大腿前側／後側，協助站起、上下階與走路穩定。",
      how: `${sets}。先做伸腿再做屈腿，活動到舒適角度即可。${pace}`,
    });
  }
  if (!out.length) {
    out.push({
      machine: "划船健身機 + 蹬腿機",
      why: "本次異常較輕，以上下肢各一項維持肌力即可。",
      how: `各 ${sets}。${pace}`,
    });
  }
  const seen = new Set();
  return out.filter((x) => (seen.has(x.machine) ? false : seen.add(x.machine)));
}

function vitalCard(label, value, unit, stdText, fail) {
  const empty = (value === undefined || value === null || value === "" || value === "-");
  const shown = empty ? "無資料" : value;
  let mark = "";
  let border = "";
  if (!empty && fail === true) {
    mark = '<span class="badge bg-danger ms-1">未達標</span>';
    border = "border-danger";
  } else if (!empty && fail === false) {
    mark = '<span class="badge bg-success ms-1">達標</span>';
  } else if (empty) {
    mark = '<span class="badge bg-secondary ms-1">無資料</span>';
  } else if (stdText) {
    mark = '<span class="badge bg-success ms-1">達標</span>';
  }
  if (!empty && label === "五次坐站" && Number(value) >= 12) {
    mark = '<span class="badge bg-danger ms-1">未達標</span>';
    border = "border-danger";
  }
  if (!empty && label === "除脂肪量") {
    const n = Number(value);
    const low = Number(String(stdText).split("～")[0]);
    const high = Number(String(stdText).split("～")[1]);
    if (low && n < low) {
      mark = '<span class="badge bg-danger ms-1">未達標</span>';
      border = "border-danger";
    }
  }
  return `<div class="col-6 col-md-3"><div class="metric-card ${border}"><div class="text-muted small">${label} ${mark}</div><div class="fw-semibold">${shown} <span class="small text-muted">${unit || ""}</span></div>${stdText ? `<div class="small text-muted">標準 ${stdText}</div>` : ""}</div></div>`;
}

async function handleAlert(id) {
  const note = prompt("處理說明（可留空）：", "已關懷個案並紀錄");
  if (note === null) return;
  try {
    await api(`/api/alerts/${id}/handle?note=${encodeURIComponent(note)}`, { method: "POST" });
    loadAlerts();
  } catch (e) {
    alert(e.message);
  }
}

async function pushAlertLine(id) {
  try {
    const r = await api(`/api/alerts/${id}/line`, { method: "POST" });
    alert(r.ok ? "已送出 LINE" : (r.reason || r.error || "尚未設定 LINE，僅產生預覽"));
  } catch (e) {
    alert(e.message);
  }
}

async function sendLineTest() {
  const box = document.getElementById("lineTestResult");
  if (box) box.innerHTML = '<div class="alert alert-info">傳送中...</div>';
  try {
    const r = await api("/api/alerts/line-test", { method: "POST" });
    const preview = (r.preview || "").replace(/</g, "&lt;");
    if (r.ok) {
      box.innerHTML = `<div class="alert alert-success">已送到 LINE。<pre class="mb-0 mt-2 small">${preview}</pre></div>`;
    } else {
      box.innerHTML = `<div class="alert alert-warning"><strong>目前是模擬預覽，還沒真正送到你的 LINE。</strong><br>${r.note || r.reason || ""}<pre class="mb-0 mt-2 small">${preview}</pre></div>`;
    }
  } catch (e) {
    if (box) box.innerHTML = `<div class="alert alert-danger">${e.message}</div>`;
  }
}


async function loadDuplicates() {
  const qEl = document.getElementById("dupQ");
  const q = qEl ? qEl.value.trim() : "";
  let url = "/api/duplicates?page=" + dupPage + "&page_size=50";
  if (q) url += "&q=" + encodeURIComponent(q);
  try {
    const data = await api(url);
    const tbody = document.getElementById("dupBody");
    if (!tbody) return;
    tbody.innerHTML = "";
    (data.items || []).forEach((r) => {
      const tr = document.createElement("tr");
      tr.innerHTML = "<td>" + (r.id_card||"") + "</td><td>" + (r.user_name||"") + "</td><td>" +
        (r.gender==="M"?"男":"女") + " / " + (r.age||"-") + "</td><td>" + (r.measure_time||r.measure_date||"-") +
        "</td><td>" + (r.grip_strength!=null?r.grip_strength:"-") + "</td><td>" +
        (r.chair_stand_time!=null?r.chair_stand_time:"-") + "</td><td>" +
        (r.walking_time!=null?r.walking_time:"-") + "</td><td>" + (r.smi!=null?r.smi:"-") +
        "</td><td>" + (r.sarcopenia_stage||"-") + "</td>";
      tbody.appendChild(tr);
    });
    const el = document.getElementById("dupTotal");
    if (el) el.textContent = "共 " + (data.total||0) + " 筆（同一天較舊的資料）";
  } catch (e) { alert(e.message); }
}

