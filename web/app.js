/* 화면 동작 — 프레임워크 없이 바닐라 JS.
 *
 * 서버가 무거운 일(수집·판정·초안)을 다 하므로 여기서는 요청을 보내고 결과를 그리기만
 * 한다. API 키는 이 파일에 절대 들어오지 않는다 — 서버가 대신 부른다.
 */

const $ = (id) => document.getElementById(id);

// 지금 보고 있는 공고. 탭을 옮겨 다녀도 이 값이 기준이 된다.
let current = null;
let currentDraft = null;

// ─────────────────────────────────────────────────────────────── 공통

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `요청 실패 (${res.status})`);
  return data;
}

async function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

// 공고 ID에는 콜론과 한글이 들어 있어 반드시 인코딩해서 보내야 한다.
const noticeUrl = (id, tail) =>
  `/api/notices/${encodeURIComponent(id)}${tail ? "/" + tail : ""}`;

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

/* 공고 첨부파일 목록을 그린다 (기업마당만 제공).
 *
 * download 속성은 일부러 쓰지 않는다. 기업마당이 Content-Disposition: attachment 로
 * 내려주므로 브라우저가 알아서 저장하고, 파일명도 서버가 준 것을 그대로 쓴다.
 * 우리가 download="이름"을 붙이면 교차 출처라 어차피 무시되고, 이름만 어긋난다.
 */
function renderFiles(el, attachments, onlyForms) {
  const list = (attachments || []).filter((a) => !onlyForms || a.kind === "서식");
  if (!list.length) {
    el.innerHTML = onlyForms ? ""
      : `<li class="none">이 공고에는 첨부파일이 없습니다. (K-Startup 공고는 첨부를 제공하지 않습니다)</li>`;
    return;
  }
  el.innerHTML = list.map((a) => `
    <li><a href="${escapeHtml(a.url)}" target="_blank" rel="noreferrer"
           title="${escapeHtml(a.name)}">
      <span class="kind ${escapeHtml(a.kind)}">${escapeHtml(a.kind)}</span>
      <span class="fname">${escapeHtml(a.name)}</span>
    </a></li>`).join("");
}

function showTab(name) {
  document.querySelectorAll(".tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) =>
    p.classList.toggle("active", p.id === "tab-" + name));
}

document.querySelectorAll(".tab").forEach((btn) =>
  btn.addEventListener("click", () => showTab(btn.dataset.tab)));

// ───────────────────────────────────────────────────── 상단 연결 상태

async function loadStatus() {
  try {
    const s = await api("/api/status");
    const mark = (ok, label) =>
      `<span class="${ok ? "on" : "off"}">${label} ${ok ? "연결됨" : "미설정"}</span>`;
    $("status").innerHTML =
      [mark(s.ai, "하이퍼클로바X"), mark(s.bizinfo, "기업마당"),
       mark(s.datagokr, "K-Startup")].join(" · ");
  } catch (e) {
    $("status").textContent = "상태를 확인할 수 없습니다.";
  }
}

// ─────────────────────────────────────────────────────────── ① 공고함

function noticeCard(item) {
  const card = document.createElement("div");
  card.className = "card";

  const dday = item.d_day === null ? "상시"
    : item.d_day < 0 ? "마감" : `D-${item.d_day}`;
  const urgent = item.d_day !== null && item.d_day >= 0 && item.d_day <= 3;
  const verdict = item.verdict || "미판정";
  const merged = item.merged
    ? `<span class="tag">${escapeHtml(item.sources.join(" · "))} 통합</span>` : "";

  card.innerHTML = `
    <div class="dday ${urgent ? "urgent" : ""}">${dday}</div>
    <div class="grow">
      <div class="title">${escapeHtml(item.title)}${merged}</div>
      <div class="line2">${escapeHtml(item.agency || "소관기관 미상")}
        · ${escapeHtml(item.support_field || "분야 미상")}
        · 마감 ${escapeHtml(item.apply_end || "미상")}</div>
    </div>
    <span class="badge ${verdict}">${verdict}</span>`;

  card.addEventListener("click", () => openNotice(item));
  return card;
}

