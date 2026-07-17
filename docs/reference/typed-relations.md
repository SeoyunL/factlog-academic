# 타입 지정 관계 (`policy/typed-relations.md`)

> 🌐 [English](typed-relations.en.md) | **한국어**

어떤 관계의 리터럴 객체는 단순 매칭이 아니라 **비교**되어야 합니다 — 그래야
결정론적 엔진이 그 값을 정렬(ordering)하거나 임계값(threshold)으로 거르거나
범위(range)로 질의할 수 있습니다(예: "2030년 이후 출시", "순위 <= 3"). 그런
관계는 `policy/typed-relations.md` 에 선언합니다. 객체가 리터럴이므로 해당 관계는
`policy/attribute-relations.md` 에도 함께 선언해야 합니다.

한 줄에 하나씩 선언합니다:

```
- `relation name` : <type> as <ascii_alias>
```

`<ascii_alias>` 는 비교 가능한 값을 담는 엔진 사이드 관계의 이름으로, 관계 이름이
비ASCII 여도 합법적인 엔진 식별자로 남도록 작성자가 직접 고르는 ASCII 식별자
(`[A-Za-z_][A-Za-z0-9_]*`)입니다. 공백이 포함된 관계 이름은 백틱으로 감쌉니다.

네 가지 타입:

- `date` — `2030.1` / `2030-01-15` → 정렬 가능한 yyyymmdd. **엔진 투영 지원**
  (정렬/임계값/범위).
- `ordinal` — `rank 3` / `3rd` → 정수 순위. **엔진 투영 지원**.
- `amount` — `100억` / `1,000원` → 정수 기본 단위. **엔진 투영 지원**. 단위 표가
  필요하며, 줄 끝에 인라인으로 줄 수 있습니다: `: amount as <alias> (억=1e8, 만=1e4, 원=1)`
  (값은 양의 정수). 절을 생략하면 기본 단위 표를 씁니다.
- `number` — `1,000` / `3.5` → 수치 크기. **엔진 투영 지원**: 정렬 가능한
  int64 로 ×1000 스케일됩니다(소수점 3자리). ⚠️ 비교 술어의 임계값은 반드시
  **스케일된 단위**로 적어야 합니다: `version >= 2.0` → `version_num(S, V), V >= 2000`.
  소수 3자리를 넘는 정밀도는 반올림됩니다(ROUND_HALF_UP).

추출기는 타입 지정 리터럴 객체를 구조가 보존되는 compact compound term으로 써도
됩니다: `date(2030,1)`, `date(2030,1,15)`, `number(2.5)`, `ordinal(3)`,
`amount(100,"억")`. `relation/3` 의 객체는 이 term 문자열을 그대로 보관하고,
타입 지정 사이드 관계가 비교 가능한 스칼라로 투영합니다.

`factlog vocab` 은 선언된 타입 지정 관계에 `[typed:<type>]` 태그를 붙여 보여
줍니다(예: `[attribute, typed:date]`).
