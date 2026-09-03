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
  card.dataset.id = item.id;      // 일괄 판정이 이 카드의 뱃지만 콕 집어 갱신한다

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

  // 공고를 고르는 것이 이 화면의 유일한 동작인데 div 에 click 만 달려 있어서
  // 키보드로는 아무 공고도 열 수 없었다. 마우스 없이도 목록을 훑을 수 있어야 한다.
  card.tabIndex = 0;
  card.setAttribute("role", "button");
  card.addEventListener("click", () => openNotice(item));
  card.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();          // Space 가 페이지를 스크롤하지 않게
    openNotice(item);
  });
  return card;
}

// 현재 화면의 필터. 목록 조회와 '전체 판정'이 **같은 대상**을 보도록 한곳에서 만든다.
function currentFilter() {
  const f = {};
  const q = $("f-q").value.trim();
  if (q) f.q = q;
  const region = $("f-region").value.trim();
  if (region) f.region = region;
  if ($("f-within").value) f.within = $("f-within").value;
  if ($("f-verdict").value) f.verdict = $("f-verdict").value;
  if ($("f-sort").value) f.sort = $("f-sort").value;
  return f;
}

function renderTally(data) {
  const t = data.tally || {};
  const left = data.total - data.judged;
  $("inbox-count").textContent = `사업 ${data.total}건`;
  $("inbox-tally").dataset.left = left;
  $("inbox-tally").innerHTML =
    `<span class="badge 가능">가능 ${t["가능"] || 0}</span>` +
    `<span class="badge 확인필요">확인필요 ${t["확인필요"] || 0}</span>` +
    `<span class="badge 불가">불가 ${t["불가"] || 0}</span>` +
    (left > 0 ? `<span class="badge 미판정">미판정 ${left}</span>` : "");
  $("btn-judge-all").disabled = left === 0;

  // 미판정이 남아 있으면 묻지 않고 앞에서부터 판정을 시작한다.
  // 한 건에 3.3초라 전건(수백 건)은 수십 분이 걸린다. 그래서 자동으로는 화면 맨
  // 위 AUTO_JUDGE 건만 채우고, 나머지는 '목록 전체 판정' 버튼에 맡긴다.
  // 같은 목록에서 두 번 돌지 않도록 필터 조합을 키로 기억한다.
  const { sort, ...narrowing } = currentFilter();   // 정렬은 대상을 바꾸지 않는다
  const key = JSON.stringify(narrowing);
  if (left > 0 && !judging && autoKey !== key) {
    autoKey = key;
    judgeAll(AUTO_JUDGE);
  }
}

/* 우리 회사에 맞는 공고를 맨 위로.
 *
 * 수백 건을 마감순으로만 늘어놓으면 담당자가 위에서부터 훑다 지치고, 정작 맞는 공고는
 * 아래 묻힙니다. 점수는 지역·업종·지원분야·마감으로 코드가 매기고(LLM을 안 쓰므로
 * 빠르고 매번 같습니다), **왜 추천했는지 근거를 반드시 함께** 보여줍니다 —
 * 근거 없는 추천 목록은 한 번 훑고 무시하게 됩니다.
 */
function renderRecommended(picks) {
  const box = $("recommend-box");
  const list = $("recommend-list");
  if (!picks || !picks.length) {
    box.hidden = true;
    $("all-heading").hidden = true;
    return;
  }
  box.hidden = false;
  $("all-heading").hidden = false;
  $("rec-basis").textContent = "회사 프로필 기준";

  list.innerHTML = "";
  picks.forEach((p) => {
    const card = noticeCard(p.item);
    card.classList.add("pick");
    const grow = card.querySelector(".grow");
    const why = document.createElement("div");
    why.className = "why";
    why.textContent = "추천 이유: " + p.reasons.join(" · ");
    grow.appendChild(why);
    list.appendChild(card);
  });
}

const PAGE = 50;                 // 한 번에 그리는 카드 수

/* 682건을 한 번에 그리면 문서가 62,550px(87 화면)이 되고, 카드가 포커스를 받게 된
   뒤로는 목록을 지나 다음 컨트롤로 가는 데만 탭을 682번 눌러야 한다. 처음에는 50장만
   그리고 나머지는 눌러서 잇는다. 데이터는 이미 받아 놓았으므로 요청은 더 없다. */