async function loadNotices() {
  const params = new URLSearchParams();
  const region = $("f-region").value.trim();
  if (region) params.set("region", region);
  if ($("f-within").value) params.set("within", $("f-within").value);
  if ($("f-verdict").value) params.set("verdict", $("f-verdict").value);

  const list = $("notice-list");
  list.innerHTML = "";
  try {
    const data = await api("/api/notices?" + params.toString());
    $("inbox-count").textContent =
      `사업 ${data.total}건 (판정 완료 ${data.judged}건)`;
    if (!data.items.length) {
      list.innerHTML = `<p class="empty">조건에 맞는 공고가 없습니다.
        ‘공고 새로 수집’을 눌러보세요.</p>`;
      return;
    }
    data.items.forEach((item) => list.appendChild(noticeCard(item)));
  } catch (e) {
    list.innerHTML = `<p class="empty">${escapeHtml(e.message)}</p>`;
  }
}

$("btn-ingest").addEventListener("click", async () => {
  const btn = $("btn-ingest");
  btn.disabled = true;
  btn.textContent = "수집 중…";
  $("ingest-log").textContent = "기업마당·K-Startup에서 공고를 받아오고 중복을 통합합니다…";
  try {
    const result = await post("/api/ingest", {});
    $("ingest-log").textContent = result.notes.join("\n");
    await loadNotices();
  } catch (e) {
    $("ingest-log").textContent = "수집 실패 — " + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "공고 새로 수집";
  }
});

["f-region", "f-within", "f-verdict"].forEach((id) => {
  const el = $(id);
  el.addEventListener(id === "f-region" ? "input" : "change", () => {
    clearTimeout(el._timer);
    el._timer = setTimeout(loadNotices, 250);   // 타이핑마다 요청하지 않도록
  });
});

// ────────────────────────────────────────────────────── ② 자격 판정

async function openNotice(item) {
  current = item;
  currentDraft = null;
  showTab("verdict");
  $("verdict-empty").hidden = true;
  $("verdict-body").hidden = false;
  $("v-title").textContent = item.title;
  $("v-meta").innerHTML =
    `${escapeHtml(item.agency || "소관기관 미상")} · 출처 ${escapeHtml(item.sources.join(" · "))}` +
    (item.merge_reason ? ` · 통합 근거: ${escapeHtml(item.merge_reason)}` : "") +
    (item.url ? ` · <a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">원문 공고</a>` : "");

  $("v-rows").innerHTML = `<tr><td colspan="4">판정 중입니다…</td></tr>`;
  $("v-overall").textContent = "";
  $("v-note").textContent = "";
  renderFiles($("v-files"), item.attachments, false);
  await loadEligibility(false);
}

async function loadEligibility(refresh) {
  try {
    const report = await api(noticeUrl(current.id, "eligibility") +
                             (refresh ? "?refresh=1" : ""));
    $("v-overall").textContent = report.overall;
    $("v-overall").className = "badge " + report.overall;
    $("v-schedule").textContent = report.schedule.label || "";
    $("v-note").textContent = report.note || "";

    $("v-rows").innerHTML = report.rows.length
      ? report.rows.map((r) => `
          <tr>
            <td><strong>${escapeHtml(r.axis)}</strong><br>${escapeHtml(r.value)}</td>
            <td>${escapeHtml(r.company_value)}</td>
            <td><span class="badge ${r.verdict}">${r.verdict}</span>
                <div class="reason">${escapeHtml(r.reason)}</div></td>
            <td class="quote">“${escapeHtml(r.quote)}”</td>
          </tr>`).join("")
      : `<tr><td colspan="4">공고문에서 자격요건을 찾지 못했습니다. 원문을 직접 확인하세요.</td></tr>`;

    $("v-docs").innerHTML = report.required_docs.length
      ? report.required_docs.map((d) => `<li>${escapeHtml(d)}</li>`).join("")
      : `<li>공고문에서 제출서류를 찾지 못했습니다.</li>`;

    const s = report.schedule;
    $("v-sched").innerHTML = [
      `<li>접수 시작: ${escapeHtml(s.begin || "미상")}</li>`,
      `<li>접수 마감: ${escapeHtml(s.end || "미상")}</li>`,
      `<li>남은 기간: ${escapeHtml(s.label || "-")} (오늘 ${escapeHtml(s.today || "")})</li>`,
    ].join("");
  } catch (e) {
    $("v-rows").innerHTML =
      `<tr><td colspan="4">판정 실패 — ${escapeHtml(e.message)}</td></tr>`;
  }
}

