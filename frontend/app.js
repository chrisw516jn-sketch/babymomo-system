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

function walkCutoff(age, male) {
  const a = age || 70;
  if (male) {
    if ([70, 71, 74, 75, 76, 80, 82, 88, 89].includes(a)) return 15;
    return 20;
  }
  if ([61, 62, 63, 67, 68, 69, 71, 73, 75, 78, 80, 81, 84, 88, 89].includes(a)) return 15;
  return 20;
}

function ageSexNorm(age, gender) {
  const male = gender === "M";
  return {
    grip: male ? 28 : 18,
    chair: 12,
    walk: walkCutoff(age, male),
    smi: male ? 7.0 : 5.7,
    bmiLow: 18.5,
    bmiHigh: 24,
    sysLow: 100,
    sysHigh: 120,
    diaLow: 60,
    diaHigh: 80,
    pulseLow: 60,
    pulseHigh: 100,
  };
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

    // Pie
    const pie = echarts.init(document.getElementById("pieChart"));
    pie.setOption({
      tooltip: { trigger: "item" },
      series: [{
        type: "pie",
        radius: ["40%", "70%"],
        data: s.sarcopenia_pie.map((d) => ({ name: d.name, value: d.value, itemStyle: { color: d.color } })),
        label: { formatter: "{b}: {c}" },
      }],
    });

    // Trend
    const trend = echarts.init(document.getElementById("trendChart"));
    const m = s.monthly_trends;
    trend.setOption({
      tooltip: { trigger: "axis" },
      legend: { data: ["檢測次數", "平均握力"] },
      xAxis: { type: "category", data: m.months },
      yAxis: [{ type: "value", name: "次數" }, { type: "value", name: "kg" }],
      series: [
        { name: "檢測次數", type: "bar", data: m.counts, itemStyle: { color: "#3b82f6" } },
        { name: "平均握力", type: "line", yAxisIndex: 1, data: m.avg_grip, itemStyle: { color: "#10b981" } },
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
      { label: "SMI", val: latest.smi, unit: "kg/m²", std: isMale ? 7.0 : 5.7, higherBetter: true },
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

    // history trend
    const hist = [...data.history].reverse();
    const chart = echarts.init(document.getElementById("caseTrendChart"));
    chart.setOption({
      tooltip: { trigger: "axis" },
      legend: { data: ["握力", "坐站", "走路"] },
      xAxis: { type: "category", data: hist.map((h) => h.measure_date) },
      yAxis: { type: "value" },
      series: [
        { name: "握力", type: "line", data: hist.map((h) => h.grip_strength) },
        { name: "坐站", type: "line", data: hist.map((h) => h.chair_stand_time) },
        { name: "走路", type: "line", data: hist.map((h) => h.walking_time) },
      ],
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
            ${vitalCard("五次坐站", v.chair_stand_time, "秒", "< " + nrm.chair, v.chair_stand_time != null && Number(v.chair_stand_time) >= nrm.chair)}
            ${vitalCard("走路時間", v.walking_time, "秒", "< " + nrm.walk, v.walking_time != null && Number(v.walking_time) >= nrm.walk)}
            ${vitalCard("SMI", v.smi, "", "≧ " + nrm.smi, v.smi != null && Number(v.smi) < nrm.smi)}
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
  const shown = (value === undefined || value === null || value === "") ? "-" : value;
  const border = fail ? "border-danger" : "";
  const mark = fail ? '<span class="badge bg-danger ms-1">未達標</span>' : (stdText ? '<span class="badge bg-success ms-1">達標</span>' : "");
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

