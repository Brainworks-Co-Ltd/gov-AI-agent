# DESIGN.md — 디자인 시스템

정부지원사업 공고 분석·신청서 초안 생성 도구의 시각 규칙.

**기반**
- 색: [Radix Colors](https://www.radix-ui.com/colors) 커스텀 팔레트 (accent `blue`, `gray`, background)
- 토큰 규약: [shadcn/ui theming](https://ui.shadcn.com/docs/theming) 의 `X` / `X-foreground` 쌍
- 컴포넌트 참조: [TailGrids Tailwind UI Components (Figma)](https://www.figma.com/design/FdanxKiecw9emStGvkqk5p/Tailwind-UI-Components-for-Figma-%7C-TailGrids--Community-?node-id=310-16978)

구현체는 `web/style.css` 하나다. 빌드 도구도, CSS 프레임워크도 쓰지 않는다.

---

## 0. 이 문서가 정하는 것

| 정한다 | 정하지 않는다 |
|---|---|
| 색 토큰과 그 위에 올라갈 글자색 | 개별 화면 레이아웃 |
| 타이포·여백·라운드 스케일 | 카피 문구 |
| 프리미티브 7종과 사용 규칙 | 애니메이션 (현재 transition 외 없음) |
| 절대 규칙 4개 (§6) | 반응형 브레이크포인트 — 발표가 통합 PC 1대 단일 뷰포트라 현재 범위 밖 |

---

## 1. 3층 구조

```
① 원시 스케일    Radix blue 1-12 / gray 1-12 (+ alpha, P3)
                 값 그 자체. 화면 코드에서 직접 쓰지 않는다.
       ↓
② 시맨틱 별칭    background / card / primary / muted / border / ring …
                 shadcn 규약. 화면 코드는 이 층만 참조한다.
       ↓
③ 프리미티브     button · .badge · .card · .grid · .chip · .field · .box
                 ②의 토큰만 조합한다.
       ↓
④ 화면별         .cards · .verdict-summary · .issues · .form …
                 ③을 조합한다. 색 리터럴 금지.
```

**경계 규칙**: 각 층은 바로 위 층만 참조한다. ④에서 `--blue-10` 을 직접 쓰면 안 되고
`--primary` 를 쓴다. ②에서만 원시 스케일을 만진다.

---

## 2. 시맨틱 토큰 (shadcn 규약)

**핵심 규칙 — 표면은 자기 위의 글자색을 함께 선언한다.** `--primary` 를 배경으로 쓰면
글자는 반드시 `--primary-foreground` 다. 글자색을 따로 고르는 순간 대비가 깨진다.

> 이 규칙이 이 프로젝트에서 실제로 막아준 것: `gray-9`(#727eb7)는 흰 글자 대비가
> **3.90:1** 로 AA 미달이다. shadcn 관점에서 gray-9 는 **유효한 foreground 가 없으므로
> 애초에 표면이 아니다.** 그래서 이 표에 없다.

| 토큰 | 값 | 원시 | 대비 | 쓰는 곳 |
|---|---|---|---|---|
| `--background` | `#f4f7ff` | Radix background | — | 페이지 바탕 |
| `--foreground` | `#181d3d` | gray-12 | **15.29:1** | 기본 글자, 제목 |
| `--card` | `#ffffff` | (순백 고정, §6-3) | — | 카드·패널 표면 |
| `--card-foreground` | `#181d3d` | gray-12 | **16.38:1** | 카드 안 글자 |
| `--primary` | `#445ae6` | blue-10 | — | 주요 버튼, 브랜드 |
| `--primary-foreground` | `#ffffff` | — | **5.45:1** | primary 위 글자 |
| `--secondary` | `#e1e6f9` | gray-3 | — | 보조 채움, 카운트 배지 |
| `--secondary-foreground` | `#181d3d` | gray-12 | 13.17:1 | |
| `--muted` | `#edf0fb` | gray-2 | — | 표 헤더, 인셋 영역 |
| `--muted-foreground` | `#4d5783` | gray-11 | **6.14:1** | 보조 텍스트, 라벨 |
| `--accent` | `#e3e8f5` | blue-3 | — | 선택·활성 상태 (탭, D-day 칩) |
| `--accent-foreground` | `#3749c5` | blue-11 | **5.86:1** | accent 위 글자, 링크 |
| `--border` | `#c3ccf1` | gray-6 | 1.48:1 (장식) | 카드 테두리, 구분선 |
| `--input` | `#c3ccf1` | gray-6 | — | 입력 컨트롤 테두리 |
| `--ring` | `#4f68f5` | blue-9 | **4.23:1** | 포커스 링 |
| `--radius-md` | `12px` | — | — | 기준 라운드 (§5) |

**보조 표면**
`--border-hover` = gray-8 `#9eace8` (버튼 hover 테두리) · `--primary-hover` = blue-11 `#3749c5`

### 왜 이 값들인가 (실측 근거)

- **`--primary` 가 blue-10 인 이유** — blue-9(#4f68f5) 위 흰 글자는 4.53:1 로 통과하지만
  여유가 0.03 이다. 폰트 렌더링 편차를 감안해 5.45:1 인 blue-10 을 쓴다. blue-9 는
  글자를 얹지 않는 `--ring` 전용.
- **`--ring` 이 파란색인 이유** — gray 스케일 전체에서 background 대비 비텍스트 기준
  3:1 을 넘는 값이 없다 (gray-8 이 2.06:1 로 최대). 회색 포커스 링은 만들 수 없다.
- **`--card` 가 순백인 이유** — §6-3 참조.

---

## 3. 판정 축 (프로젝트 고유)

shadcn 에는 없는 축이다. 자격 판정 결과 3분류 + 미판정 4상태를 표현한다.
**shadcn 의 `X`/`X-foreground` 규약을 그대로 따른다.**

| 상태 | surface | foreground | border | 대비 |
|---|---|---|---|---|
| **가능** | `#e9f9ee` | `#193b2d` | `#b4dfc4` | 11.29:1 |
| **불가** | `#ffefef` | `#641723` | `#f9c6c6` | 11.16:1 |
| **확인필요** | `#fff7c2` | `#4f3422` | `#e9c162` | 10.47:1 |
| **미판정** | `#edf0fb` | `#4d5783` | `#d6ddf6` | **6.14:1** |

**step3 배경 + step12 글자** 조합이다. Radix 기본 step11 을 글자로 쓰면 이 배경에서
4.5:1 을 아슬하게 미달한다 (green 4.40, amber 4.30).

솔리드 색(아이콘·좌측 막대 전용, 글자 금지):
`--ok-solid` `#30a46c` · `--no-solid` `#e5484d` · `--chk-solid` `#ffc53d`

> **미결** — green·red·amber 는 Radix **기본** 스케일이다. blue·gray 처럼 이 배경
> (`#f4f7ff`)에 맞춰 커스텀 생성하지 않았다. 지금도 대비는 통과하지만 톤이 미세하게
> 겉돈다. 여유가 생기면 Radix 커스텀 생성기로 같은 background 를 넣고 재생성한다.

---

## 4. 타이포

**본문 폰트: Pretendard Variable.** `web/PretendardVariable.woff2` 로 자체 호스팅한다.
CDN 을 쓰지 않는 이유는 서브셋 CDN 이 조각 수백 개를 받는 구조라 네트워크가 불안정한
현장에서 통째로 실패하기 때문이다. 폴백은 맑은 고딕 → Apple SD Gothic Neo → system-ui.

### 크기 스케일 (6단)

| 이름 | 값 | 쓰는 곳 |
|---|---|---|
| `xs` | 11px | 배지, 태그, 캡션, 서류 힌트 |
| `sm` | 12px | 보조 텍스트, 메타 줄, 라벨, 로그 |
| `base` | 13px | 본문, 표 셀, 버튼, 입력 |
| `md` | 15px | 카드 제목, 강조 수치 |
| `lg` | 18px | 섹션 제목 (h2) |
| `xl` | 21px | 페이지 제목 (h1) |

> **현재 코드는 10종을 쓰고 있다** — 11 / 12 / 12.5 / 13 / 13.5 / 14 / 14.5 / 15 / 18 / 20.
> 12~14.5 구간에 6종이 몰려 있고, 12px 과 12.5px 은 눈으로 구분되지 않는다.
> 위 6단으로 정리하면 12.5 → 12, 13.5 → 13, 14 → 13 또는 15, 14.5 → 15, 20 → 21 이다.
> **아직 반영 전.**

### 굵기

| 값 | 쓰는 곳 |
|---|---|
| 500 | 기본 본문 강조 없음 |
| 600 | 라벨, 보조 제목, 버튼 |
| 700 | 카드 제목, 배지, 표 헤더 |
| 800 | 페이지 제목, D-day 숫자 |

### 자간

한글은 기본 자간이 넓다. 제목급에 음수 자간을 준다.
`h1` `-0.03em` · `h2`·카드 제목 `-0.02em` · 본문 0 · D-day 등 숫자 강조 `-0.04em`

---

## 5. 여백 · 라운드

### 여백 스케일 (6단, 8px 기반)

| 이름 | 값 | 쓰는 곳 |
|---|---|---|
| `2xs` | 2px | 배지 세로 패딩 등 미세 조정 |
| `xs` | 4px | 인접 요소 간 최소 간격 |
| `sm` | 8px | 컨트롤 내부 패딩, 인라인 갭 |
| `md` | 12px | 카드 목록 간격, 폼 행 간격 |
| `lg` | 16px | 카드 내부 패딩 |
| `xl` | 24px | 섹션 간 간격, 페이지 좌우 여백 |

> **현재 코드는 12종을 쓰고 있다** — 1·2·3·4·5·6·7·8·10·11·12·14.
> 8px 이 10회로 사실상 기준값이고 14px 이 8회다. 위 6단으로 정리하면
> 1·3 → 2 또는 4, 5·6·7 → 4 또는 8, 10·11 → 12, 14 → 16 이다. **아직 반영 전.**

### 라운드

| 토큰 | 값 | 쓰는 곳 |
|---|---|---|
| `--radius-xs` | 6px | 칩, 태그, 작은 kind 배지, 인용 블록, 포커스 링 |
| `--radius-sm` | 8px | 버튼, 입력, 탭 |
| `--radius-md` | 12px | 카드, 패널, 표 |
| `--radius-pill` | 999px | 판정 배지 |

`border-radius` 에 생값을 쓰지 않는다. 검증: `grep -n 'border-radius: *[0-9]' web/style.css`
출력이 비어야 한다.

### 그림자 (2단)

```css
--shadow-card: 0 1px 2px rgba(24,29,61,.04), 0 1px 3px rgba(24,29,61,.03);
--shadow-lift: 0 4px 12px rgba(24,29,61,.07), 0 1px 3px rgba(24,29,61,.04);
```
`card` 는 기본 표면, `lift` 는 hover·활성 상태. 3단계 이상은 만들지 않는다.

---

## 6. 절대 규칙

### 6-1. 한글 modifier 클래스명은 바꿀 수 없다

`.badge.가능` `.badge.불가` `.badge.확인필요` `.badge.미판정` ·
`.issue.오류` `.issue.경고` `.issue.정보` · `.kind.서식` `.kind.공고문` ·
`.checklist .kind.보유` `.발급` `.작성`

`web/app.js` 가 **백엔드 값을 그대로 className 에 넣어** 이 클래스를 만든다
(`app.js:107`, `:197`, `:288`, `:56`). BEM 같은 규칙으로 바꾸려면 `app.js` 에
매핑 함수를 넣어야 하고, 그건 스타일 변경이 아니라 로직 변경이다.

**한글 이름을 그대로 두고, 베이스 클래스(`.badge` `.issue` `.kind`)에만 공통 규칙을
건다.** 판정 문자열은 `agent/schemas.py` 의 `VERDICT_OK` / `VERDICT_NO` /
`VERDICT_CHECK` 상수와 1:1이다.

### 6-2. 색 리터럴은 `:root` 안에만 존재한다

③·④ 층에서 `#` 으로 시작하는 값을 쓰지 않는다. 검증:

```bash
awk '/^:root/,/^}/{next} /#[0-9a-fA-F]{3,6}\b/{print NR": "$0}' web/style.css
```

출력이 비어야 한다.

### 6-3. 카드 표면은 순백 고정

`--background`(#f4f7ff, L 0.9297)가 `gray-1`(#f2f3f9, L 0.8982)보다 **밝다.**
Radix 관례대로 gray-1 을 카드에 쓰면 **카드가 페이지보다 어두워져** 떠 보이지 않는다.
`--card` 는 `#ffffff` 다.

### 6-4. 프리미티브는 새 클래스명을 만들지 않는다

`button` `.badge` `.card` `.grid` `.chip` `.field` `.box` — 전부 **이미 존재하는
셀렉터**다. 여기에 규칙을 붙이면 `index.html` 과 `app.js` 의 className 을 건드리지
않아도 된다. 새 컴포넌트가 필요하면 먼저 이 7개로 조합할 수 있는지 확인한다.

---

## 7. 프리미티브 7종

| 셀렉터 | 역할 | 상태 |
|---|---|---|
| `button` | 모든 버튼. `.primary` 로 강조 | hover(테두리/배경) · disabled(opacity .55) · focus-visible(ring) |
| `.badge` | 판정 3분류 + 미판정 pill | 상태별 modifier (§6-1) |
| `.card` | 공고 목록 항목 | hover(lift + translateY -1px) |
| `.grid` | 데이터 표 | 헤더 `--muted` 배경 |
| `.chip` | 값 표시용 작은 태그 | 없음 |
| `.field` | 라벨 + 입력 세로 묶음 | focus-within |
| `.box` | 흰 상자 (패널·폼·섹션 공유) | 없음 |

**포커스**: `:focus-visible` 에 `outline: 2px solid var(--ring); outline-offset: 2px`
를 전역으로 건다. 개별 컴포넌트에서 포커스를 지우지 않는다.

---

## 8. 알려진 미결

| | 항목 |
|---|---|
| 1 | **타이포 10종 → 6단, 여백 12종 → 6단, radius 생값 7곳** 정리 미반영 (§4, §5) |
| 2 | green·red·amber 가 Radix 기본 스케일 — 이 background 에 맞춘 커스텀 생성 미적용 (§3) |
| 3 | 반응형 브레이크포인트 미정의 — 현재 `@media (max-width: 900px)` 한 개뿐. 발표가 단일 뷰포트라 우선순위 밖 |
| 4 | 다크 모드 없음. 계획 없음 |
| 5 | `.badge` pill 의 `999px` 이 토큰 아님 |

### shadcn 규약 밖의 확장 토큰

§2 는 `web/style.css` 와 **이름까지 일치한다**. 다만 shadcn 표준에 없는 토큰 4개를
확장으로 둔다:

| 토큰 | 값 | 이유 |
|---|---|---|
| `--border-subtle` | gray-4 `#d6ddf6` | 헤더·탭 하단처럼 카드 테두리보다 옅어야 하는 구분선 |
| `--border-hover` | gray-8 `#9eace8` | 버튼 hover 테두리 |
| `--primary-hover` | blue-11 `#3749c5` | primary hover |
| `--radius-xs` / `--radius-pill` | 6px / 999px | 작은 배지·칩, pill 배지 |

**텍스트 계층은 2단이다.** `--foreground`(gray-12) 와 `--muted-foreground`(gray-11).
3단째로 gray-10(`#6874a9`)을 쓰던 시기가 있었으나 **background 위 4.21:1, muted 면 위
3.96:1 로 AA 미달**이라 폐기했다. 세 번째 위계가 필요하면 **색이 아니라 크기·굵기로**
구분한다 (메타 텍스트는 이미 11~12px, 본문은 13px).
