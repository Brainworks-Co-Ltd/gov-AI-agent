"""초안을 말로 고치는 대화 Agent.

담당자가 초안을 받고 나서 하는 일은 '다시 쓰기'가 아니라 '여기만 손보기'다.
기대효과가 두루뭉술하다, 추진 계획에 일정이 없다, 문체가 딱딱하다 — 이런 요청을
항목을 찾아 직접 고치는 대신 말로 하게 한다.

**전체를 다시 만들지 않는다.** 이 모듈에서 가장 중요한 규칙이다. '기대효과를 더
구체적으로'라는 요청에 초안 전체를 새로 쓰면, 담당자가 다른 항목에 직접 써 넣은
문장이 통째로 날아간다. 되돌릴 방법도 없다. 그래서 두 단계로 나눈다.

    ① 무엇을 고쳐 달라는 요청인지, 어느 항목에 해당하는지 먼저 정한다 (라우팅)
    ② 그 항목만 다시 쓴다. 나머지 항목은 화면에 있던 글자 그대로 둔다

**대화가 할루시네이션 우회로가 되면 안 된다.** 초안 생성에 걸어 둔 안전장치
(자리표시자 치환·근거 없는 주장 표시·근거 없는 수치 표시·문체 통일)를 고친 글에도
똑같이 적용한다. "매출 30% 성장했다고 써 줘" 같은 요청이 통과되면, 심사 지침이
탈락 사유로 명시한 바로 그것을 도구가 대신 만들어 주는 꼴이 된다.
"""
from __future__ import annotations

import time

from agent import drafter
from agent.schemas import CompanyProfile, Notice
from tools import hyperclova_api

# 한 번의 요청으로 고칠 수 있는 항목 수. 항목마다 따로 부르므로 이 수만큼 시간이 든다.
# '전체적으로 문체를 바꿔 줘' 같은 요청은 항목이 많으면 오래 걸리는데, 담당자를 1분
# 넘게 기다리게 하느니 몇 개만 고치고 나머지는 다시 요청하게 하는 편이 낫다.
MAX_TARGETS = 4

_ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["수정", "질문"]},
        "targets": {"type": "array", "items": {"type": "string"}},
        "instruction": {"type": "string"},
        "answer": {"type": "string"},
    },
    "required": ["intent", "targets", "instruction", "answer"],
}

_ROUTE_SYSTEM = (
    "너는 정부지원사업 신청서 초안을 담당자와 함께 고치는 도우미다. 담당자의 말이 "
    "**어느 항목을 어떻게 고쳐 달라는 것인지** 판단한다.\n"
    "\n"
    "- intent: 초안 본문을 바꿔 달라는 요청이면 '수정', 그냥 묻는 말이면 '질문'.\n"
    "- targets: 고쳐야 할 항목 제목을 [항목 목록]에 있는 그대로 적는다. 담당자가 항목을 "
    "집어 말하지 않아도 내용으로 판단해라 ('일정이 없다' → 추진 계획).\n"
    "  **어느 한 항목이 아니라 초안 전체에 해당하는 요청이면 [\"*\"] 한 개만 넣어라.** "
    "문체·말투·분량·용어 통일이 여기 해당한다:\n"
    "    '전체적으로 문장을 짧게' → [\"*\"]\n"
    "    '말투를 딱딱하게 바꿔 줘' → [\"*\"]\n"
    "    '전부 더 자세히 써 줘' → [\"*\"]\n"
    "  질문이면 빈 배열.\n"
    "- instruction: 그 항목을 **어떻게** 고칠지 한두 문장으로 적는다. 담당자의 말을 "
    "그대로 옮기지 말고, 글을 고치는 사람이 바로 따를 수 있는 지시로 바꿔라.\n"
    "- answer: intent가 '질문'일 때만 담당자에게 할 답을 적는다. '수정'이면 빈 문자열.\n"
    "\n"
    "항목을 못 고르겠으면 targets를 비우고 intent를 '질문'으로 두고, answer에 어느 "
    "항목을 고칠지 되물어라. 엉뚱한 항목을 고치는 것보다 되묻는 편이 낫다."
)

_EDIT_SYSTEM = drafter._SECTION_SYSTEM + (
    "\n\n[지금은 이미 있는 글을 고치는 중이다]\n"
    "담당자가 고쳐 달라고 한 부분만 손본다. **요청과 상관없는 문장은 그대로 둬라** — "
    "말투를 바꿔 달라고 했는데 내용까지 바뀌면 담당자가 무엇이 달라졌는지 알 수 없다.\n"
    "고친 항목의 전체 본문을 처음부터 끝까지 다시 출력한다. 바뀐 곳만 따로 적거나 "
    "'수정했습니다' 같은 설명을 붙이지 마라.\n"
    "담당자가 없는 실적·수치를 써 달라고 하더라도 지어내지 마라. 그 자리에는 "
    "'[확인 필요: 무엇]'을 적어 담당자가 실제 값을 넣게 한다."
)