function renderCards(list, items) {
  let shown = 0;
  const more = document.createElement("button");
  const draw = () => {
    items.slice(shown, shown + PAGE)
         .forEach((it) => list.insertBefore(noticeCard(it), more));
    shown = Math.min(shown + PAGE, items.length);
    more.textContent = `공고 ${items.length - shown}건 더 보기`;
    more.hidden = shown >= items.length;
  };
  more.addEventListener("click", draw);
  list.appendChild(more);
  draw();
}

/* 검색어를 치면 250ms 간격으로 요청이 나가는데 682건 응답이 그보다 오래 걸리면
   요청이 겹친다. await 전에 목록을 비우던 탓에 늦게 온 옛 응답이 새 응답 뒤에
   덧그려져 같은 카드가 여러 벌 쌓였다 (19건이 57건으로 보였다).
   마지막으로 보낸 요청의 응답만 그린다. 비우는 것도 응답이 온 뒤로 미뤄
   기다리는 동안 목록이 깜빡이지 않게 한다. */
let loadSeq = 0;

async function loadNotices() {
  const seq = ++loadSeq;
  const params = new URLSearchParams(currentFilter());
  const list = $("notice-list");
  try {
    const data = await api("/api/notices?" + params.toString());
    if (seq !== loadSeq) return;          // 더 새 요청이 이미 떠났다
    list.innerHTML = "";
    renderTally(data);
    renderRecommended(data.recommended);
    if (!data.items.length) {
      list.innerHTML = `<p class="empty">조건에 맞는 공고가 없습니다.
        검색어나 필터를 지워 보세요.</p>`;
      return;
    }
    renderCards(list, data.items);
  } catch (e) {
    if (seq !== loadSeq) return;
    list.innerHTML = `<p class="empty">${escapeHtml(e.message)}</p>`;
  }
}

/* 목록 전체 판정.
 *
 * 공고 하나마다 LLM을 부르므로 수백 건이면 수십 분이 걸린다. 한 번에 다 돌리면
 * 화면이 멈춘 것처럼 보이므로, 서버에 조금씩 나눠 요청하고 결과를 받는 대로 카드에
 * 반영한다. 그래서 담당자는 다 끝나기 전에도 '가능'부터 눈으로 집을 수 있고,
 * 중간에 멈출 수도 있다.
 */
let judging = false;
let autoKey = null;          // 자동 판정을 이미 돌린 필터 조합
const AUTO_JUDGE = 12;       // 자동으로 채우는 건수 (배치 4 × 3회 ≈ 10초)

async function judgeAll(max = Infinity) {
  if (judging) return;
  judging = true;
  $("btn-judge-all").hidden = true;
  $("btn-judge-stop").hidden = false;

  try {
    let judged = 0;
    while (judging && judged < max) {
      const limit = Math.min(4, max - judged);
      const result = await post("/api/judge", { ...currentFilter(), limit });
      judged += result.judged.length;
      // 받은 판정을 카드에 바로 칠한다 (전체를 다시 불러오지 않는다).
      result.judged.forEach((j) => {
        const el = document.querySelector(`.card[data-id="${CSS.escape(j.id)}"] .badge`);
        if (el) {
          el.textContent = j.verdict;
          el.className = "badge " + j.verdict;
        }
      });
      $("ingest-log").textContent =
        `판정 중… ${result.done}/${result.total}건 (남은 ${result.remaining}건)`;
      if (!result.remaining || !result.judged.length) break;
    }
    const rest = Number($("inbox-tally").dataset.left || 0) - judged;
    $("ingest-log").textContent = !judging ? "판정을 멈췄습니다."
      : max === Infinity || rest <= 0 ? "판정을 마쳤습니다."
      : `앞에서 ${judged}건을 판정했습니다. 남은 ${rest}건은 ‘목록 전체 판정’을 누르세요.`;
  } catch (e) {
    $("ingest-log").textContent = "판정 실패 — " + e.message;
  } finally {
    judging = false;
    $("btn-judge-all").hidden = false;
    $("btn-judge-stop").hidden = true;
    await loadNotices();          // 집계와 뱃지를 최종 상태로 맞춘다
  }
}

