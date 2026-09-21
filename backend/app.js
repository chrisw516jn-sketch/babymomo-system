const API = "";
let token = localStorage.getItem("babymomo_token") || "";
let currentUser = null;
let currentPage = 1;
let currentCaseId = null;
let dupPage = 1;
const PAGE_SIZE = 30;
const VIEWS = ["dashboard", "cases", "records", "duplicates", "alerts", "import", "report", "users", "settings", "audit", "api", "feedback"];

async function api(path, options = {}) {
  const headers = options.headers || {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (!(options.body instanceof FormData) && options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(API + path, { ...options, headers });
  if (res.status === 401) {
    doLogout();
    throw new Error("登入已過期");
  }
  if (options.raw) return res;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || (typeof data.detail === "string" ? data.detail : res.statusText) || "請求失敗");
  return data;
}

async function downloadBlob(path, fallbackName) {
  const res = await api(path, { raw: true });
  if (!res.ok) {
    let msg = "下載失敗";
    try {
      const j = await res.json();
      msg = j.detail || msg;
    } catch (_) {}
    throw new Error(msg);
  }
  const blob = await res.blob();
  let fname = fallbackName;
  const cd = res.headers.get("Content-Disposition") || "";
  const m = cd.match(/filename="?([^";]+)"?/i);
  if (m) fname = m[1];
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = fname;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

function stageClass(stage) {
  if (!stage) return "bg-secondary";
  const s = String(stage);
  if (s.indexOf("嚴重") >= 0) return "bg-danger";
  if (s === "肌少症") return "bg-warning text-dark";
  if (s.indexOf("前期") >= 0) return "bg-info text-dark";
  if (s === "正常") return "bg-success";
  return "bg-secondary";
}

function stageBadge(stage) {
  const map = {
    "正常": "badge-normal",
    "肌少症前期": "badge-pre",
    "肌少症": "badge-sarco",
    "嚴重肌少症": "badge-severe",
  };
  const cls = map[stage] || stageClass(stage);
  return '<span class="badge ' + cls + '">' + (stage || "-") + "</span>";
}

// 避免舊快取或缺函式
window.stageClass = stageClass;
window.stageBadge = stageBadge;

function isAdmin() {
  return currentUser && ["superadmin", "admin", "company_admin"].includes(currentUser.role);
}

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
  document.querySelectorAll(".admin-only").forEach((el) => {
    el.style.display = isAdmin() ? "" : "none";
  });
  setRange(90);
  loadStats();
  loadAlertCount();
  showView("dashboard");
  if (!window._alertTimer) {
    window._alertTimer = setInterval(loadAlertCount, 60000);
  }
}

function showView(name) {
  VIEWS.forEach((v) => {
    const el = document.getElementById(`view-${v}`);
    if (el) el.style.display = v === name ? "block" : "none";
  });
  document.querySelectorAll(".sidebar .nav-link[data-view]").forEach((a) => {
    a.classList.toggle("active", a.getAttribute("data-view") === name);
  });
  if (name === "dashboard") {
    loadStats();
  }
  if (name === "records") loadRecords();
  if (name === "alerts") loadAlerts();
  if (name === "cases") loadCases();
  if (name === "users") loadUsers();
  if (name === "settings") {
    loadThresholds();
    loadEquipment();
  }
  if (name === "audit") loadAudit();
  if (name === "report") {
    const end = new Date();
    const start = new Date();
    start.setDate(end.getDate() - 30);
    document.getElementById("repStart").value = start.toISOString().slice(0, 10);
    document.getElementById("repEnd").value = end.toISOString().slice(0, 10);
  }
}

function setRange(days) {
  const end = new Date();
  const start = new Date();
  start.setDate(end.getDate() - days);
  document.getElementById("statStart").value = start.toISOString().slice(0, 10);
  document.getElementById("statEnd").value = end.toISOString().slice(0, 10);
  loadStats();
}