$("btn-rejudge").addEventListener("click", async () => {
  $("v-rows").innerHTML = `<tr><td colspan="4">다시 판정 중입니다…</td></tr>`;
  await loadEligibility(true);
});

$("btn-to-draft").addEventListener("click", () => openDraft(false));

// ───────────────────────────────────────────────────── ③ 초안 · 점검

function renderDraft(draft) {
  currentDraft = draft;
  $("d-title").textContent = current.title;
  $("d-note").textContent = draft.note || "";
  // 초안을 옮겨 담을 서식 파일을 바로 옆에 둔다 — 다시 찾으러 가지 않게.
  renderFiles($("d-files"), current.attachments, true);

  // 서식의 '값만 채우는 칸'. 문단이 아니라 숫자·이름이라 초안에 넣지 않고 목록으로만
  // 알려 준다 — 서식을 열었을 때 뭘 채워야 하는지 미리 보이게 하는 게 목적이다.
  const fields = draft.form_fields || [];
  $("d-fields").innerHTML = fields.length
    ? `서식에서 값만 채우면 되는 칸: ` +
      fields.map((f) => `<span class="chip">${escapeHtml(f)}</span>`).join(" ")
    : "";
  $("d-sections").innerHTML = draft.sections.map((s, i) => `
    <div class="section">
      <h4>${escapeHtml(s.title)}</h4>
      <div class="src">${s.sources && s.sources.length
        ? "참고: " + escapeHtml(s.sources.join(", ")) : "참고한 과거 신청서 없음"}</div>
      <textarea data-index="${i}">${escapeHtml(s.body)}</textarea>
    </div>`).join("");
}

// 담당자가 화면에서 고친 내용을 그대로 모아 점검·복사에 쓴다.
function collectDraft() {
  const sections = currentDraft.sections.map((s, i) => ({
    title: s.title,
    body: ($("d-sections").querySelector(`textarea[data-index="${i}"]`) || {}).value ?? s.body,
    sources: s.sources || [],
  }));
  return { ...currentDraft, sections };
}

async function openDraft(refresh) {
  showTab("draft");
  $("draft-empty").hidden = true;
  $("draft-body").hidden = false;
  $("d-sections").innerHTML = `<p class="empty">초안을 만들고 있습니다… (10~20초)</p>`;
  $("d-issues").innerHTML = `<p class="empty small">‘제출 전 점검’을 누르세요.</p>`;
  try {
    if (!refresh) {
      const saved = await api(noticeUrl(current.id, "draft"));
      if (saved.draft) {
        renderDraft(saved.draft);
        if (saved.issues && saved.issues.length) renderIssues(saved.issues);
        return;
      }
    }
    renderDraft(await post(noticeUrl(current.id, "draft"), { refresh }));
  } catch (e) {
    $("d-sections").innerHTML = `<p class="empty">초안 생성 실패 — ${escapeHtml(e.message)}</p>`;
  }
}

function renderIssues(issues) {
  if (!issues.length) {
    $("d-issues").innerHTML = `<p class="empty small">발견된 문제가 없습니다.</p>`;
    return;
  }
  $("d-issues").innerHTML = issues.map((i) => `
    <div class="issue ${i.severity}">
      <div class="where">[${escapeHtml(i.severity)}] ${escapeHtml(i.kind)} · ${escapeHtml(i.where)}</div>
      <div class="msg">${escapeHtml(i.message)}</div>
      ${i.suggestion ? `<div class="fix">→ ${escapeHtml(i.suggestion)}</div>` : ""}
    </div>`).join("");
}

$("btn-regenerate").addEventListener("click", () => openDraft(true));

$("btn-check").addEventListener("click", async () => {
  if (!currentDraft) return;
  $("d-issues").innerHTML = `<p class="empty small">점검 중입니다…</p>`;
  try {
    const result = await post(noticeUrl(current.id, "check"), collectDraft());
    renderIssues(result.issues);
  } catch (e) {
    $("d-issues").innerHTML = `<p class="empty small">점검 실패 — ${escapeHtml(e.message)}</p>`;
  }
});

$("btn-copy").addEventListener("click", async () => {
  if (!currentDraft) return;
  const text = collectDraft().sections
    .map((s) => `■ ${s.title}\n${s.body}`).join("\n\n");
  try {
    await navigator.clipboard.writeText(text);
    $("d-copied").textContent = "복사했습니다. 한글 문서에 붙여넣으세요.";
  } catch (e) {
    $("d-copied").textContent = "복사에 실패했습니다. 직접 선택해 복사하세요.";
  }
  setTimeout(() => ($("d-copied").textContent = ""), 4000);
});

// ─────────────────────────────────────────────────────── 회사 프로필

// [키, 라벨, 종류]. 자격 판정이 실제로 쓰는 항목만 노출한다.
const PROFILE_FIELDS = [
  ["name", "회사명", "text"], ["ceo", "대표자", "text"],
  ["biz_no", "사업자등록번호", "text"],
  ["industry", "업종", "text"], ["ksic", "표준산업분류 코드", "text"],
  ["company_type", "기업규모", "text"],
  ["region", "소재지 시·도", "text"], ["region_detail", "시·군·구", "text"],
  ["founded", "설립일 (YYYY-MM-DD)", "text"],
  ["employees", "상시근로자 수", "number"],
  ["revenue_krw", "직전연도 매출액 (원)", "number"],
  ["export_usd", "직전연도 수출액 (달러)", "number"],
  ["tax_arrears", "국세·지방세를 체납 중이다", "bool"],
  ["closed", "휴업·폐업 상태다", "bool"],
  ["recent_layoffs", "최근 1년 이내 고용조정이 있었다", "bool"],
  ["root_tech", "뿌리기술 활용 기업이다", "bool"],
  ["women_owned", "여성기업 확인서를 보유했다", "bool"],
  ["smart_factory", "스마트공장을 이미 구축했다", "bool"],
  ["docs_on_hand", "보유 서류 (쉼표로 구분)", "list"],
  ["strengths", "회사 강점·현안 (초안에 쓰입니다)", "area"],
];

function renderProfile(p) {
  $("profile-form").innerHTML = PROFILE_FIELDS.map(([key, label, kind]) => {
    const value = p[key];
    if (kind === "bool") {
      return `<div class="field check">
        <input type="checkbox" id="p-${key}" ${value ? "checked" : ""}>
        <label for="p-${key}">${label}</label></div>`;
    }
    if (kind === "area") {
      return `<div class="field wide"><label for="p-${key}">${label}</label>
        <textarea id="p-${key}">${escapeHtml(value || "")}</textarea></div>`;
    }
    if (kind === "list") {
      return `<div class="field wide"><label for="p-${key}">${label}</label>
        <input type="text" id="p-${key}" value="${escapeHtml((value || []).join(", "))}"></div>`;
    }
    return `<div class="field"><label for="p-${key}">${label}</label>
      <input type="${kind}" id="p-${key}" value="${escapeHtml(value ?? "")}"></div>`;
  }).join("");
}

async function loadProfile() {
  try {
    renderProfile(await api("/api/profile"));
  } catch (e) {
    $("profile-form").innerHTML = `<p class="empty">${escapeHtml(e.message)}</p>`;
  }
}

$("btn-save-profile").addEventListener("click", async () => {
  const payload = {};
  PROFILE_FIELDS.forEach(([key, , kind]) => {
    const el = $("p-" + key);
    if (!el) return;
    if (kind === "bool") payload[key] = el.checked;
    else if (kind === "number") payload[key] = Number(el.value || 0);
    else if (kind === "list")
      payload[key] = el.value.split(",").map((s) => s.trim()).filter(Boolean);
    else payload[key] = el.value;
  });
  try {
    renderProfile(await post("/api/profile", payload));
    $("profile-saved").textContent =
      "저장했습니다. 프로필이 바뀌었으므로 판정은 다시 계산됩니다.";
    await loadNotices();
  } catch (e) {
    $("profile-saved").textContent = "저장 실패 — " + e.message;
  }
  setTimeout(() => ($("profile-saved").textContent = ""), 5000);
});

// ─────────────────────────────────────────────────────────────── 시작

loadStatus();
loadNotices();
loadProfile();