$("btn-judge-all").addEventListener("click", () => judgeAll());
$("btn-judge-stop").addEventListener("click", () => { judging = false; });

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

["f-q", "f-region", "f-within", "f-verdict", "f-sort"].forEach((id) => {
  const el = $(id);
  el.addEventListener(el.tagName === "SELECT" ? "change" : "input", () => {
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
  // 좌측 레일 컨텍스트 — 단계를 넘어가도 어느 공고를 붙들고 있는지 안 끊기게 한다.
  $("ctx-title").textContent = item.title;
  $("rail-ctx").hidden = false;
  // 출처를 분명히 밝힌다. 어느 기관 오픈API에서 받은 자료인지, 원문은 어디인지를
  // 기관별로 따로 건다 — 통합된 공고는 양쪽 원문이 다 있어야 대조가 된다.
  const links = (item.source_links || []).length
    ? (item.source_links || []).map((l) =>
        `<a href="${escapeHtml(l.url)}" target="_blank" rel="noreferrer">
           ${escapeHtml(l.source)} 원문 공고 ↗</a>`).join(" · ")
    : `<span class="none">원문 링크가 제공되지 않은 공고입니다.</span>`;

  $("v-meta").innerHTML =
    `소관기관 ${escapeHtml(item.agency || "미상")}` +
    ` · 자료 출처 <strong>${escapeHtml(item.sources.join(" + "))} 오픈API</strong>` +
    (item.merge_reason
      ? ` · 통합 근거: ${escapeHtml(item.merge_reason)}` : "") +
    `<br>${links}`;

  $("v-rows").innerHTML = `<tr>` + [4,3,4].map((n,i)=>`<td>` + `<span class="sk w80"></span>`.repeat(1) + (i===2 ? `<span class="sk w60"></span>` : ``) + `</td>`).join("") + `<td><span class="sk"></span><span class="sk w40"></span></td></tr>`;
  $("v-overall").textContent = "";
  $("v-note").textContent = "";
  renderOverview(item);
  renderFiles($("v-files"), item.attachments, false);
  await loadEligibility(false);
}

/* 판정 위에 공고 자체를 요약해 보여준다.
 *
 * '가능/불가'만 봐서는 이 사업이 뭘 해 주는 건지 알 수 없어, 담당자가 결국 원문 공고를
 * 다시 열게 된다. 개요·지원내용·지원대상을 함께 두면 이 화면에서 판단이 끝난다.
 */
function renderOverview(item) {
  const rows = [
    ["지원분야", item.support_field],
    ["소관기관", item.agency],
    ["지원지역", item.region_text],
    ["사업 개요", item.summary],
    ["지원대상", item.target_text],
    ["신청 제외대상", item.exclude_text],
    ["신청방법", item.docs_text],
  ].filter(([, v]) => v && String(v).trim());

  $("v-overview").innerHTML = rows.length
    ? rows.map(([k, v]) =>
        `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("")
    : `<dd class="none">공고문에 요약 정보가 없습니다. 원문 공고를 확인하세요.</dd>`;
}

async function loadEligibility(refresh) {
  try {
    const report = await api(noticeUrl(current.id, "eligibility") +
                             (refresh ? "?refresh=1" : ""));
    $("v-overall").textContent = report.overall;
    $("v-overall").className = "badge " + report.overall;
    $("ctx-badge").textContent = report.overall;
    $("ctx-badge").className = "badge " + report.overall;
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

    loadChecklist(report.docs_source);

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

/* 제출서류 체크리스트.
 *
 * 경고 목록이 아니라 체크리스트인 이유: 서류를 실제로 뗐는지는 사람만 압니다.
 * 도구가 "없습니다"라고 단정하면 이미 준비한 서류에도 빨간 경고가 뜨고, 그런 게
 * 몇 개 쌓이면 담당자가 경고 전체를 무시하게 됩니다. 아는 것(프로필의 보유 서류)만
 * 미리 체크해 두고 나머지는 담당자가 직접 켭니다. 체크 상태는 서버에 남습니다.
 */
function renderChecklist(data, docsSource) {
  $("v-docs-progress").textContent =
    data.total ? `${data.done}/${data.total} 준비됨` : "";
  $("v-docs-note").textContent = data.note || "";

  // 항목마다 같은 힌트가 붙으면 다섯 번 반복돼 정보가 아니라 배경 소음이 된다.
  // 전부 같은 문구일 때는 목록 아래에 한 번만 쓴다.
  const hints = data.items.map((it) => it.hint || "");
  const sharedHint = hints.length > 1 && hints.every((h) => h === hints[0]) ? hints[0] : "";

  $("v-checklist").innerHTML = data.items.length
    ? data.items.map((it) => `
        <label class="doc ${it.checked ? "done" : ""}">
          <input type="checkbox" data-doc="${escapeHtml(it.name)}"
                 ${it.checked ? "checked" : ""}>
          <span class="kind ${escapeHtml(it.kind)}">${escapeHtml(it.kind)}</span>
          <span class="dname">${escapeHtml(it.name)}</span>
          ${sharedHint ? "" : `<span class="dhint">${escapeHtml(it.hint)}</span>`}
        </label>`).join("") +
      (sharedHint
        ? `<p class="src-note">${escapeHtml(sharedHint)}</p>` : "") +
      (docsSource
        ? `<p class="src-note">서류 목록 출처: ${escapeHtml(docsSource)}</p>` : "")
    : `<p class="empty small">제출서류를 찾지 못했습니다.</p>`;

  $("v-checklist").querySelectorAll("input[type=checkbox]")
    .forEach((box) => box.addEventListener("change", saveChecklist));
}

async function loadChecklist(docsSource) {
  $("v-checklist").innerHTML = `<span class="sk w60"></span><span class="sk w80"></span><span class="sk w60"></span>`;
  try {
    renderChecklist(await api(noticeUrl(current.id, "checklist")), docsSource);
  } catch (e) {
    $("v-checklist").innerHTML =
      `<p class="empty small">${escapeHtml(e.message)}</p>`;
  }
}

async function saveChecklist() {
  const checks = {};
  $("v-checklist").querySelectorAll("input[type=checkbox]")
    .forEach((box) => { checks[box.dataset.doc] = box.checked; });
  try {
    const data = await post(noticeUrl(current.id, "checklist"), { checks });
    $("v-docs-progress").textContent = `${data.done}/${data.total} 준비됨`;
    $("v-checklist").querySelectorAll(".doc").forEach((el) => {
      const box = el.querySelector("input");
      el.classList.toggle("done", box.checked);
    });
  } catch (e) {
    $("v-docs-note").textContent = "체크 상태를 저장하지 못했습니다 — " + e.message;
  }
}

$("btn-rejudge").addEventListener("click", async () => {
  $("v-rows").innerHTML = `<tr>` + [4,3,4].map((n,i)=>`<td>` + `<span class="sk w80"></span>`.repeat(1) + (i===2 ? `<span class="sk w60"></span>` : ``) + `</td>`).join("") + `<td><span class="sk"></span><span class="sk w40"></span></td></tr>`;
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
  fillChatTargets(draft.sections);
  $("d-sections").innerHTML = draft.sections.map((s, i) => `
    <div class="section">
      <h4>${escapeHtml(s.title)}</h4>
      <div class="src">${s.sources && s.sources.length
        ? "참고: " + escapeHtml(s.sources.join(", ")) : "참고한 과거 신청서 없음"}</div>
      <textarea data-index="${i}">${escapeHtml(s.body)}</textarea>
    </div>`).join("");
}

/* 고칠 항목 목록을 실제 초안에 맞춘다. 항목 이름은 공고 서식에서 오므로
   공고마다 다르다 — 고정 목록을 둘 수 없다. */
function fillChatTargets(sections) {
  const sel = $("chat-target");
  const keep = sel.value;
  sel.innerHTML = `<option value="">내용을 보고 항목 찾기</option>
    <option value="*">전체 항목</option>` +
    sections.map((s) => `<option>${escapeHtml(s.title)}</option>`).join("");
  sel.value = [...sel.options].some((o) => o.value === keep) ? keep : "";
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

async function openDraft(refresh, sections) {
  showTab("draft");
  $("draft-empty").hidden = true;
  $("draft-body").hidden = false;
  $("section-editor").hidden = true;
  // 다른 공고의 초안을 열면 앞선 대화는 남겨 두면 안 된다 — "더 짧게" 같은 말이
  // 엉뚱한 공고의 맥락으로 이어진다.
  chatHistory = [];
  $("chat-log").innerHTML = `<p class="empty small">고칠 내용을 적으면 결과를 여기에서 보여 드립니다.</p>`;
  // 항목을 하나씩 따로 생성하므로(분량 확보) 항목 수만큼 시간이 걸린다.
  $("d-sections").innerHTML =
    `<p class="empty">초안을 만들고 있습니다…<br>항목마다 따로 써 내려가느라
     30초~1분 걸립니다. 잠시만 기다려 주세요.</p>`;
  $("d-issues").innerHTML = `<p class="empty small">‘제출 전 점검’을 누르면 확인할 내용을 보여 드립니다.</p>`;
  try {
    if (!refresh && !sections) {
      const saved = await api(noticeUrl(current.id, "draft"));
      if (saved.draft) {
        renderDraft(saved.draft);
        if (saved.issues && saved.issues.length) renderIssues(saved.issues);
        return;
      }
    }
    renderDraft(await post(noticeUrl(current.id, "draft"), { refresh, sections }));
  } catch (e) {
    $("d-sections").innerHTML = `<p class="empty">초안 생성 실패 — ${escapeHtml(e.message)}</p>`;
  }
}

/* 작성 항목 직접 고치기.
 *
 * 첨부가 없거나 PDF 서식이라 항목을 못 읽으면 기본 구성으로 씁니다. 그때 담당자가
 * 실제 양식을 찾아 확인했다면, 그 항목을 여기에 적어 다시 쓰게 하는 게 맞습니다 —
 * 도구가 못 읽었다고 담당자까지 기본 구성에 갇힐 이유가 없습니다.
 */
$("btn-edit-sections").addEventListener("click", () => {
  const editor = $("section-editor");
  if (!editor.hidden) { editor.hidden = true; return; }
  $("section-list").value =
    (currentDraft ? currentDraft.sections.map((s) => s.title) : []).join("\n");
  editor.hidden = false;
  $("section-list").focus();
});

$("btn-cancel-sections").addEventListener("click", () => {
  $("section-editor").hidden = true;
});

$("btn-apply-sections").addEventListener("click", async () => {
  const titles = $("section-list").value.split("\n")
    .map((t) => t.replace(/^\s*[-*■□]?\s*\d*[.)]?\s*/, "").trim())
    .filter(Boolean);
  if (!titles.length) {
    $("d-copied").textContent = "항목을 한 줄에 하나씩 적어 주세요.";
    return;
  }
  await openDraft(true, titles);
});

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
  $("d-issues").innerHTML = `<span class="sk w80"></span><span class="sk"></span><span class="sk w40"></span>`;
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

/* 말로 고치기 (대화 Agent).
 *
 * 보내는 것은 **저장된 초안이 아니라 화면에 보이는 글**이다(collectDraft). 담당자가
 * 방금 손으로 고쳐 넣은 문장을 서버가 모른 채 덮어쓰면 안 된다.
 *
 * 앞선 대화를 함께 보내 "더 짧게", "그거 말고" 같은 이어지는 말이 통하게 한다.
 */
let chatHistory = [];

function pushChat(role, text, changed) {
  const log = $("chat-log");
  if (log.querySelector(".empty")) log.innerHTML = "";
  const row = document.createElement("div");
  row.className = "chat-msg " + role;
  row.innerHTML = escapeHtml(text).replace(/\n/g, "<br>") +
    (changed && changed.length
      ? `<div class="chat-changed">고친 항목: ${changed.map(escapeHtml).join(", ")}</div>`
      : "");
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
  return row;
}

async function sendChat() {
  const input = $("chat-text");
  const message = input.value.trim();
  if (!message || !currentDraft) return;
  input.value = "";
  pushChat("me", message);
  const waiting = pushChat("bot", "고치는 중입니다…");
  $("btn-chat-send").disabled = true;
  try {
    const result = await post(noticeUrl(current.id, "chat"), {
      message,
      target: $("chat-target").value,
      sections: collectDraft().sections,
      history: chatHistory.slice(-6),
    });
    waiting.remove();
    pushChat("bot", result.reply, result.changed);
    chatHistory.push({ role: "담당자", text: message });
    chatHistory.push({ role: "도우미", text: result.reply });
    if (result.changed && result.changed.length) {
      // 고쳐진 본문으로 화면을 다시 그리고, 바뀐 항목을 잠깐 표시해 준다.
      renderDraft({ ...currentDraft, sections: result.sections,
                    unresolved: result.unresolved || [] });
      highlightChanged(result.changed);
    }
  } catch (e) {
    waiting.remove();
    pushChat("bot", "고치지 못했습니다 — " + e.message);
  } finally {
    $("btn-chat-send").disabled = false;
    input.focus();
  }
}

/* 손으로 고친 글을 저장한다.

   지금까지 초안이 저장되는 경로는 대화(chat)뿐이었다. textarea 에 직접 친 글은
   collectDraft() 로 모아 점검·복사에만 쓰였고 어디에도 남지 않아서, 탭을 옮기거나
   새로고침하면 사라졌다. 명시적인 저장 버튼을 두면 누르는 걸 잊으므로 친 뒤
   잠깐 멈추면 알아서 보낸다. */
let saveTimer = null;

function saveDraftSoon() {
  if (!current) return;
  clearTimeout(saveTimer);
  $("d-saved").textContent = "…";
  saveTimer = setTimeout(async () => {
    try {
      await post(noticeUrl(current.id, "save"), { sections: collectDraft().sections });
      currentDraft = collectDraft();     // 화면과 기억을 같은 글로 맞춘다
      $("d-saved").textContent = "저장됨";
    } catch (e) {
      $("d-saved").textContent = "저장 실패 — " + e.message;
    }
  }, 900);
}

// 항목은 초안을 그릴 때마다 새로 만들어지므로 컨테이너에 한 번만 건다.
$("d-sections").addEventListener("input", (e) => {
  if (e.target.tagName === "TEXTAREA") saveDraftSoon();
});

// 어느 항목이 바뀌었는지 눈으로 알 수 있게 잠깐 테두리를 준다. 항목이 여럿이면
// 화면 어디가 달라졌는지 글만 봐서는 찾기 어렵다.
function highlightChanged(titles) {
  const boxes = [...$("d-sections").querySelectorAll(".section")];
  let first = null;
  boxes.forEach((box) => {
    const title = box.querySelector("h4").textContent;
    if (!titles.includes(title)) return;
    box.classList.add("changed");
    if (!first) first = box;
    setTimeout(() => box.classList.remove("changed"), 4000);
  });
  if (first) first.scrollIntoView({ behavior: "smooth", block: "center" });
}

$("btn-chat-send").addEventListener("click", sendChat);
$("chat-text").addEventListener("keydown", (e) => {
  // Enter 로 보내고 Shift+Enter 로 줄바꿈 — 채팅에서 익숙한 방식.
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendChat();
  }
});

// ─────────────────────────────────────────────────────── 회사 프로필

// [키, 라벨, 종류]. 자격 판정이 실제로 쓰는 항목만 노출한다.
/* 자격 판정이 실제로 읽는 값. agent/eligibility.py 의 _judge_region(p.region,
   p.region_detail) · _judge_years(p.founded) · _judge_size(p.employees,
   p.revenue_krw) · _judge_industry(p.industry) 와 1:1이다. 이 중 하나라도
   비어 있으면 그 축은 판정이 안 되고 '확인필요'로 밀린다. */
const JUDGE_FIELDS = ["region", "region_detail", "founded",
                      "employees", "revenue_krw", "industry"];

function profileGaps(p) {
  return JUDGE_FIELDS.filter((k) => {
    const v = p[k];
    return v === "" || v === null || v === undefined || v === 0;
  });
}

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
  ["venture_certified", "벤처인증기업이다", "bool"],
  ["root_tech", "뿌리기술 활용 기업이다", "bool"],
  ["women_owned", "여성기업 확인서를 보유했다", "bool"],
  ["smart_factory", "스마트공장을 이미 구축했다", "bool"],
  ["tech_services", "보유 기술 및 서비스 (구체적일수록 초안이 구체적으로 나옵니다)", "area"],
  ["strengths", "회사 강점·현안 (초안에 쓰입니다)", "area"],
  ["prior_support", "최근 3년 지원사업 수혜 이력 (쉼표로 구분 — 중복 수혜 판정에 쓰입니다)", "list"],
  // 주생산품·설비처럼 회사마다 다른 항목. 미리 칸을 정해 둘 수 없어 이름부터 직접 적는다.
  ["extra", "그 밖의 회사 정보 (주생산품·설비 등 — 초안에 그대로 쓰입니다)", "pairs"],
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
    if (kind === "pairs") {
      return `<div class="field wide"><label>${label}</label>
        <div id="p-${key}" class="pairs"></div>
        <button type="button" class="ghost" id="p-${key}-add">+ 항목 추가</button></div>`;
    }
    // 어느 값이 판정을 좌우하는지 폼 위에서 바로 보이게 한다.
    const must = JUDGE_FIELDS.includes(key) ? ` <span class="chip">판정 필수</span>` : "";
    return `<div class="field"><label for="p-${key}">${label}${must}</label>
      <input type="${kind}" id="p-${key}" value="${escapeHtml(value ?? "")}"></div>`;
  }).join("");

  // 이름·내용 쌍은 innerHTML 을 다시 쓰면 입력 중인 값이 날아가므로 DOM 으로 붙인다.
  PROFILE_FIELDS.forEach(([key, , kind]) => {
    if (kind !== "pairs") return;
    const box = $("p-" + key);
    const entries = Object.entries(p[key] || {});
    if (!entries.length) entries.push(["", ""]);
    entries.forEach(([k, v]) => box.appendChild(pairRow(k, v)));
    $("p-" + key + "-add").addEventListener("click", () => {
      box.appendChild(pairRow("", ""));
      box.lastChild.querySelector("input").focus();
    });
  });
}

/* '이름 / 내용' 한 줄. 회사마다 적을 항목이 달라(주생산품·설비·보유 라인…)
 * 칸을 미리 정해 둘 수 없어, 이름부터 담당자가 적게 한다. */
function pairRow(name, value) {
  const row = document.createElement("div");
  row.className = "pair";
  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.className = "pair-key";
  nameInput.placeholder = "항목 이름 (예: 주생산품)";
  nameInput.value = name;
  const valueInput = document.createElement("input");
  valueInput.type = "text";
  valueInput.className = "pair-value";
  valueInput.placeholder = "내용";
  valueInput.value = value;
  const del = document.createElement("button");
  del.type = "button";
  del.className = "ghost pair-del";
  del.textContent = "×";
  del.title = "이 항목 지우기";
  del.addEventListener("click", () => row.remove());
  row.append(nameInput, valueInput, del);
  return row;
}

/* 안내 상자를 지금 상태에 맞춘다. 채워야 할 게 남았으면 무엇이 왜 필요한지
   적고, 다 찼으면 상자를 감춘다. 돌려주는 값은 '아직 비었는가'다. */
function renderOnboard(p) {
  const labels = Object.fromEntries(PROFILE_FIELDS.map(([k, l]) => [k, l]));
  const gaps = profileGaps(p);
  $("profile-onboard").hidden = !gaps.length;
  if (gaps.length) {
    $("onboard-why").innerHTML =
      // 라벨의 형식 안내 괄호("설립일 (YYYY-MM-DD)")는 문장 안에서 읽기 나쁘다.
      `자격 판정은 <b>${gaps.map((k) => escapeHtml((labels[k] || k).replace(/\s*\(.*\)/, ""))).join("</b>, <b>")}</b>`
      + ` 값을 보고 합니다. 비어 있으면 그 요건은 판정하지 못하고 ‘확인필요’로 남습니다.`;
  }
  return gaps.length > 0;
}

async function loadProfile() {
  try {
    const p = await api("/api/profile");
    renderProfile(p);
    // 판정에 쓸 값이 없는 채로 공고함을 열면 682건이 전부 '확인필요'로만 나온다.
    // 그 화면은 도구가 뭘 하는지 보여주지 못하므로, 처음에는 여기부터 보인다.
    if (renderOnboard(p)) showTab("profile");
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
    else if (kind === "pairs") {
      const out = {};
      el.querySelectorAll(".pair").forEach((row) => {
        const name = row.querySelector(".pair-key").value.trim();
        // 이름이 없으면 버린다 — 빈 줄로 남겨 둔 행이다.
        if (name) out[name] = row.querySelector(".pair-value").value.trim();
      });
      payload[key] = out;
    } else payload[key] = el.value;
  });
  try {
    const saved = await post("/api/profile", payload);
    const wasIncomplete = !$("profile-onboard").hidden;
    renderProfile(saved);
    const stillIncomplete = renderOnboard(saved);
    $("profile-saved").textContent =
      "저장했습니다. 프로필이 바뀌었으므로 판정을 다시 계산합니다.";
    autoKey = null;
    await loadNotices();
    // 처음 채운 사람은 여기서 멈추면 다음에 뭘 할지 모른다. 공고함으로 넘긴다.
    if (wasIncomplete && !stillIncomplete) showTab("inbox");
  } catch (e) {
    $("profile-saved").textContent = "저장 실패 — " + e.message;
  }
  setTimeout(() => ($("profile-saved").textContent = ""), 5000);
});

/* 기존 사업계획서 올리기.
 *
 * 파일을 본문에 그대로 싣고 이름은 헤더에 담습니다 — 표준 라이브러리 서버라
 * multipart 파서가 없고, 직접 짜면 경계 문자열 처리에서 사고가 납니다.
 */
function renderPastList(items) {
  const list = $("past-list");
  if (!items || !items.length) {
    list.innerHTML =
      `<li class="none">아직 올린 사업계획서가 없습니다. 초안이 회사 프로필만 보고 쓰입니다.</li>`;
    return;
  }
  list.innerHTML = items.map((it) => `
    <li><a href="#" data-del="${escapeHtml(it.name)}" title="지우기">
      <span class="kind">${it.sections ? it.sections + "문항" : "1덩어리"}</span>
      <span class="fname">${escapeHtml(it.name)}</span>
      <span class="kind">${it.chars.toLocaleString()}자</span>
      <span class="kind">지우기</span>
    </a></li>`).join("");

  list.querySelectorAll("a[data-del]").forEach((a) =>
    a.addEventListener("click", async (e) => {
      e.preventDefault();
      if (!confirm(`'${a.dataset.del}' 을 지울까요?`)) return;
      const r = await post("/api/past-applications/delete", { name: a.dataset.del });
      renderPastList(r.items);
      $("past-log").textContent = "지웠습니다.";
    }));
}

async function loadPastList() {
  try {
    renderPastList((await api("/api/past-applications")).items);
  } catch (e) {
    $("past-list").innerHTML = `<li class="none">${escapeHtml(e.message)}</li>`;
  }
}

// 인풋을 감췄으니 무엇을 골랐는지는 직접 알려 줘야 한다.
$("past-file").addEventListener("change", () => {
  const picked = [...($("past-file").files || [])];
  $("past-log").textContent = !picked.length ? ""
    : picked.length === 1 ? picked[0].name : `${picked.length}개 선택됨`;
  $("btn-upload-past").disabled = !picked.length;
});

$("btn-upload-past").addEventListener("click", async () => {
  const files = [...($("past-file").files || [])];
  if (!files.length) {
    $("past-log").textContent = "올릴 파일을 고르세요.";
    return;
  }
  $("btn-upload-past").disabled = true;
  const notes = [];
  for (const file of files) {
    $("past-log").textContent = `${file.name} 여는 중…`;
    try {
      const res = await api("/api/past-applications", {
        method: "POST",
        headers: { "X-Filename": encodeURIComponent(file.name) },
        body: await file.arrayBuffer(),
      });
      notes.push(`${file.name}: ${res.note}`);
      if (res.items) renderPastList(res.items);
    } catch (e) {
      notes.push(`${file.name}: ${e.message}`);
    }
  }
  $("past-log").textContent = notes.join(" / ");
  $("past-file").value = "";
  $("btn-upload-past").disabled = false;
});

// ─────────────────────────────────────────────────────────────── 시작

loadStatus();
loadNotices();
loadProfile();
loadPastList();