function setRangePreset(kind) {
  const end = new Date();
  const start = new Date();
  if (kind === "week") {
    const day = end.getDay() || 7;
    start.setDate(end.getDate() - day + 1);
  } else if (kind === "month") {
    start.setDate(1);
  } else if (kind === "year") {
    start.setMonth(0, 1);
  } else if (kind === "all") {
    document.getElementById("statStart").value = "";
    document.getElementById("statEnd").value = "";
    loadStats();
    return;
  }
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

    // --- 肌少症分期：橫向長條圖 ---
    const pieEl = document.getElementById("pieChart");
    const pie = echarts.getInstanceByDom(pieEl) || echarts.init(pieEl);
    const order = ["嚴重肌少症", "肌少症", "肌少症前期", "正常"];
    const colorMap = {
      "正常": "#cbd5e1",
      "肌少症前期": "#f5c542",
      "肌少症": "#f08a4b",
      "嚴重肌少症": "#e06b8a",
    };
    const rawPie = s.sarcopenia_pie || [];
    const cats = order.filter((n) => rawPie.some((d) => d.name === n) || true);
    const values = cats.map((n) => {
      const hit = rawPie.find((d) => d.name === n);
      return hit ? hit.value : 0;
    });
    const colors = cats.map((n) => colorMap[n] || "#94a3b8");
    pie.setOption({
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: (p) => `${p[0].name}：${p[0].value} 人`,
      },
      grid: { left: 88, right: 48, top: 16, bottom: 24 },
      xAxis: {
        type: "value",
        minInterval: 1,
        splitLine: { lineStyle: { color: "#f1f5f9" } },
        axisLabel: { color: "#94a3b8" },
      },
      yAxis: {
        type: "category",
        data: cats,
        inverse: false,
        axisTick: { show: false },
        axisLine: { show: false },
        axisLabel: { color: "#64748b", fontSize: 12 },
      },
      series: [{
        type: "bar",
        data: values.map((v, i) => ({
          value: v,
          itemStyle: { color: colors[i], borderRadius: [0, 8, 8, 0] },
        })),
        barWidth: 18,
        label: {
          show: true,
          position: "right",
          formatter: "{c} 人",
          color: "#64748b",
          fontSize: 12,
        },
      }],
    }, true);

    // --- 月度趨勢：檢測次數 + 平均握力雙長條 ---
    const trendEl = document.getElementById("trendChart");
    const trend = echarts.getInstanceByDom(trendEl) || echarts.init(trendEl);
    const m = s.monthly_trends || { months: [], counts: [], avg_grip: [], avg_chair: [], avg_walk: [] };
    const months = m.months || [];
    trend.setOption({
      color: ["#2bb8a8", "#8ea4f5"],
      tooltip: { trigger: "axis" },
      legend: {
        data: ["檢測次數", "平均握力"],
        top: 0,
        textStyle: { color: "#64748b", fontSize: 12 },
      },
      grid: { left: 44, right: 44, top: 40, bottom: 36 },
      xAxis: {
        type: "category",
        data: months,
        axisLine: { lineStyle: { color: "#e2e8f0" } },
        axisLabel: { color: "#94a3b8", fontSize: 11 },
        axisTick: { show: false },
      },
      yAxis: [
        {
          type: "value",
          name: "次數",
          nameTextStyle: { color: "#94a3b8", fontSize: 11 },
          splitLine: { lineStyle: { color: "#f1f5f9" } },
          axisLabel: { color: "#94a3b8" },
        },
        {
          type: "value",
          name: "kg",
          nameTextStyle: { color: "#94a3b8", fontSize: 11 },
          splitLine: { show: false },
          axisLabel: { color: "#94a3b8" },
        },
      ],
      series: [
        {
          name: "檢測次數",
          type: "bar",
          barMaxWidth: 26,
          data: m.counts || [],
          itemStyle: { color: "#2bb8a8", borderRadius: [4, 4, 0, 0] },
        },
        {
          name: "平均握力",
          type: "bar",
          yAxisIndex: 1,
          barMaxWidth: 26,
          data: m.avg_grip || [],
          itemStyle: { color: "#8ea4f5", borderRadius: [4, 4, 0, 0] },
        },
      ],
    }, true);

    window.addEventListener("resize", () => {
      pie.resize();
      trend.resize();
    });
  } catch (e) {
    document.getElementById("summaryText").textContent = "載入失敗：" + e.message;
  }
  loadReminders();
}

async function loadReminders() {
  const card = document.getElementById("reminderCard");
  const list = document.getElementById("reminderList");
  if (!card || !list) return;
  try {
    const data = await api("/api/reminders?days=14");
    if (!data.items || !data.items.length) {
      card.style.display = "none";
      return;
    }
    card.style.display = "block";
    document.getElementById("reminderOverdue").textContent = `${data.overdue_count || 0} 筆逾期`;
    list.innerHTML = data.items.slice(0, 30).map((r) => `
      <div class="d-flex justify-content-between align-items-center border-bottom py-1 px-1 small">
        <div>
          <a href="#" onclick="goCaseDetail('${r.id_card}'); return false;" class="fw-semibold text-decoration-none">${r.user_name || r.id_card}</a>
          ${stageBadge(r.stage)}
          <span class="text-muted ms-1">${r.content ? r.content.slice(0, 40) : ""}</span>
        </div>
        <div class="text-nowrap">
          <span class="badge ${r.overdue ? "bg-danger" : "bg-info text-dark"}">${r.next_follow_date || "-"}${r.overdue ? " 逾期" : ""}</span>
        </div>
      </div>
    `).join("");
  } catch (e) {
    card.style.display = "none";
  }
}

async function loadCases() {
  const q = document.getElementById("caseQ").value.trim();
  const stage = document.getElementById("caseStage").value;
  const need = document.getElementById("caseNeedCare").checked;
  let url = `/api/cases?page=1&page_size=100`;
  if (q) url += `&q=${encodeURIComponent(q)}`;
  if (stage) url += `&stage=${encodeURIComponent(stage)}`;
  if (need) url += `&need_care=true`;
  const box = document.getElementById("casesGrid");
  box.innerHTML = '<div class="col-12 text-muted">載入中...</div>';
  try {
    const data = await api(url);
    document.getElementById("casesTotal").textContent = `共 ${data.total} 位個案`;
    const chips = document.getElementById("quickCaseChips");
    if (chips) {
      const top = data.items.slice(0, 8);
      chips.innerHTML = top.length
        ? top.map((c) => {
            const mask = (c.id_card || "").length >= 6 ? c.id_card.slice(0,6)+"****" : (c.id_card||"");
            return `<button class="chip-btn" onclick="goCaseDetail('${c.id_card}')">${mask}（${c.user_name || ""} ${c.age || ""}歲）</button>`;
          }).join("") + `<button class="chip-btn" onclick="showView('cases')">篩選「肌少症前期」</button>`
        : '<span class="text-muted small">匯入資料後會出現個案按鈕</span>';
    }
    if (!data.items.length) {
      box.innerHTML = '<div class="col-12"><div class="alert alert-light border">尚無個案資料，請先匯入檢測紀錄。</div></div>';
      return;
    }
    box.innerHTML = data.items.map((c) => `
      <div class="col-md-6 col-lg-4">
        <div class="case-card p-3 ${c.need_care ? "need-care" : ""}" onclick="goCaseDetail('${c.id_card}')">
          <div class="d-flex justify-content-between align-items-start">
            <div>
              <strong>${c.user_name}</strong>
              <div class="small text-muted">${c.id_card} · ${c.gender === "M" ? "男" : "女"} · ${c.age || "-"}歲</div>
            </div>
            ${stageBadge(c.latest_stage)}
          </div>
          <div class="small mt-2">
            最近：${c.latest_time || "-"}<br>
            握力 ${c.grip_strength ?? "-"} · 坐站 ${c.chair_stand_time ?? "-"} · SMI ${c.smi ?? "-"}
          </div>
          <div class="small mt-1">
            共 ${c.total_records} 筆 · 異常 ${c.abnormal_times} 次
            ${c.open_alerts ? `<span class="badge bg-danger ms-1">未處理 ${c.open_alerts}</span>` : ""}
            ${c.need_care ? '<span class="badge bg-warning text-dark ms-1">需關懷</span>' : ""}
          </div>
        </div>
      </div>
    `).join("");
  } catch (e) {
    box.innerHTML = `<div class="col-12"><div class="alert alert-danger">${e.message}</div></div>`;
  }
}

function goCaseDetail(idCard) {
  showView("records");
  openCase(idCard);
}

async function loadRecords() {
  const q = document.getElementById("searchQ").value.trim();
  const start = document.getElementById("recStart").value;
  const end = document.getElementById("recEnd").value;
  const stage = document.getElementById("recStage").value;
  const gender = document.getElementById("recGender").value;
  const abnormal = document.getElementById("recAbnormalOnly").checked;
  let url = `/api/measurements?page=${currentPage}&page_size=${PAGE_SIZE}`;
  if (q) url += `&q=${encodeURIComponent(q)}`;
  if (start) url += `&start_date=${start}`;
  if (end) url += `&end_date=${end}`;
  if (stage) url += `&stage=${encodeURIComponent(stage)}`;
  if (gender) url += `&gender=${gender}`;
  if (abnormal) url += `&abnormal_only=true`;
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

async function loadDuplicates() {
  const qEl = document.getElementById("dupQ");
  const q = qEl ? qEl.value.trim() : "";
  let url = `/api/duplicates?page=${dupPage}&page_size=${PAGE_SIZE}`;
  if (q) url += `&q=${encodeURIComponent(q)}`;
  try {
    const data = await api(url);
    const tbody = document.getElementById("dupBody");
    if (!tbody) return;
    tbody.innerHTML = "";
    (data.items || []).forEach((r) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${r.id_card}</td>
        <td>${r.user_name}</td>
        <td>${r.gender === "M" ? "男" : "女"} / ${r.age || "-"}</td>
        <td>${r.measure_time || r.measure_date || "-"}</td>
        <td>${r.grip_strength != null ? r.grip_strength : "-"}</td>
        <td>${r.chair_stand_time != null ? r.chair_stand_time : "-"}</td>
        <td>${r.walking_time != null ? r.walking_time : "-"}</td>
        <td>${r.smi != null ? r.smi : "-"}</td>
        <td>${stageBadge(r.sarcopenia_stage)}</td>
      `;
      tbody.appendChild(tr);
    });
    const totalEl = document.getElementById("dupTotal");
    if (totalEl) totalEl.textContent = `共 ${data.total} 筆（同一天較舊的資料）`;
    const badge = document.getElementById("dupBadge");
    if (badge) {
      badge.textContent = data.total || 0;
      badge.style.display = data.total ? "inline" : "none";
    }
  } catch (e) {
    alert(e.message);
  }
}

function changeDupPage(delta) {
  dupPage = Math.max(1, dupPage + delta);
  loadDuplicates();
}

let currentSuggestedRetest = null;

async function openCase(idCard) {
  currentCaseId = idCard;
  try {
    const data = await api(`/api/cases/${encodeURIComponent(idCard)}`);
    const p = data.profile;
    const latest = data.latest;
    document.getElementById("casePanel").style.display = "block";
    const masked = (p.id_card || "").length >= 6 ? (p.id_card.slice(0,6) + "****") : (p.id_card || "");
    const av = document.getElementById("caseAvatar");
    if (av) av.textContent = (p.user_name || "長").slice(0,1);
    document.getElementById("caseName").innerHTML = `${p.user_name} <span class="text-primary fs-6">${masked}</span> ${stageBadge(latest.sarcopenia_stage)}`;
    document.getElementById("caseMeta").innerHTML =
      `${p.gender === "M" ? "男性" : "女性"} · ${p.age || "-"} 歲 · 身高 ${p.height || "-"} cm · 體重 ${p.weight || "-"} kg · BMI: ${p.bmi || "-"} · <span class="text-primary">最新檢測：${latest.measure_time || "-"}</span>`;
    const cnt = document.getElementById("caseCountBadge");
    if (cnt) cnt.textContent = `累計量測紀錄：${data.total_records || 0} 次`;
    const tag = document.getElementById("caseChartTag");
    if (tag) tag.textContent = `個案：${p.user_name}（${masked}）`;
    document.getElementById("btnPrintReport").onclick = () => {
      window.open(`/api/cases/${encodeURIComponent(idCard)}/report`, "_blank");
    };

    const isMale = p.gender === "M";
    const sex = isMale ? "男" : "女";
    const metrics = [
      { icon:"💪", label: "握力測量", val: latest.grip_strength, unit: "kg", hint: `${sex} ≥ ${isMale ? 28 : 18} kg`, std: isMale ? 28 : 18, higherBetter: true },
      { icon:"↑", label: "五次坐站", val: latest.chair_stand_time, unit: "秒", hint: "標準值：< 12 秒", std: 12, higherBetter: false },
      { icon:"🚶", label: "走路時間", val: latest.walking_time, unit: "秒", hint: "標準值：< 20 秒", std: 20, higherBetter: false },
      { icon:"🦴", label: "骨骼肌量 SMI", val: latest.smi, unit: "kg/m²", hint: `${sex} ≥ ${isMale ? 7.0 : 5.7} kg/m²`, std: isMale ? 7.0 : 5.7, higherBetter: true },
      { icon:"❤", label: "血壓（收縮/舒張）", val: latest.systolic ? `${latest.systolic} / ${latest.diastolic || "-"}` : null, unit: "mmHg", hint: "標準: 120/80 mmHg", note: latest.systolic && latest.systolic < 140 ? "血壓正常平穩" : (latest.systolic ? "血壓偏高" : "") },
      { icon:"♡", label: "心率脈搏", val: latest.pulse, unit: "bpm", hint: "安靜心率: 60~100 bpm", note: latest.pulse && latest.pulse >= 60 && latest.pulse <= 100 ? "正常安靜心率" : "" },
      { icon:"●", label: "體脂率", val: latest.body_fat, unit: "%", hint: isMale ? "男 10~20%" : "女 23~36.9%" },
      { icon:"▢", label: "體位 BMI", val: latest.bmi, unit: "", hint: "衛福部: 18.5 ~ 24.0", note: latest.bmi && latest.bmi >= 18.5 && latest.bmi < 24 ? "標準體位" : "" },
    ];
    const box = document.getElementById("caseMetrics");
    box.innerHTML = metrics.map((m) => {
      let note = m.note || "";
      if (m.std != null && m.val != null && !note) {
        const pass = m.higherBetter ? Number(m.val) >= m.std : Number(m.val) < m.std;
        note = pass ? "達標" : "未達標";
      }
      const noteHtml = note ? `<div class="small ${String(note).includes("未")||String(note).includes("偏高") ? "text-danger" : "text-success"}">${note}</div>` : "";
      return `<div class="col-6 col-md-3"><div class="photo-metric">
        <div class="d-flex justify-content-between small text-muted"><span>${m.icon} ${m.label}</span><span>${m.hint || ""}</span></div>
        <div class="val">${m.val != null ? m.val : "-"} <small class="fs-6 text-muted fw-normal">${m.unit || ""}</small></div>
        ${noteHtml}
      </div></div>`;
    }).join("");

    
    const hist = [...data.history].reverse();
    const caseEl = document.getElementById("caseTrendChart");
    const chart = echarts.getInstanceByDom(caseEl) || echarts.init(caseEl);
    chart.setOption({
      color: ["#3b82f6", "#f59e0b", "#8b5cf6", "#10b981"],
      tooltip: {
        trigger: "axis",
        backgroundColor: "rgba(15,23,42,.92)",
        borderWidth: 0,
        textStyle: { color: "#fff", fontSize: 12 },
      },
      legend: {
        data: ["握力 kg", "坐站 秒", "走路 秒", "SMI"],
        top: 0,
        textStyle: { fontSize: 12, color: "#475569" },
      },
      grid: { left: 44, right: 20, top: 36, bottom: 28 },
      xAxis: {
        type: "category",
        data: hist.map((h) => h.measure_date),
        axisLine: { lineStyle: { color: "#e2e8f0" } },
        axisLabel: { color: "#64748b", fontSize: 11 },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value",
        splitLine: { lineStyle: { color: "#f1f5f9", type: "dashed" } },
        axisLabel: { color: "#64748b" },
      },
      series: [
        {
          name: "握力 kg", type: "line", smooth: true, symbol: "circle", symbolSize: 8,
          data: hist.map((h) => h.grip_strength),
          lineStyle: { width: 3 },
          areaStyle: { color: "rgba(59,130,246,.12)" },
        },
        {
          name: "坐站 秒", type: "line", smooth: true, symbol: "circle", symbolSize: 7,
          data: hist.map((h) => h.chair_stand_time),
          lineStyle: { width: 2 },
        },
        {
          name: "走路 秒", type: "line", smooth: true, symbol: "circle", symbolSize: 7,
          data: hist.map((h) => h.walking_time),
          lineStyle: { width: 2 },
        },
        {
          name: "SMI", type: "line", smooth: true, symbol: "diamond", symbolSize: 8,
          data: hist.map((h) => h.smi),
          lineStyle: { width: 2 },
        },
      ],
    }, true);

    // 運動／復健建議
    const adv = data.exercise_advice;
    currentSuggestedRetest = data.suggested_retest_date || null;
    const advBox = document.getElementById("exerciseAdviceBox");
    if (adv && advBox) {
      advBox.style.display = "block";
      document.getElementById("exerciseTitle").textContent = adv.title || "運動建議";
      document.getElementById("exerciseSummary").textContent = adv.summary || "";
      document.getElementById("exerciseItems").innerHTML = (adv.items || []).map((t) => `<li>${t}</li>`).join("");
      document.getElementById("exerciseRetest").textContent = currentSuggestedRetest || "-";
      // 若關懷下次日期空白，自動帶入建議日
      const nextInp = document.getElementById("careNextDate");
      if (nextInp && !nextInp.value && currentSuggestedRetest) {
        nextInp.value = currentSuggestedRetest;
      }
    } else if (advBox) {
      advBox.style.display = "none";
    }

    const notes = data.care_notes || [];
    document.getElementById("careNotesList").innerHTML = notes.length
      ? notes.map((n) => `
        <div class="border-bottom py-1">
          <span class="text-muted">${n.created_at ? n.created_at.replace("T", " ").slice(0, 16) : ""} · ${n.created_by || ""}</span>
          ${n.next_follow_date ? `<span class="badge bg-info text-dark ms-1">下次 ${n.next_follow_date}</span>` : ""}
          <div>${n.content}</div>
        </div>
      `).join("")
      : '<div class="text-muted">尚無關懷紀錄</div>';
  } catch (e) {
    alert(e.message);
  }
}

function fillSuggestedRetest() {
  if (currentSuggestedRetest) {
    document.getElementById("careNextDate").value = currentSuggestedRetest;
  }
}

async function downloadCasePdf() {
  if (!currentCaseId) return;
  try {
    await downloadBlob(
      `/api/cases/${encodeURIComponent(currentCaseId)}/report.pdf`,
      `babymomo_${currentCaseId}.pdf`
    );
  } catch (e) {
    alert(e.message);
  }
}

async function downloadBackup() {
  const msg = document.getElementById("backupMsg");
  try {
    if (msg) msg.textContent = "準備下載…";
    await downloadBlob("/api/admin/backup", "babymomo_backup.db");
    if (msg) msg.innerHTML = '<span class="text-success">備份已開始下載</span>';
  } catch (e) {
    if (msg) msg.innerHTML = `<span class="text-danger">${e.message}</span>`;
    else alert(e.message);
  }
}

function closeCase() {
  document.getElementById("casePanel").style.display = "none";
  currentCaseId = null;
}

function printCaseReport() {
  if (!currentCaseId) return;
  window.open(`/api/cases/${encodeURIComponent(currentCaseId)}/report`, "_blank");
}

async function addCareNote() {
  if (!currentCaseId) return;
  const content = document.getElementById("careNoteInput").value.trim();
  if (!content) {
    alert("請輸入關懷內容");
    return;
  }
  const next = document.getElementById("careNextDate").value || null;
  try {
    await api("/api/care-notes", {
      method: "POST",
      body: JSON.stringify({
        id_card: currentCaseId,
        content,
        note_type: "followup",
        next_follow_date: next,
      }),
    });
    document.getElementById("careNoteInput").value = "";
    openCase(currentCaseId);
  } catch (e) {
    alert(e.message);
  }
}

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
        成功 <strong>${data.success}</strong> · 略過 <strong>${data.skipped}</strong> ·
        自動刪除重複 <strong>${data.deleted || 0}</strong> · 失敗 <strong>${data.failed}</strong>
      </div>
      ${data.messages.length ? `<ul class="small text-danger">${data.messages.map((m) => `<li>${m}</li>`).join("")}</ul>` : ""}
    `;
    loadStats();
    loadAlertCount();
  } catch (e) {
    box.innerHTML = `<div class="alert alert-danger">${e.message}</div>`;
  }
}

async function doDedupe() {
  if (!confirm("將掃描並刪除重複檢測紀錄（每組只留最早一筆）。確定嗎？")) return;
  const box = document.getElementById("importResult");
  box.innerHTML = '<div class="alert alert-info">審核中...</div>';
  try {
    const data = await api("/api/dedupe", { method: "POST" });
    box.innerHTML = `<div class="alert alert-success">已刪除重複 <strong>${data.deleted || 0}</strong> 筆</div>`;
    loadStats();
  } catch (e) {
    box.innerHTML = `<div class="alert alert-danger">${e.message}</div>`;
  }
}

function exportCSV() {
  const q = document.getElementById("searchQ").value.trim();
  const start = document.getElementById("recStart").value;
  const end = document.getElementById("recEnd").value;
  let url = API + "/api/export/csv?";
  if (q) url += `q=${encodeURIComponent(q)}&`;
  if (start) url += `start_date=${start}&`;
  if (end) url += `end_date=${end}&`;
  fetch(url, { headers: { Authorization: `Bearer ${token}` } })
    .then((r) => r.blob())
    .then((blob) => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "babymomo_export.csv";
      a.click();
    });
}

async function loadPeriodReport() {
  const start = document.getElementById("repStart").value;
  const end = document.getElementById("repEnd").value;
  let url = "/api/report/period?";
  if (start) url += `start_date=${start}&`;
  if (end) url += `end_date=${end}&`;
  const box = document.getElementById("periodReportBox");
  try {
    const r = await api(url);
    const stages = r.stage_counts || {};
    box.innerHTML = `
      <h6 class="fw-bold">期間報表摘要</h6>
      <p class="mb-1">區間：${r.start_date || "不限"} ～ ${r.end_date || "不限"}</p>
      <p class="mb-1">檢測總筆數：<strong>${r.total_records}</strong></p>
      <p class="mb-1">列冊人數：<strong>${r.unique_users}</strong></p>
      <p class="mb-1">有異常人數：<strong class="text-danger">${r.abnormal_users}</strong></p>
      <p class="mb-1">分期分布：
        ${Object.keys(stages).map((k) => `${k} ${stages[k]}`).join("　") || "無"}
      </p>
      <p class="small text-muted mb-0">產生時間：${r.generated_at} · 操作者：${r.operator}</p>
    `;
  } catch (e) {
    box.innerHTML = `<div class="text-danger">${e.message}</div>`;
  }
}

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
  } catch (e) { /* ignore */ }
}

async function loadAlerts() {
  const onlyUnhandled = document.getElementById("alertOnlyUnhandled")?.checked;
  const aq = document.getElementById("alertQ")?.value?.trim() || "";
  const aStart = document.getElementById("alertStart")?.value || "";
  const aEnd = document.getElementById("alertEnd")?.value || "";
  const aStage = document.getElementById("alertStage")?.value || "";
  let url = "/api/alerts?page_size=100";
  if (onlyUnhandled) url += "&only_unhandled=true";
  if (aq) url += `&q=${encodeURIComponent(aq)}`;
  if (aStart) url += `&start_date=${encodeURIComponent(aStart)}`;
  if (aEnd) url += `&end_date=${encodeURIComponent(aEnd)}`;
  if (aStage) url += `&stage=${encodeURIComponent(aStage)}`;
  const box = document.getElementById("alertsList");
  if (!box) return;
  box.innerHTML = '<div class="text-muted">載入中...</div>';
  try {
    const data = await api(url);
    loadAlertCount();
    if (!data.items || data.items.length === 0) {
      box.innerHTML = '<div class="alert alert-success">目前沒有異常通報。</div>';
      return;
    }
    box.innerHTML = data.items.map((a) => {
      const sevClass = a.severity === "critical" ? "border-danger" : a.severity === "warning" ? "border-warning" : "border-info";
      const sevBadge = a.severity === "critical" ? "bg-danger" : a.severity === "warning" ? "bg-warning text-dark" : "bg-info";
      const v = a.vitals || {};
      const g = (v.gender || "").toUpperCase();
      const gripFail = v.grip_strength != null && ((g === "M" || g === "男") ? v.grip_strength < 28 : (g === "F" || g === "女") ? v.grip_strength < 18 : false);
      const chairFail = v.chair_stand_time != null && v.chair_stand_time > 12;
      const walkFail = v.walking_time != null && v.walking_time > 18;
      const smiFail = v.smi != null && ((g === "M" || g === "男") ? v.smi < 7.0 : (g === "F" || g === "女") ? v.smi < 5.7 : false);
      const bpFail = v.bp_status && v.bp_status !== "血壓正常" && v.bp_status !== "未量測";
      const handled = a.is_handled
        ? `<span class="badge bg-success">已處理 by ${a.handled_by || "-"}</span>`
        : `<button class="btn btn-sm btn-primary" onclick="handleAlert(${a.id})">標記已關懷</button>`;
      return `<div class="card mb-3 ${sevClass}" style="border-left-width:5px">
        <div class="card-body">
          <div class="d-flex justify-content-between align-items-start gap-2 flex-wrap mb-2">
            <div>
              ${!a.is_handled ? `<input type="checkbox" class="form-check-input me-2 alert-check" value="${a.id}" />` : ""}
              <span class="badge ${sevBadge} me-1">${a.severity === "critical" ? "緊急" : a.severity === "warning" ? "注意" : "提醒"}</span>
              <span class="badge ${stageClass(a.sarcopenia_stage)}">${a.sarcopenia_stage || "-"}</span>
              <strong class="ms-1">${a.user_name}</strong>
              <span class="text-muted small">（${a.id_card}）</span>
              <div class="small text-muted mt-1">${a.created_at ? a.created_at.replace("T", " ").slice(0, 19) : ""} · 異常 ${a.abnormal_count || 0} 項</div>
            </div>
            <div class="d-flex gap-2 flex-wrap">
              <button class="btn btn-sm btn-outline-secondary" onclick="goCaseDetail('${a.id_card}')">看個案</button>
              <button class="btn btn-sm btn-outline-success" onclick="pushAlertLine(${a.id})"><i class="bi bi-line"></i> 傳 LINE</button>
              ${handled}
            </div>
          </div>
          <div class="row g-2 mb-2">
            ${vitalCard("握力", v.grip_strength, "kg", gripFail)}
            ${vitalCard("五次坐站", v.chair_stand_time, "秒", chairFail)}
            ${vitalCard("走路時間", v.walking_time, "秒", walkFail)}
            ${vitalCard("SMI", v.smi, "", smiFail)}
            ${vitalCard("血壓", (v.systolic != null && v.diastolic != null) ? (v.systolic + "/" + v.diastolic) : "-", "mmHg", bpFail)}
            ${vitalCard("脈搏", v.pulse, "bpm", false)}
          </div>
          <div class="bg-light rounded p-2 small" style="white-space:pre-line">${a.message || ""}</div>
          ${a.handle_note ? `<div class="small text-success mt-2">處理備註：${a.handle_note}</div>` : ""}
          ${renderInterventionPlan(a.intervention_plan)}
        </div>
      </div>`;
    }).join("");
  } catch (e) {
    box.innerHTML = `<div class="alert alert-danger">${e.message}</div>`;
  }
}