def _outline(sections: list[dict]) -> str:
    """라우팅에 넘길 항목 목록 — 제목과 첫 줄만. 본문을 다 넣으면 길기만 하고,
    어느 항목인지 고르는 데에는 첫 줄이면 충분하다."""
    out = []
    for s in sections:
        head = (s.get("body") or "").strip().splitlines()
        out.append(f"- {s.get('title', '')}: {head[0][:60] if head else '(빈 항목)'}")
    return "\n".join(out)


def _route(message: str, sections: list[dict], notice: Notice,
           history: list[dict]) -> dict:
    """담당자의 말을 (의도, 대상 항목, 지시)로 바꾼다."""
    recent = "\n".join(f"{h.get('role')}: {h.get('text', '')[:200]}"
                       for h in history[-4:])
    # 공고 정보를 함께 넘긴다. 이게 없으면 "마감이 언제야?" 같은 질문에 도구가 답을
    # 갖고 있으면서도 "확인할 수 없습니다"라고 답한다 — 담당자가 가장 답답해할 대목이다.
    deadline = notice.apply_end.isoformat() if notice.apply_end else "미상"
    d_day = notice.d_day
    facts = (f"제목: {notice.title}\n소관기관: {notice.agency}\n"
             f"지원분야: {notice.support_field}\n"
             f"접수 마감: {deadline}"
             + (f" (D-{d_day})" if d_day is not None else "") + "\n"
             f"지원대상: {(notice.target_text or '')[:400]}\n"
             f"개요: {(notice.summary or '')[:800]}")
    user = (f"[공고 정보 — 질문에 답할 때 이 내용을 근거로 쓴다]\n{facts}\n\n"
            f"[항목 목록]\n{_outline(sections)}\n\n"
            + (f"[앞선 대화]\n{recent}\n\n" if recent else "")
            + f"[담당자의 말]\n{message}")
    try:
        return hyperclova_api.chat_structured(_ROUTE_SYSTEM, user, _ROUTE_SCHEMA,
                                             max_tokens=700, temperature=0.1)
    except Exception as e:
        # 라우팅이 실패하면 아무것도 고치지 않는다. 어느 항목인지 모르는 채로
        # 손대면 엉뚱한 항목이 통째로 바뀐다.
        return {"intent": "질문", "targets": [], "instruction": "",
                "answer": f"요청을 이해하지 못했습니다. ({e}) 어느 항목을 어떻게 "
                          f"고칠지 항목 이름과 함께 다시 말씀해 주세요."}


def _resolve_targets(targets: list, sections: list[dict]) -> list[int]:
    """모델이 돌려준 항목 이름을 실제 항목 번호로 바꾼다.

    이름이 조금씩 어긋나는 일이 잦아('기대 효과' vs '기대효과') 공백을 지우고 견준다.
    """
    titles = [(s.get("title") or "") for s in sections]
    # 모델이 '*' 대신 '전체'·'모든 항목' 처럼 말로 적어 보내는 일이 잦다. 그걸 못 알아
    # 들으면 "전체적으로 문장을 짧게 해 줘" 같은 흔한 요청이 통째로 실패한다.
    _ALL = {"*", "전체", "전부", "모두", "모든항목", "전체항목", "all"}
    if any("".join((t or "").split()).lower() in _ALL for t in targets):
        return list(range(len(sections)))

    def key(t: str) -> str:
        return "".join((t or "").split())

    picked: list[int] = []
    for want in targets:
        for i, title in enumerate(titles):
            if i in picked:
                continue
            if key(title) == key(want) or (key(want) and key(want) in key(title)):
                picked.append(i)
                break
    return picked


def _rewrite(section: dict, instruction: str, message: str, notice: Notice,
             profile: CompanyProfile, notice_text: str) -> str:
    """항목 하나를 지시대로 다시 쓴다. 실패하면 원래 글을 그대로 돌려준다."""
    body_excerpt = (f"\n[공고문 본문 발췌]\n{notice_text[:2000]}\n"
                    if notice_text else "")
    user = (f"[공고]\n제목: {notice.title}\n소관기관: {notice.agency}\n"
            f"개요: {notice.summary}\n{body_excerpt}\n"
            f"[회사 정보]\n{drafter._profile_block(profile)}\n\n"
            f"[고칠 항목]\n{section.get('title', '')}\n\n"
            f"[지금 본문]\n{section.get('body', '')}\n\n"
            f"[담당자가 한 말]\n{message}\n\n"
            f"[고칠 방향]\n{instruction}\n\n"
            f"위 본문을 고쳐 전체를 다시 출력하시오.")
    try:
        return hyperclova_api.chat(_EDIT_SYSTEM, user, max_tokens=3000,
                                   temperature=0.4,
                                   model=drafter.SECTION_MODEL).strip()
    except Exception as e:
        print(f"[알림] '{section.get('title')}' 수정 실패 — 원래 글을 둡니다. ({e})")
        return ""


