"""HyperCLOVA X 응답을 담는 스키마.

`structurer.py`의 시스템 프롬프트가 지시하는 JSON 키와 1:1로 맞춘다. 키 이름에 공백이
들어가는 항목(`기업 부담금` 등)은 파이썬 식별자로 못 쓰므로 `alias`로 매핑한다.
`populate_by_name=True`라서 별칭(LLM 응답)·필드명(코드) 양쪽으로 넣고 뺄 수 있다.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvaluationItem(BaseModel):
    # 모델이 배점을 숫자(30)로 내놓는 경우가 잦아 문자열로 강제 변환한다.
    model_config = ConfigDict(populate_by_name=True, coerce_numbers_to_str=True)

    항목명: str | None = None
    배점: str | None = None
    세부내용: str | None = None


class NoticeExtraction(BaseModel):
    """구조화 2차 호출 결과 — 공고문 하나의 필드별 값."""

    model_config = ConfigDict(populate_by_name=True, coerce_numbers_to_str=True)

    사업명: str = ""
    주관기관: str | None = None
    신청기간: str = ""
    지원대상: str = ""
    신청제외대상_항목: list[str] = Field(default_factory=list)
    지원금액: str = ""
    기업_부담금: str = Field("", alias="기업 부담금")
    제출_서류: list[str] = Field(default_factory=list, alias="제출 서류")
    평가: list[EvaluationItem] = Field(default_factory=list)
    평가_비고: str | None = Field(None, alias="평가 비고")
    유의_사항: str | None = Field(None, alias="유의 사항")

    @model_validator(mode="before")
    @classmethod
    def _normalize_nulls(cls, data):
        """모델이 문자열 필드를 null 로, 리스트 필드를 null 로 내놓아도 견디게 한다."""
        if not isinstance(data, dict):
            return data
        str_keys = ("사업명", "신청기간", "지원대상", "지원금액", "기업 부담금", "기업_부담금")
        list_keys = ("신청제외대상_항목", "제출 서류", "제출_서류", "평가")
        out = dict(data)
        for k in str_keys:
            if k in out and out[k] is None:
                out[k] = ""
        for k in list_keys:
            if k in out and out[k] is None:
                out[k] = []
        return out


class FieldPageLocations(BaseModel):
    """구조화 1차(위치 탐색) 호출 결과 — 각 필드가 등장하는 페이지 번호들."""

    model_config = ConfigDict(populate_by_name=True)

    사업명: list[int] = Field(default_factory=list)
    신청기간: list[int] = Field(default_factory=list)
    지원대상: list[int] = Field(default_factory=list)
    신청제외대상_항목: list[int] = Field(default_factory=list)
    지원금액: list[int] = Field(default_factory=list)
    기업_부담금: list[int] = Field(default_factory=list, alias="기업 부담금")
    제출_서류: list[int] = Field(default_factory=list, alias="제출 서류")
    평가: list[int] = Field(default_factory=list)
    유의_사항: list[int] = Field(default_factory=list, alias="유의 사항")

    def all_pages(self) -> set[int]:
        pages: set[int] = set()
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, list):
                pages.update(int(v) for v in value)
        return pages