function renderInterventionPlan(plan) {
  if (!plan) return "";
  const immediate = (plan.immediate || []).map((t) => `<li>${t}</li>`).join("");
  const equipment = (plan.equipment || []).map((eq) => `
    <div class="border rounded p-2 mb-2 bg-white">
      <div class="fw-semibold text-primary"><i class="bi bi-bicycle me-1"></i>${eq.name || ""}</div>
      <div class="small mt-1"><span class="text-muted">為什麼：</span>${eq.why || ""}</div>
      <div class="small"><span class="text-muted">怎麼用：</span>${eq.how || ""}</div>
      <div class="small text-danger"><span class="text-muted">注意：</span>${eq.caution || ""}</div>
    </div>
  `).join("");
  const protocol = (plan.protocol || []).map((t) => `<li>${t}</li>`).join("");
  const care = (plan.care || []).map((t) => `<li>${t}</li>`).join("");
  return `
    <div class="mt-3 p-3 rounded border border-success-subtle" style="background:#f0fdf4">
      <div class="fw-bold text-success mb-1"><i class="bi bi-clipboard2-pulse me-1"></i>${plan.title || "應對方案"}</div>
      <div class="small text-muted mb-2">${plan.brand_note || ""}</div>
      <div class="small fw-semibold">立即處置</div>
      <ul class="small mb-2">${immediate}</ul>
      <div class="small fw-semibold">真茂科技運動輔具建議</div>
      ${equipment}
      <div class="small fw-semibold mt-2">訓練原則</div>
      <ul class="small mb-2">${protocol}</ul>
      <div class="small fw-semibold">關懷追蹤</div>
      <ul class="small mb-0">${care}</ul>
    </div>
  `;
}

