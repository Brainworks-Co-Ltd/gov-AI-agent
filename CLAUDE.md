# gov-AI-agent

정부지원사업 공고를 모아 중복을 통합하고, 회사 프로필로 신청 자격을 판정하고,
신청서 초안을 써 주는 도구. 「2026 전남광주 청년 AI 솔버톤」 트랙 B-08 제출물.

## 제약

- **파이썬 표준 라이브러리만 사용한다.** `pip install` 불필요가 README 셀링 포인트다.
  새 의존성을 넣으려면 `requirements.txt` 와 README 3곳을 함께 고쳐야 한다.
- **빌드 단계가 없다.** `web/` 은 정적 파일 3개(html·css·js) + 폰트 하나다.
- LLM 은 네이버 하이퍼클로바X. `CLOVA_API_KEY` 환경변수.

## 디자인

`DESIGN.md` 가 규칙이다. 특히:

- 색 리터럴은 `:root` 안에만 존재한다. 검증:
  `awk '/^:root/,/^}/{next} /#[0-9a-fA-F]{3,6}\b/{print NR": "$0}' web/style.css`
- 한글 modifier 클래스명(`.badge.가능` 등)은 백엔드 값이라 바꿀 수 없다.
- 프리미티브 7종(`button` `.badge` `.card` `.grid` `.chip` `.field` `.box`)으로
  먼저 조합해 보고, 새 클래스명은 만들지 않는다.

## 테스트

```bash
python -X utf8 tests_eligibility_region.py
```

지역 요건 판정과 누락 검증의 회귀 테스트. 판정 로직을 고치면 여기부터 돌린다.

## 커밋 메시지

본문은 3줄 이내. 종결어미는 `~한다` 가 아니라 `~함.` 또는 명사형.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec
