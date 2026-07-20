# sample-kb — 빠른 시작 튜토리얼

`sample-kb` 는 자기 데이터 없이 factlog를 처음부터 끝까지 한 번 완주해 볼 수 있는
**최소 예제 지식베이스(KB)** 입니다. 이미 소스 한 개와 후보/accepted 사실이 들어
있어서, 이미 추출된 고정 후보에서 시작해 **사람의 검토 → 승인 → 엔진 검증 →
출처 추적** 흐름을 따라갈 수 있습니다. 이때 아래 예시 출력은 **복사본에 동봉된
고정 데이터**(candidates.csv·query.dl)를 대상으로 하는 결정적 단계
(review/status/accept/check)에서 정확히 재현됩니다. `/factlog sync`·`/factlog
query`·`/factlog ask` 같은 LLM 슬래시 단계는 다시 돌리면 결과가 달라질 수 있어
예시와 어긋날 수 있습니다(정상 — 비결정 추출 경계). 소스에서 후보를 직접 다시
뽑는 `/factlog sync` 는 완주 뒤 선택 절에서 체험합니다. (이 KB는 smoke test도
사용합니다.)

이 튜토리얼은 한 번의 성공 흐름을 끝까지 따라가는 것이 목표입니다. 각 단계마다
명령을 **어디서 실행하는지** 를 명시합니다. factlog의 명령은
[개념 문서](../../docs/guide/concepts.md#명령-한눈에-보기--slash-command--cli-command--kb-파일) 의
구분표대로 두 계층으로 나뉩니다.

- **Claude Code slash command** — Claude Code 세션 안에서 `/factlog ...` 로 실행.
  소스를 읽어 후보를 추출하고, 쿼리 초안을 잡고, 엔진 검증 흐름을 호출합니다(검증
  자체는 결정론 엔진이 수행).
- **Python CLI command** — 터미널(shell)에서 `factlog ...`(또는 `python3 -m
  factlog ...`)로 실행. KB 상태를 확인하고, 후보를 **사람이** 검토·승인·추적합니다.

> 한 가지 원칙을 기억하세요: **에이전트는 결론을 내리지 않습니다.** `sync` 는
> 후보만 만들고, `logic_report.txt` 는 엔진이 생성하며, `accept`/`reject` 는
> 자동 단계가 아니라 **사람의 게이트** 입니다.

## 0. 복사본에서 실습하기 (원본은 그대로 둡니다)

원본 `examples/sample-kb` 는 **초기 상태로 남겨 둡니다**(smoke test가 이 상태를
기대합니다). 실습은 항상 **복사본** 에서 하세요. factlog 저장소 루트에서:

> ⚠️ **주의:** `factlog use` 는 전역 설정
> (`${XDG_CONFIG_HOME:-~/.config}/factlog/config.json`)의 **활성 KB를 바꾸고
> 기존 값을 덮어씁니다.** 이미 쓰던 KB가 있다면, 시작하기 전에 현재 활성 KB
> 경로를 적어 두세요(아래 정리 절에서 복원합니다). 전역 설정을 아예 건드리고
> 싶지 않다면 매 명령에 `--target ./factlog-demo` 를 붙여 `use` 를 건너뛰어도
> 됩니다.

*터미널에서 실행:*

```bash
factlog where                             # (선택) 시작 전 현재 활성 KB 경로를 적어 둔다
cp -R examples/sample-kb ./factlog-demo   # 원본을 건드리지 않는 실습용 복사본
factlog use ./factlog-demo                # 이 복사본을 활성 KB로 지정
factlog where                             # 출력된 활성 KB 절대경로가 복사본을 가리키는지 확인
```

`factlog use` 이후로는 어느 디렉터리에서 실행하든 모든 명령이 이 복사본을
대상으로 동작합니다(`--target` 불필요). 아래 모든 단계는 이 복사본 위에서
일어나며, **원본 `examples/sample-kb` 는 바뀌지 않습니다.**

## 1. 복사본 안을 둘러보기

복사본에는 다음이 이미 들어 있습니다.

| 위치 | 내용 |
|------|------|
| `sources/example.md` | 추출의 원본이 되는 마크다운 소스(Claude Code 개요 한 편). |
| `facts/candidates.csv` | 추출된 **후보 사실** — `confirmed` 7개 + `needs_review` 1개(`Anthropic, develops, Claude Code`). |
| `facts/accepted.dl` | 엔진 입력 — `confirmed`/`accepted` 사실만 컴파일된 결과(현재 7개). |
| `facts/query.dl` | `policy/questions.md` 로부터 만든 Datalog 쿼리 초안. |
| `facts/logic_report.txt` | **엔진이 생성한** 로직 체크 리포트(사람이 편집하지 않음). |
| `policy/questions.md` | 자연어 연구 질문 목록(q1–q6) — `query.dl` 의 q1–q6과 1:1. |

핵심은 `candidates.csv` 의 마지막 행입니다. `Anthropic, develops, Claude Code`
는 `needs_review` 상태라서 **아직 엔진 입력(`accepted.dl`)에 들어 있지
않습니다.** 이것이 곧 우리가 사람으로서 검토·승인할 후보입니다.

복사본에는 이 후보가 이미 들어 있으므로, **새로 추출하지 않고** 곧장 검토·승인
흐름으로 들어갑니다(그래서 아래 예시 출력이 정확히 재현됩니다). 소스에서 후보를
직접 다시 뽑아 보는 `/factlog sync` 는 완주 뒤 **(선택) 재추출 체험** 절에서
다룹니다.

## 2. 대기 중인 후보 검토하기 — `factlog review` / `factlog status`

이제 사람이 검토할 차례입니다. **읽기 전용** 명령으로 무엇이 대기 중인지 봅니다.

*터미널에서 실행:*

```bash
factlog review     # 대기 큐(candidate + needs_review) 나열
factlog status     # KB 상태 요약: 상태별 사실 수, 어휘, 엔진, 리포트 신선도
```

`factlog review` 는 대기 중인 한 줄을 보여 줍니다.

```
  Anthropic / develops / Claude Code
    ← sources/example.md#what-is-claude-code  [needs_review, conf 0.90]
        note: inferred from developed_by relation
  decide with: factlog accept <subject> <relation> <object>   (or: factlog reject ...)
```

`factlog status` 의 `facts:` 줄은 `confirmed=7, needs_review=1`, 그리고 `7 engine
fact(s)` 를 보고합니다 — 즉 대기 중인 후보는 **아직 엔진 입력이 아닙니다.**

## 3. 사람의 게이트 — `factlog accept`

후보를 살펴보고 받아들이기로 결정했다면 승인합니다. 이 단계는 **자동이 아니라
사람의 판단** 이며, factlog의 신뢰 경계입니다.

*터미널에서 실행:*

```bash
factlog accept Anthropic develops "Claude Code"   # needs_review → accepted
```

(받아들이지 않을 후보라면 같은 자리에서 `factlog reject <subject> <relation>
<object>` 로 폐기합니다 — 감사용으로 `superseded` 로 남습니다.)

`accept` 는 후보의 상태를 `accepted` 로 바꾸고 `facts/accepted.dl` 을
재컴파일합니다. 이제 다시 확인하면 큐가 비어 있고 엔진 입력이 8개로 늘어난 것을
볼 수 있습니다.

```bash
factlog review    # → no pending facts
factlog status    # → facts: 8 ... [confirmed=7, accepted=1]; 8 engine fact(s)
```

## 4. 질문을 쿼리로, 그리고 엔진 검증 — `/factlog query` · `/factlog check`

승인된 사실이 준비됐으니 질문에 답할 차례입니다. 자연어 질문은 이미
`facts/query.dl` 초안으로 복사본에 동봉돼 있고, 엔진이 그 초안을 컴파일·로직
체크합니다.

*Claude Code 세션에서 실행:*

```
/factlog check     # accepted 사실 컴파일 + 로직 체크 + 엔진 리포트 표시
/factlog query     # (선택) policy/questions.md → facts/query.dl 초안 재생성 (비결정)
```

`facts/query.dl` 은 복사본에 **이미 동봉** 돼 있으니 곧장 `/factlog check` 를
실행하면 됩니다. 로직 체크가 그 동봉 초안을 평가해 아래 q1–q6 예시가 그대로
재현됩니다. `/factlog query` 는 그 초안을 LLM으로 다시 잡아 보는 **선택** 단계이며,
비결정이라 재생성하면 예시와 어긋날 수 있습니다(바로 아래 경고 참조). `check` 가
보여 주는 `facts/logic_report.txt` 는 **엔진이 생성** 합니다 — Claude가 결과를
서술하거나 결론짓는 것이 아닙니다.

> `/factlog query` 를 다시 돌리면 초안이 달라져 아래 출력(q5의 정확한 트리플
> 문자열·미검증/1행 등)과 어긋날 수 있습니다 — `sync` 와 같은 비결정 경계이며 버그가
> 아닙니다. 반면 `/factlog check` 의 컴파일·로직 체크는 같은 입력이면 항상 같은
> 결과를 냅니다.

이 예제의 `query.dl`(q1–q6은 `policy/questions.md` 의 q1–q6과 1:1)은 일부러
**hit · miss · 그리고 내 게이트가 답을 바꾸는 경우** 를 모두 시연합니다.

- q1–q3 은 accepted 사실에 맞아떨어져 각각 1행으로 해소됩니다.
- q4 `relation("factlog", "developed_by", "Anthropic")?` 는 세 상수가 모두 엔진에
  존재하지만 그 트리플 자체는 accepted 사실이 아니므로 **0행** 으로 해소됩니다 —
  "승인하지 않은 것은 엔진 hit 이 아니다" 를 보여 줍니다.
- **q5 `relation("Anthropic", "develops", "Claude Code")?` 는 방금 3절에서 여러분이
  accept 한 바로 그 트리플입니다.** 저장소에 커밋된 초기 리포트(엔진 입력 7개,
  `develops` 가 아직 needs_review)에서는 `develops` 가 아직 accepted 어휘가 아니므로,
  이 쿼리의 결과는 검증된 **0행(부정)** 이 아니라 **미검증(unverified)** 으로 표기되고,
  리포트에는 `develops` 가 엔진 관계가 아니라는 경고도 함께 남습니다. KB가 승인한 적
  없는 어휘에 대한 빈 결과는 "그런 사실이 없음(검증된 부정)"이 아니라 애초에 엔진이
  답할 수 없는 질문이기 때문입니다 — q4 가 세 상수 모두 승인된 어휘라 검증된 **0행** 인
  것과 정확히 대비됩니다. 3절에서 accept 한 뒤(엔진 입력 8개) `/factlog check` 를 다시
  돌리면 **같은 쿼리가 1행** (`Anthropic, develops, Claude Code`)으로 해소되고 그 경고도
  사라집니다 — 즉 **사람의 게이트가 엔진의 답을 바꾼 것** 입니다.
- q6 은 accepted 관계로 답할 수 없어 `review_required` 로 사람에게 라우팅됩니다.

## 5. 한 질문에 답 받기 — `/factlog ask`

검증된 답을 한 번에 받고 싶다면 `ask` 를 씁니다. 질문은 결정론적으로
엔진(검증됨) 또는 위키 탐색(미검증)으로 라우팅되며, **Claude 가 판정하는 것이
아니라 `tools/ask_router.py` 가 코드로** 어느 경로인지 결정합니다(라우팅 근거는
accepted 사실뿐 — 후보 어휘는 절대 새지 않습니다). 각 답에는 근거 소스
경로(`← <source>`)가 함께 붙습니다.

*Claude Code 세션에서 실행:*

```
/factlog ask Who developed Claude Code?
```

이 질문은 초안 `relation("Claude Code", "developed_by", "Anthropic")?` 로 잡히고,
그 트리플은 **처음부터** accepted 사실(4절 q1)이라 곧장 **엔진 hit(검증됨, 1행)**
으로 해소됩니다.

```
VERIFIED — engine
query: relation("Claude Code", "developed_by", "Anthropic")?
rows: 1
  - Claude Code, developed_by, Anthropic (sources: 1, extraction conf: 0.99)
    ← sources/example.md#what-is-claude-code
```

이제 **3절에서 여러분이 직접 accept 한** 트리플을 ask 경로에서도 확인해 봅니다.

```
/factlog ask Does Anthropic develop Claude Code?
```

이 질문은 초안 `relation("Anthropic", "develops", "Claude Code")?` 로 잡힙니다 —
4절 q5 와 **같은 트리플** 입니다. 3절에서 accept 를 마쳤으므로(엔진 입력 8개)
`develops` 가 이제 엔진 관계라서 이 질문도 **엔진 hit(검증됨, 1행)** 으로
해소됩니다.

```
VERIFIED — engine
query: relation("Anthropic", "develops", "Claude Code")?
rows: 1
  - Anthropic, develops, Claude Code (sources: 1, extraction conf: 0.90)
    ← sources/example.md#what-is-claude-code
```

> **반사실 — 만약 3절 accept 전에 이 질문을 던졌다면?** 그때는 엔진 입력이 7개고
> `develops` 가 아직 accepted 관계가 아니라서, `ask_router.py` 는 같은 초안을
> **결정론적으로 위키 탐색(UNVERIFIED)** 으로 라우팅합니다 — Claude 의 판단이
> 아니라 `develops` 가 엔진 관계 집합에 없다는 사실 하나만으로 코드가 내리는
> 결정입니다. 이때 질문이 accepted 엔티티(Anthropic·Claude Code)를 언급하므로
> grounding 앵커로 검증돼 보이는 것은 방향이 반대인
> `Claude Code, developed_by, Anthropic` 이며(그 외 Claude Code 의 다른 accepted
> 속성 몇 개도 앵커로 함께 뜹니다), 정작 여러분이 겨냥한
> `Anthropic, develops, Claude Code` 트리플 자체는 **미검증** 으로 남습니다.
> 4절 q5 가 리포트에서 미검증→1행 으로 바뀌었듯, 같은 **사람의 게이트** 가 ask
> 경로에서도 이 질문의 답을 **미검증(wiki) → 검증(engine)** 으로 바꾼 것입니다.

## 6. 사실의 출처 추적 — `factlog provenance`

마지막으로, 답의 근거를 끝까지 따라가 봅니다. **읽기 전용** 으로 트리플이 어느
소스의 어느 발췌에서 왔는지 보여 줍니다.

*터미널에서 실행:*

```bash
factlog provenance "Claude Code" developed_by Anthropic
factlog vocab        # 질의에 쓸 수 있는 정확한 엔티티/관계 이름 보기
```

`provenance` 출력은 소스 경로·상태·신뢰도·노트(추출 발췌), 그리고 소스가 디스크에
없을 때의 `[stale]` 표시까지 보여 줍니다.

```
  Claude Code / developed_by / Anthropic
    ← sources/example.md#what-is-claude-code  [confirmed, conf 0.99]
        note: stated directly in source
```

이로써 **추출 → 검토 → 승인 → 쿼리/검증 → 출처 추적** 한 사이클을 끝냈습니다.

## (선택) 재추출 체험 — `/factlog sync`

여기까지는 복사본에 이미 들어 있던 **고정 후보** 로 완주했습니다. 이제 소스에서
후보를 직접 다시 뽑아 보고 싶다면 `/factlog sync` 를 실행합니다. 새 소스를
추가했거나 추출을 다시 돌리고 싶을 때 쓰는 명령입니다.

*Claude Code 세션에서 실행:*

```
/factlog sync
```

`sync` 는 `sources/` 를 읽어 **후보 사실만** 다시 만들어 `facts/candidates.csv`
와 `pages/` 를 갱신합니다. 그 자체로는 신뢰할 수 있는 사실을 만들지 **않습니다** —
승인은 사람 단계의 몫입니다. 사람이 이미 내린 `accept`/`reject` 결정은 재싱크
후에도 **보존** 됩니다.

> **여기서가 학습 포인트입니다.** `sync` 는 LLM 추출 단계라 **비결정적** 입니다.
> 다시 돌리면 후보 문구·confidence(위 예시의 conf 0.90, note, `accept` 인자 등)가
> 달라질 수 있고, 그러면 2~4절의 정확한 예시 출력과 더는 일치하지 않습니다 — 이는
> **버그가 아니라 정상** 입니다. factlog가 왜 추출(비결정)과 검증(결정론 엔진)을
> 서로 다른 경계로 나누는지 몸으로 느낄 수 있는 지점입니다. 컴파일·로직 체크·출처
> 추적은 언제 돌려도 같은 입력이면 같은 결과지만, 후보 추출은 그렇지 않습니다.
>
> 정확한 예시 출력을 다시 보고 싶다면 **복사본을 새로 뜨세요.** 이 시점엔
> `./factlog-demo` 가 이미 있으므로, 먼저 `rm -rf ./factlog-demo` 로 기존 복사본을
> 지운 뒤 0절을 다시 실행하세요(그대로 다시 `cp` 하면 `./factlog-demo/sample-kb`
> 로 중첩 복사됩니다). 갓 복사한 KB에는 고정 후보(candidates.csv)와 동봉된
> query.dl 초안이 그대로 들어 있어, 그 고정 데이터 기준의 결정적 단계
> (review/status/accept/check) 출력이 2절부터 동일하게 재현됩니다(`/factlog query`
> 를 다시 돌리면 초안이 달라질 수 있는 것과 같은 맥락).

## 어떤 명령이 복사본 파일을 바꾸나

원본은 그대로 두고, 모든 변경은 복사본(`./factlog-demo`)에서만 일어납니다. 어떤
명령이 무엇을 쓰는지 정리하면 다음과 같습니다.

| 명령 | 실행 위치 | 복사본에 미치는 영향 |
|------|-----------|----------------------|
| `/factlog sync` | Claude Code 세션 | `facts/candidates.csv`, `pages/`, `decisions/`, `runs/` 갱신(후보만 — accepted 아님) |
| `factlog review` | 터미널 | **읽기 전용** — 대기 큐 나열 |
| `factlog status` | 터미널 | **읽기 전용** — KB 상태 요약 |
| `factlog accept` / `reject` | 터미널 | `facts/candidates.csv`(상태 변경) + `facts/accepted.dl` 재컴파일 |
| `/factlog query` | Claude Code 세션 | `facts/query.dl`(쿼리 초안) 갱신 |
| `/factlog check` | Claude Code 세션 | `facts/accepted.dl` 컴파일 + `facts/logic_report.txt`(**엔진 생성**) 갱신 |
| `/factlog ask` | Claude Code 세션 | **읽기 전용** — 한 질문에 검증된/미검증 답변 |
| `factlog provenance` / `vocab` | 터미널 | **읽기 전용** — 출처/어휘 조회 |

> `facts/logic_report.txt` 와 `facts/accepted.dl` 은 **엔진/컴파일러가 생성** 하는
> 산출물입니다. 데모를 맞추려고 손으로 편집하지 마세요 — 결과가 다르면 입력
> 데이터를 고쳐 엔진이 그 리포트를 내게 합니다.

## 정리

실습이 끝나면 복사본은 지워도 됩니다(원본은 영향 없음). 단, 0절에서 `factlog use`
로 **전역 활성 KB를 복사본으로 바꿔 두었으므로**, 복사본을 지운 뒤에는 활성 KB를
이전 것으로 **복원** 하세요(안 그러면 활성 KB가 사라진 경로를 가리켜 이후
`factlog status` 등이 깨집니다).

*터미널에서 실행:*

```bash
rm -rf ./factlog-demo
factlog use <0절에서-적어둔-이전-KB>   # 활성 KB를 원래대로 복원
factlog where                          # 복원됐는지 확인
```

(처음부터 `--target ./factlog-demo` 로만 실습했다면 전역 설정을 바꾸지 않았으므로
복원 단계가 필요 없습니다 — 복사본만 지우면 됩니다.)

자세한 명령 설명은 [factlog 문서](../../docs/README.md) 를 참고하세요.