function vitalCard(label, value, unit, fail) {
  const shown = (value === undefined || value === null || value === "") ? "-" : value;
  const cls = fail ? "metric-card fail" : "metric-card";
  const lab = fail ? label + " ⚠️" : label;
  return `<div class="col-6 col-md-3"><div class="${cls}"><div class="text-muted small">${lab}</div><div class="fw-semibold ${fail ? "text-danger" : ""}">${shown} <span class="small text-muted">${unit || ""}</span></div></div></div>`;
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

async function batchHandleSelected() {
  const ids = [...document.querySelectorAll(".alert-check:checked")].map((el) => parseInt(el.value, 10));
  if (!ids.length) {
    alert("請先勾選要處理的通報");
    return;
  }
  const note = prompt("批次處理說明：", "批次標記已關懷") || "批次標記已關懷";
  try {
    const r = await api("/api/alerts/batch-handle", {
      method: "POST",
      body: JSON.stringify({ ids, note }),
    });
    alert(`已處理 ${r.handled} 筆`);
    loadAlerts();
  } catch (e) {
    alert(e.message);
  }
}

async function pushAlertLine(id) {
  try {
    const r = await api(`/api/alerts/${id}/line`, { method: "POST" });
    alert(r.ok ? "已送出 LINE" : (r.reason || r.error || "送出失敗"));
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
      box.innerHTML = `<div class="alert alert-warning">模擬預覽<br>${r.reason || ""}<pre class="mb-0 mt-2 small">${preview}</pre></div>`;
    }
  } catch (e) {
    if (box) box.innerHTML = `<div class="alert alert-danger">${e.message}</div>`;
  }
}

async function sendDailySummary() {
  try {
    const r = await api("/api/alerts/daily-summary", { method: "POST" });
    alert(r.ok ? "每日摘要已送到 LINE" : (r.reason || "送出失敗"));
  } catch (e) {
    alert(e.message);
  }
}

function showCreateUser() {
  const box = document.getElementById("createUserBox");
  box.style.display = box.style.display === "none" ? "block" : "none";
}

async function loadUsers() {
  try {
    const users = await api("/api/users");
    document.getElementById("usersBody").innerHTML = users.map((u) => `
      <tr>
        <td>${u.username}</td>
        <td>${u.display_name}</td>
        <td>${u.role}</td>
        <td>${u.is_active ? '<span class="badge bg-success">啟用</span>' : '<span class="badge bg-secondary">停用</span>'}</td>
        <td>
          <button class="btn btn-sm btn-outline-secondary" onclick="toggleUser(${u.id}, ${u.is_active})">${u.is_active ? "停用" : "啟用"}</button>
          <button class="btn btn-sm btn-outline-primary" onclick="resetUserPwd(${u.id}, '${u.username}')">重設密碼</button>
        </td>
      </tr>
    `).join("");
  } catch (e) {
    document.getElementById("usersBody").innerHTML = `<tr><td colspan="5">${e.message}</td></tr>`;
  }
}

async function createUser() {
  const username = document.getElementById("nuUser").value.trim();
  const password = document.getElementById("nuPass").value;
  const display_name = document.getElementById("nuName").value.trim() || username;
  const role = document.getElementById("nuRole").value;
  try {
    await api("/api/users", {
      method: "POST",
      body: JSON.stringify({ username, password, display_name, role }),
    });
    alert("建立成功");
    loadUsers();
  } catch (e) {
    alert(e.message);
  }
}

async function toggleUser(id, active) {
  try {
    await api(`/api/users/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ is_active: !active }),
    });
    loadUsers();
  } catch (e) {
    alert(e.message);
  }
}

async function resetUserPwd(id, username) {
  const pwd = prompt(`重設 ${username} 的密碼：`);
  if (!pwd) return;
  try {
    await api(`/api/users/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ password: pwd }),
    });
    alert("密碼已更新");
  } catch (e) {
    alert(e.message);
  }
}

async function loadThresholds() {
  try {
    const t = await api("/api/settings/thresholds");
    const fields = [
      ["grip_male", "男性握力下限 (kg)"],
      ["grip_female", "女性握力下限 (kg)"],
      ["smi_male", "男性 SMI 下限"],
      ["smi_female", "女性 SMI 下限"],
      ["chair_stand", "五次坐站上限 (秒)"],
      ["walking_time", "走路時間上限 (秒)"],
      ["systolic_high", "收縮壓偏高 (mmHg)"],
      ["diastolic_high", "舒張壓偏高 (mmHg)"],
    ];
    document.getElementById("thresholdForm").innerHTML = fields.map(([k, label]) => `
      <div class="col-md-6">
        <label class="form-label small">${label}</label>
        <input type="number" step="0.1" class="form-control form-control-sm" id="th_${k}" value="${t[k]}" />
      </div>
    `).join("");
  } catch (e) {
    document.getElementById("thresholdMsg").innerHTML = `<div class="text-danger">${e.message}</div>`;
  }
}

async function saveThresholds() {
  const keys = ["grip_male", "grip_female", "smi_male", "smi_female", "chair_stand", "walking_time", "systolic_high", "diastolic_high"];
  const body = {};
  keys.forEach((k) => {
    body[k] = parseFloat(document.getElementById("th_" + k).value);
  });
  try {
    await api("/api/settings/thresholds", { method: "PUT", body: JSON.stringify(body) });
    document.getElementById("thresholdMsg").innerHTML = '<div class="text-success">已儲存</div>';
  } catch (e) {
    document.getElementById("thresholdMsg").innerHTML = `<div class="text-danger">${e.message}</div>`;
  }
}

let _eqItems = [];

function _eqCardHtml(it, idx) {
  const triggers = (it.triggers || []).join(",");
  return `
    <div class="border rounded p-3 bg-white" data-eq-idx="${idx}">
      <div class="d-flex justify-content-between align-items-center mb-2">
        <strong class="text-primary">#${idx + 1}</strong>
        <button type="button" class="btn btn-sm btn-outline-danger" onclick="removeEquipmentRow(${idx})">刪除</button>
      </div>
      <div class="row g-2">
        <div class="col-md-4">
          <label class="form-label small">名稱</label>
          <input class="form-control form-control-sm eq-name" value="${(it.name || "").replace(/"/g, "&quot;")}">
        </div>
        <div class="col-md-4">
          <label class="form-label small">觸發條件（逗號分隔）</label>
          <input class="form-control form-control-sm eq-triggers" value="${triggers.replace(/"/g, "&quot;")}" placeholder="grip,chair,stage">
        </div>
        <div class="col-md-4">
          <label class="form-label small">ID（選填）</label>
          <input class="form-control form-control-sm eq-id" value="${(it.id || "").replace(/"/g, "&quot;")}">
        </div>
        <div class="col-12">
          <label class="form-label small">為什麼</label>
          <textarea class="form-control form-control-sm eq-why" rows="2">${it.why || ""}</textarea>
        </div>
        <div class="col-12">
          <label class="form-label small">怎麼用</label>
          <textarea class="form-control form-control-sm eq-how" rows="2">${it.how || ""}</textarea>
        </div>
        <div class="col-12">
          <label class="form-label small">注意</label>
          <textarea class="form-control form-control-sm eq-caution" rows="2">${it.caution || ""}</textarea>
        </div>
      </div>
    </div>`;
}

