"""파이프라인 조립 — 화면(serve.py)과 CLI(demo.py)가 함께 쓰는 진입점.

여기 있는 함수들은 "공고 하나를 붙잡고 담당자가 하는 일"과 1:1로 대응한다.

    eligibility_of()  이 공고, 우리가 신청할 수 있나?
    draft_of()        신청서 초안 좀 잡아줘
    check_of()        내기 전에 빠진 거 없는지 봐줘

DB 캐시를 여기서 다룬다. 판정과 초안은 LLM을 부르는 비싼 작업이라, 같은 공고를 다시
열 때마다 새로 부르면 화면이 느려지고 결과도 미묘하게 달라져 담당자가 혼란스럽다.
회사 프로필이 바뀌면 판정 캐시는 자동으로 무효가 된다(tools/store.get_verdict 참고).
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace

from agent import checker, drafter, eligibility, formspec
from agent.schemas import (CheckIssue, CompanyProfile, Draft, DraftSection,
                           EligibilityReport, Notice, Requirement, RequirementVerdict)
from tools import profile_store, store


def _cluster_siblings(conn: sqlite3.Connection, notice: Notice) -> list[Notice]:
    """같은 사업으로 통합된 다른 기관 공고들.

    중복 제거의 실익이 여기서 회수된다. 기업마당 공고에는 없는 '신청 제외대상'이
    K-Startup 쪽에만 적혀 있는 일이 흔한데, 둘을 함께 읽으면 그 결격 사유까지
    판정에 반영된다.
    """
    cluster = store.cluster_of(conn, notice.id)
    if not cluster:
        return []
    siblings = [store.get_notice(conn, i) for i in cluster["member_ids"]
                if i != notice.id]
    return [n for n in siblings if n is not None]


def _report_from_dict(d: dict) -> EligibilityReport:
    """캐시된 JSON을 다시 리포트 객체로. (화면·점검이 같은 형태를 쓰도록)"""
    rows = [RequirementVerdict(
        requirement=Requirement(axis=r["axis"], operator=r["operator"],
                                value=r["value"], quote=r["quote"]),
        verdict=r["verdict"], company_value=r["company_value"], reason=r["reason"])
        for r in d.get("rows", [])]
    return EligibilityReport(notice_id=d["notice_id"], overall=d["overall"], rows=rows,
                             required_docs=d.get("required_docs", []),
                             schedule=d.get("schedule", {}), note=d.get("note", ""))


def eligibility_of(conn: sqlite3.Connection, notice: Notice,
                   profile: CompanyProfile | None = None,
                   refresh: bool = False) -> EligibilityReport:
    """자격 판정. 캐시가 유효하면 재사용한다."""
    profile = profile or profile_store.load()
    phash = profile_store.hash_of(profile)

    if not refresh:
        cached = store.get_verdict(conn, notice.id, phash)
        if cached:
            return _report_from_dict(cached)

    report = eligibility.evaluate(notice, profile, _cluster_siblings(conn, notice))
    store.save_verdict(conn, notice.id, report.overall, report.to_dict(), phash)
    return report


def form_spec_of(conn: sqlite3.Connection, notice: Notice,
                 refresh: bool = False) -> dict:
    """첨부 서식에서 읽어낸 작성 항목·기입란·제출서류. 결과는 캐시한다.

    첨부를 내려받아 열고 LLM까지 부르는 작업이라, 같은 공고를 다시 열 때마다
    반복하면 안 된다. 서식 파일은 공고가 끝날 때까지 바뀌지 않으므로 기한도 두지 않는다.
    """
    if not refresh:
        cached = store.get_form_spec(conn, notice.id)
        if cached is not None:
            return cached

    # 첨부는 기업마당에만 있다. 통합된 공고의 대표가 K-Startup 쪽으로 뽑혔더라도
    # 서식을 놓치지 않도록, 같은 사업의 첨부를 모아서 넘긴다.
    merged = list(notice.attachments)
    have = {a["url"] for a in merged}
    for sibling in _cluster_siblings(conn, notice):
        merged += [a for a in sibling.attachments if a["url"] not in have]
        have |= {a["url"] for a in sibling.attachments}

    spec = formspec.of_notice(replace(notice, attachments=merged))
    store.save_form_spec(conn, notice.id, spec)
    return spec


def draft_of(conn: sqlite3.Connection, notice: Notice,
             profile: CompanyProfile | None = None,
             refresh: bool = False, sections: list[str] | None = None) -> Draft:
    """신청서 초안. 이미 만들어 둔 게 있으면 그대로 돌려준다.

    초안은 담당자가 손대는 물건이라, 화면을 다시 열었다고 멋대로 새로 만들면
    고쳐 둔 내용이 날아간다. 다시 만들려면 refresh=True 를 명시해야 한다.

    항목은 **첨부된 신청서 서식에서 읽어낸 것**을 우선한다. 서식을 못 읽으면(PDF
    서식이거나 첨부가 없으면) 공고 본문에서 추측한다 — 그 경우 담당자가 서식을 열어
    항목을 맞춰야 하므로, 어느 쪽이었는지 note 로 알려 준다.
    """
    profile = profile or profile_store.load()
    # 담당자가 항목을 직접 지정하면 캐시를 건너뛰고 그 항목으로 새로 쓴다.
    if not refresh and not sections:
        saved = store.get_draft(conn, notice.id)
        if saved:
            d = saved["draft"]
            return Draft(notice_id=d["notice_id"],
                         sections=[DraftSection(**s) for s in d["sections"]],
                         unresolved=d.get("unresolved", []), note=d.get("note", ""),
                         form_file=d.get("form_file", ""),
                         form_fields=d.get("form_fields", []))

    spec = form_spec_of(conn, notice)
    chosen = sections or spec.get("write_sections") or None
    draft = drafter.generate(notice, profile, chosen,
                             notice_text=spec.get("notice_text", ""))
    draft.form_file = spec.get("source_file", "")
    draft.form_fields = spec.get("fill_fields", [])

    if sections:
        hint = "담당자가 지정한 항목으로 다시 작성했습니다."
    elif chosen and draft.form_file:
        hint = f"항목을 첨부 서식 '{draft.form_file}' 에 맞췄습니다."
    else:
        why = spec.get("note", "").strip()
        hint = ("서식 항목을 읽지 못해 기본 구성으로 만들었습니다. "
                "실제 양식을 확인하셨다면 '작성 항목 고치기'로 항목을 바꿔 다시 쓸 수 "
                "있습니다.")
        if why:
            hint += f" ({why})"
    draft.note = f"{draft.note} {hint}".strip()

    store.save_draft(conn, notice.id, draft.to_dict())
    return draft


def save_edited_draft(conn: sqlite3.Connection, notice: Notice,
                      sections: list[dict], unresolved: list[str]) -> None:
    """대화로 고친 초안을 저장한다.

    저장해 두지 않으면 화면을 닫는 순간 사라진다 — 담당자 입장에서는 고쳐 달라고
    말해서 고쳐진 글이 없어지는 것이라, 대화 기능 자체를 못 믿게 된다.

    서식 정보(form_file·form_fields)는 저장돼 있던 것을 그대로 이어 쓴다. 본문만
    바뀌었을 뿐 어느 서식에 옮겨 담을지는 달라지지 않았다.
    """
    saved = store.get_draft(conn, notice.id) or {}
    previous = saved.get("draft") or {}
    draft = Draft(
        notice_id=notice.id,
        sections=[DraftSection(title=s.get("title", ""), body=s.get("body", ""),
                               sources=s.get("sources") or [])
                  for s in sections],
        unresolved=unresolved,
        note=previous.get("note", ""),
        form_file=previous.get("form_file", ""),
        form_fields=previous.get("form_fields", []),
    )
    store.save_draft(conn, notice.id, draft.to_dict())


def required_docs_of(conn: sqlite3.Connection, notice: Notice,
                     profile: CompanyProfile | None = None) -> list[str]:
    """이 공고가 요구하는 제출서류 목록.

    오픈API 응답에는 사실상 없다(기업마당 reqstMthPapersCn는 접수처 안내문이다).
    진짜 목록은 첨부(공고문·서식) 안에 있으므로 여기서 합친다.
    """
    report = eligibility_of(conn, notice, profile)
    docs = list(report.required_docs)
    for doc in form_spec_of(conn, notice).get("documents", []):
        if doc not in docs:
            docs.append(doc)
    return docs


def checklist_of(conn: sqlite3.Connection, notice: Notice,
                 profile: CompanyProfile | None = None) -> dict:
    """제출서류 체크리스트. 담당자가 체크해 둔 상태를 얹어 돌려준다."""
    profile = profile or profile_store.load()
    report = eligibility_of(conn, notice, profile)
    report.required_docs = required_docs_of(conn, notice, profile)
    return checker.document_checklist(report, profile, notice,
                                      store.get_doc_checks(conn, notice.id))


def check_of(conn: sqlite3.Connection, notice: Notice, draft: Draft,
             profile: CompanyProfile | None = None) -> list[CheckIssue]:
    """작성한 글 점검 — 수치 불일치·표기 오류. 서류는 checklist_of 가 맡는다."""
    profile = profile or profile_store.load()
    issues = checker.run(draft, profile)
    store.save_draft(conn, notice.id, draft.to_dict(),
                     [i.__dict__ for i in issues])
    return issues


def notice_view(conn: sqlite3.Connection, notice: Notice,
                verdicts: dict[str, str] | None = None) -> dict:
    """공고 목록 카드 하나에 필요한 정보 묶음.

    중복 통합된 공고는 '어느 기관들에서 왔는지'와 '왜 합쳤는지'를 함께 실어 보낸다.
    합쳐 놓고 근거를 안 보여주면 담당자가 도구를 못 믿는다.
    """
    cluster = store.cluster_of(conn, notice.id)
    sources = [notice.source]
    # 기관별 원문 링크. 통합된 공고는 양쪽 다 실어 보낸다 — 담당자가 "이 판정이 어느
    # 공고를 보고 나온 건가"를 원문에서 직접 확인할 수 있어야 도구를 믿는다.
    source_links = [{"source": notice.source, "url": notice.url}] if notice.url else []
    reason = ""
    attachments = list(notice.attachments)
    if cluster:
        reason = cluster["reason"] or ""
        for mid in cluster["member_ids"]:
            src = mid.split(":", 1)[0]
            if src not in sources:
                sources.append(src)
            # 첨부는 기업마당에만 있다. 대표가 K-Startup 쪽으로 뽑힌 경우에도
            # 서식 파일을 잃지 않도록 통합된 공고들의 첨부를 모아 준다.
            if mid != notice.id:
                sibling = store.get_notice(conn, mid)
                if sibling:
                    have = {a["url"] for a in attachments}
                    attachments += [a for a in sibling.attachments
                                    if a["url"] not in have]
                    if sibling.url and not any(l["url"] == sibling.url
                                               for l in source_links):
                        source_links.append({"source": sibling.source,
                                             "url": sibling.url})

    data = notice.to_dict()
    data.pop("raw", None)               # 원본 JSON은 화면에 필요 없다 (응답만 무거워짐)
    data["attachments"] = attachments
    data["sources"] = sources
    data["source_links"] = source_links
    data["merged"] = len(sources) > 1
    data["merge_reason"] = reason
    data["verdict"] = (verdicts or {}).get(notice.id)
    return data
