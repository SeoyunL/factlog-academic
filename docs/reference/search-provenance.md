# 어휘 · 검색 · 출처 추적

> 🌐 [English](search-provenance.en.md) | **한국어**

## 어휘 살펴보기 (`factlog vocab`)

`ask` 와 `provenance` 는 정확한 엔티티/관계 이름을 필요로 합니다. `factlog vocab`
은 이를 나열합니다 — 사용 횟수와 함께 엔티티 이름과 관계 이름을 보여 주므로
무엇을 질의할 수 있는지 알 수 있습니다.

```bash
factlog vocab              # entities + relations (engine facts)
factlog vocab --entities   # just entities
factlog vocab --relations  # just relations (tagged [attribute] / [single-valued] / [typed:<type>])
factlog vocab --all        # include non-engine names (candidate/needs_review/superseded)
```

선언된 속성(attribute) 관계의 객체는 엔티티가 아니라 리터럴이므로 엔티티 목록에서
제외됩니다(`status` 와 동일한 타이핑).

## 사실 찾기 (`factlog search`)

정확한 이름을 모를 때는 `factlog search <term>` 이 주어/관계/객체 전체에 대해
대소문자 구분 없는 부분 문자열 매칭을 수행하고, 일치하는 사실을(상태와 출처 수와
함께) 나열합니다. `vocab` 은 이름을 나열하고, `search` 는 조각으로 사실을 찾으며,
`provenance` 는 정확한 트리플을 추적합니다.

```bash
factlog search fastapi   # case-insensitive; matches 'FastAPI'
factlog search acme      # partial — every fact mentioning the fragment
```

## 사실의 출처 추적 (`factlog provenance`)

모든 사실은 자신이 추출된 소스를 기록합니다. `factlog provenance`(별칭 `trace`)
는 일치하는 사실에 대해 그 근거가 되는 모든 행을 나열합니다 — **소스 경로, 상태,
신뢰도, 노트(추출된 발췌문), 그리고 소스 파일이 디스크에 없을 때의 `[stale]`
표시**까지. (`superseded`/`needs_review` 를 포함한) 모든 상태가 표시되므로,
폐기된 근거도 계속 보입니다.

```bash
factlog provenance Acme uses FastAPI   # exact triple
factlog provenance Acme uses           # all objects for (subject, relation)
factlog provenance Acme                # all facts about a subject
factlog provenance - uses              # relation only ('-' wildcards a position)
factlog provenance - - FastAPI         # object only
```

위치 인자는 `(subject, relation, object)` 접두사입니다. 리터럴 `-` 는 해당 위치를
와일드카드 처리하며, 생략된 뒤쪽 위치도 와일드카드로 취급됩니다(와일드카드가 아닌
항목이 최소 하나는 있어야 합니다). 공백이 포함된 항목은 따옴표로 감싸십시오.

`/factlog ask` 역시 검증된 엔진 답변 아래에 각 근거 소스 경로(`← <source>`)를
나열하므로, 쿼리로 찾은 사실을 그 자리에서 추적할 수 있습니다.