def chat(notice: Notice, profile: CompanyProfile, sections: list[dict],
         message: str, history: list[dict] | None = None,
         notice_text: str = "") -> dict:
    """담당자의 말 한 마디를 받아 초안을 고친다.

    반환: {reply, sections, changed, unresolved}
      - sections : 고친 뒤의 전체 항목 (안 고친 항목은 받은 그대로)
      - changed  : 실제로 바뀐 항목 제목들 — 화면에서 표시해 주기 위한 것
    """
    history = history or []
    if not message.strip():
        return {"reply": "무엇을 고칠지 말씀해 주세요.", "sections": sections,
                "changed": [], "unresolved": []}
    if not hyperclova_api.is_configured():
        return {"reply": "AI가 설정되지 않아 대화로 고칠 수 없습니다.",
                "sections": sections, "changed": [], "unresolved": []}

    route = _route(message, sections, notice, history)
    if route.get("intent") != "수정":
        answer = (route.get("answer") or "").strip()
        return {"reply": answer or "어느 항목을 어떻게 고칠지 말씀해 주세요.",
                "sections": sections, "changed": [], "unresolved": []}

    picked = _resolve_targets(route.get("targets") or [], sections)
    if not picked:
        return {"reply": "어느 항목을 고칠지 찾지 못했습니다. 항목 이름과 함께 "
                         "다시 말씀해 주세요.",
                "sections": sections, "changed": [], "unresolved": []}

    trimmed = len(picked) > MAX_TARGETS
    picked = picked[:MAX_TARGETS]
    instruction = (route.get("instruction") or message).strip()

    # 근거로 인정할 글 — 초안 생성 때와 같은 기준을 쓴다.
    substituted = " ".join(drafter._format_value(attr, profile) or ""
                           for attr in set(drafter._PLACEHOLDERS.values()))
    grounded = (f"{drafter._profile_evidence(profile)} "
                f"{drafter._profile_block(profile)} {substituted} {notice_text}")

    out = [dict(s) for s in sections]
    changed: list[str] = []
    unresolved: list[str] = []
    flagged: list[str] = []
    for n, i in enumerate(picked):
        if n:
            time.sleep(drafter._CALL_GAP_SEC)
        title = out[i].get("title") or ""
        body = _rewrite(out[i], instruction, message, notice, profile, notice_text)
        if not body:
            continue

        # ── 초안 생성과 똑같은 안전장치를 통과시킨다.
        # 대화로 들어온 글이라고 느슨하게 두면, "매출 30% 늘었다고 써 줘" 한 마디로
        # 심사 지침이 탈락 사유로 못 박은 수치가 본문에 박힌다.
        body = drafter._clean_body(body, title)
        body, dropped = drafter.strip_unsupported_claims(body, profile)
        body, missing = drafter.fill_placeholders(body, profile)
        body, numbers = drafter.flag_unverified_numbers(body, grounded)
        if body.strip() == (out[i].get("body") or "").strip():
            continue
        out[i]["body"] = body
        changed.append(title)
        flagged.extend(d for d in dropped if d not in flagged)
        for key in missing:
            if key not in unresolved:
                unresolved.append(key)
        for num in numbers:
            label = f"근거 없는 수치: {num}"
            if label not in unresolved:
                unresolved.append(label)

    if not changed:
        return {"reply": "고치지 못했습니다. 조금 더 구체적으로 말씀해 주시겠어요?",
                "sections": sections, "changed": [], "unresolved": []}

    reply = f"‘{', '.join(changed)}’ 항목을 고쳤습니다."
    if instruction and instruction != message.strip():
        reply += f" ({instruction})"
    if trimmed:
        reply += (f" 한 번에 {MAX_TARGETS}개 항목까지만 고칩니다 — "
                  f"나머지도 바꾸려면 한 번 더 말씀해 주세요.")
    if flagged:
        reply += (f" 근거가 없어 [확인 필요]로 남긴 것: {', '.join(flagged)}."
                  f" 회사 프로필에 실제 내역을 넣으면 그대로 반영됩니다.")
    if unresolved:
        reply += f" 확인이 필요한 값: {', '.join(unresolved[:5])}."
    return {"reply": reply, "sections": out, "changed": changed,
            "unresolved": unresolved}