function renderEquipmentEditor() {
  const box = document.getElementById("equipmentEditor");
  if (!box) return;
  box.innerHTML = _eqItems.map((it, i) => _eqCardHtml(it, i)).join("") ||
    '<div class="text-muted">尚無項目，請按「新增一筆」</div>';
}

async function loadEquipment() {
  const msg = document.getElementById("equipmentMsg");
  try {
    const data = await api("/api/settings/equipment");
    _eqItems = data.items || [];
    renderEquipmentEditor();
    if (msg) msg.textContent = "";
  } catch (e) {
    if (msg) msg.innerHTML = `<span class="text-danger">${e.message}</span>`;
  }
}

function _collectEquipmentFromDom() {
  const cards = document.querySelectorAll("#equipmentEditor [data-eq-idx]");
  const items = [];
  cards.forEach((card) => {
    const name = card.querySelector(".eq-name")?.value?.trim() || "";
    if (!name) return;
    const triggersRaw = card.querySelector(".eq-triggers")?.value || "";
    const triggers = triggersRaw.split(",").map((s) => s.trim()).filter(Boolean);
    items.push({
      id: card.querySelector(".eq-id")?.value?.trim() || `eq_${items.length + 1}`,
      name,
      why: card.querySelector(".eq-why")?.value || "",
      how: card.querySelector(".eq-how")?.value || "",
      caution: card.querySelector(".eq-caution")?.value || "",
      triggers: triggers.length ? triggers : ["always_abnormal"],
    });
  });
  return items;
}

function addEquipmentRow() {
  _eqItems = _collectEquipmentFromDom();
  _eqItems.push({
    id: `eq_${Date.now()}`,
    name: "新輔具",
    why: "",
    how: "",
    caution: "",
    triggers: ["always_abnormal"],
  });
  renderEquipmentEditor();
}

function removeEquipmentRow(idx) {
  _eqItems = _collectEquipmentFromDom();
  _eqItems.splice(idx, 1);
  renderEquipmentEditor();
}

async function saveEquipment() {
  const msg = document.getElementById("equipmentMsg");
  const items = _collectEquipmentFromDom();
  if (!items.length) {
    if (msg) msg.innerHTML = '<span class="text-danger">請至少保留一筆</span>';
    return;
  }
  try {
    const data = await api("/api/settings/equipment", {
      method: "PUT",
      body: JSON.stringify({ items }),
    });
    _eqItems = data.items || items;
    renderEquipmentEditor();
    if (msg) msg.innerHTML = '<span class="text-success">已儲存，異常通報會套用最新內容</span>';
  } catch (e) {
    if (msg) msg.innerHTML = `<span class="text-danger">${e.message}</span>`;
  }
}

async function resetEquipment() {
  if (!confirm("確定還原為系統預設輔具建議？")) return;
  const msg = document.getElementById("equipmentMsg");
  try {
    const data = await api("/api/settings/equipment/reset", { method: "POST", body: "{}" });
    _eqItems = data.items || [];
    renderEquipmentEditor();
    if (msg) msg.innerHTML = '<span class="text-success">已還原預設</span>';
  } catch (e) {
    if (msg) msg.innerHTML = `<span class="text-danger">${e.message}</span>`;
  }
}

async function loadAudit() {
  const q = document.getElementById("auditQ").value.trim();
  let url = "/api/audit-logs?page_size=100";
  if (q) url += `&q=${encodeURIComponent(q)}`;
  try {
    const data = await api(url);
    document.getElementById("auditBody").innerHTML = (data.items || []).map((a) => `
      <tr>
        <td class="small">${a.created_at ? a.created_at.replace("T", " ").slice(0, 19) : "-"}</td>
        <td>${a.operator || "-"}</td>
        <td>${a.action || "-"}</td>
        <td class="small">${a.details || ""}</td>
      </tr>
    `).join("") || '<tr><td colspan="4">無資料</td></tr>';
  } catch (e) {
    document.getElementById("auditBody").innerHTML = `<tr><td colspan="4">${e.message}</td></tr>`;
  }
}

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

document.getElementById("loginPassword")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") doLogin();
});

async function submitFeedback() {
  const catEl = document.querySelector('input[name="fbCat"]:checked');
  const category = catEl ? catEl.value : "suggestion";
  const title = (document.getElementById("fbTitle")?.value || "").trim();
  const content = (document.getElementById("fbContent")?.value || "").trim();
  const contact = (document.getElementById("fbContact")?.value || "").trim();
  const statusEl = document.getElementById("fbStatus");
  const btn = document.getElementById("fbSubmitBtn");
  if (!content || content.length < 3) {
    if (statusEl) statusEl.textContent = "請至少填寫 3 個字的內容";
    return;
  }
  if (btn) btn.disabled = true;
  if (statusEl) statusEl.textContent = "傳送中…";
  try {
    const res = await api("/api/feedback", {
      method: "POST",
      body: JSON.stringify({
        category,
        title: title || null,
        content,
        contact: contact || null,
        page_url: location.href,
      }),
    });
    if (statusEl) {
      statusEl.textContent = res.message || "已送出";
      statusEl.className = res.line_sent ? "small text-success" : "small text-warning";
    }
    if (document.getElementById("fbContent")) document.getElementById("fbContent").value = "";
    if (document.getElementById("fbTitle")) document.getElementById("fbTitle").value = "";
  } catch (e) {
    if (statusEl) {
      statusEl.textContent = e.message || "送出失敗";
      statusEl.className = "small text-danger";
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}
