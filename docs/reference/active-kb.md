# 활성 KB (설정해 둔 KB를 어디서든 대상으로)

> 🌐 [English](active-kb.en.md) | **한국어**

`factlog init`/`setup`(또는 `factlog use <kb>`) 이후, 선택한 KB가 **활성 KB**로
기록됩니다. 그래서 `ingest`/`ask`/`sync` 및 도구들이 어느 작업 디렉터리에서든
그 KB를 대상으로 동작합니다 — `--target`/`--wiki` 가 필요 없습니다.

*Claude Code에 입력:*

```bash
factlog use ~/wiki        # make ~/wiki the active KB (recorded in config)
factlog where             # show the active KB and how it was resolved
factlog sources           # list registered sources (original, conversion, fact count)
factlog status            # KB state: facts by status, vocabulary, conflicts, questions, logic freshness, engine
cd /anywhere && factlog ingest report.pdf   # → ~/wiki/runs/sources/report.txt
factlog eject report.pdf  # inverse of ingest: remove the conversion + retire its facts
factlog ignore drafts/*.md   # exclude sources from sync (re-extraction)
factlog provenance Acme uses FastAPI   # trace a fact to its source(s)
```

> **슬래시 명령(`/factlog …`)도 활성 KB에서 동작합니다.** 다만 factlog **소스
> 저장소 안에서** 실행하면 번들 `examples/sample-kb` 와 혼동될 수 있으니, KB
> 폴더에서 열거나 `factlog use <kb>` 로 활성 KB를 먼저 지정하세요. `factlog where`
> 로 어느 KB가 대상인지 확인할 수 있습니다. 신선도 게이트(PreToolUse 훅)도
> **활성 KB**(`FACTLOG_ROOT > config > cwd` 로 해석된)를 보호합니다 — 활성 KB가
> 아닌 다른 KB의 엔진 입력을 직접 편집하는 경우는 게이트의 대상이 아닙니다.

해석 우선순위: `--target`/`--wiki` 플래그 > `$FACTLOG_ROOT` > 활성 KB 설정
(`${XDG_CONFIG_HOME:-~/.config}/factlog/config.json`) > 현재 디렉터리. 설정이 없으면
동작은 종전과 같습니다(현재 디렉터리 사용).

## 해석 우선순위 표

네 후보를 위에서부터 훑어 **처음으로 값이 있는 것**이 이깁니다. 어느 것이 이겼는지는
`factlog where` 의 `resolved from:` 줄에 그대로 찍힙니다.

| 순위 | 출처 | 지정 방법 | `factlog where` 의 `resolved from:` 표기 |
|------|------|-----------|------------------------------------------|
| 1 | 명령줄 플래그 | `--target <경로>` (도구에 따라 `--wiki <경로>`) | (표시되지 않음 — 아래 참고) |
| 2 | 환경 변수 | `export FACTLOG_ROOT=<경로>` | `env ($FACTLOG_ROOT)` |
| 3 | 활성 KB 설정 | `factlog use <경로>` (또는 `factlog init`/`setup` 이 자동 기록) | `config file` |
| 4 | 현재 디렉터리 | (아무것도 지정하지 않았을 때의 폴백) | `current directory` |

1순위가 `factlog where` 출력에 나타나지 않는 이유는, `where` 자신이 `--target` 을
받지 않기 때문입니다. 플래그는 그 플래그를 준 **명령 하나에만** 적용되므로,
`where` 는 언제나 2~4순위 중 하나로 해석된 결과를 보고합니다.

> **positional 경로를 받는 도구는 한 순위가 더 있습니다.** `tools/validate.py` 는
> KB 경로를 위치 인자로도 받는데(`validate.py <경로>`), 이 인자는 **1순위와 2순위
> 사이**에 들어갑니다 — `--target`/`--wiki` > positional > `$FACTLOG_ROOT` > 활성 KB
> 설정 > 현재 디렉터리. 셸 하니스(`tests/*.sh`)와 `merge_candidates` 의 위임 호출이
> KB를 이 자리로 넘기기 때문에, 설정보다 아래에 두면 **호출자가 명시한 KB 대신 활성
> KB를 검사**하게 됩니다. 빈 값(`validate.py ""`)은 다음 순위로 떨어지지 않고 그
> 자리에서 거부됩니다(종료 코드 1) — 안 그러면 미설정 변수 하나가 조용히 대상을
> 바꿔 버립니다.

> **손대 쓰는 두 도구는 3순위로 해석된 KB를 거부합니다.** `tools/finalize.py`
> (`--target`, 별칭 `--wiki`)와 `tools/merge_candidates.py`(`--wiki` — 이쪽은
> `--target` 을 받지 않습니다)는 위 표대로 해석은 하지만, 3순위(활성 KB 설정)로만
> 정해진 KB를 **현재 디렉터리가 그 KB 밖일 때** 거부합니다(종료 코드 1).
> `merge_candidates` 가 `facts/candidates.csv`, `pages/`,
> `decisions/open-questions.md` 를 다시 쓰고, `finalize` 는 그것들을 직접 쓰지 않고
> `merge_candidates` 를 체인한 뒤 `facts/accepted.dl` 을 재컴파일합니다. 그래서
> 아무도 겨냥하지 않은 실행이 활성 KB를 조용히 덮어쓰고 그 KB의 로직 리포트를
> 무효화하는 일을 막습니다. 겨냥하는 방법은 세 가지입니다 — `--target`/`--wiki`
> 로 이름 대기, `$FACTLOG_ROOT` 로 이름 대기, 그 KB 안에서 실행하기. 거부 메시지가
> 해석된 경로와 두 가지 지정 방법(플래그, `export FACTLOG_ROOT`)을 함께 찍어 줍니다
> — "그 KB 안에서 실행하기"는 겨냥 방법이긴 하지만 메시지에는 나오지 않습니다.

> **알려진 예외: 빈 플래그값은 이 표를 타지 않습니다.** `--wiki ""`/`--target ""`
> — export하지 않은 `$FACTLOG_ROOT` 를 그대로 넘겼을 때 생기는 형태입니다 — 에서
> 두 도구의 동작이 갈립니다. `finalize.py --target ""` 는 여전히 설정 KB로 해석해
> 거부하지만(종료 코드 1), `merge_candidates.py --wiki ""` 는 루트를 **두 번**
> 해석합니다. 가드는 3순위를 보는데 실제 쓰기 경로는 빈 인자를 다시 읽어 **현재
> 디렉터리**로 떨어지므로, 거부 없이 현재 디렉터리에 쓰면서 출처는 `(from config)`
> 로 찍고 종료 코드 0으로 끝납니다.

> **자기 엔진 출력만 다시 쓰는 `compile_facts.py`/`run_logic_check.py` 는 플래그
> 없이 3순위 KB를 그대로 씁니다.** 다만 이것은 "위험이 없다"는 뜻이 아니라 **건드리는
> 파일 범위가 자기 엔진 출력으로 한정된다**는 뜻입니다. KB 밖에서 겨냥 없이 실행한
> `compile_facts.py` 도 single-valued 충돌을 만나면 활성 KB의 `facts/accepted.dl` 을
> **삭제**하고 종료 코드 1로 끝나므로, 충돌이 풀릴 때까지 그 KB는 엔진 입력을
> 잃습니다. 두 도구가 가드에서 빠져 있는 것은 확정된 규칙이 아니라 잠정 상태입니다
> (`merge_candidates` 의 가드 docstring이 이 둘을 "follow-up으로 남긴다"고 적고
> 있습니다). 그리고 아직 **설정 티어를 아예 보지 않는** 스크립트도 남아 있습니다 —
> `tools/generate_logic_policy.py` 는 KB 플래그가 없고 `$FACTLOG_ROOT` 와 현재
> 디렉터리만 보므로, 설정만 해 둔 채 KB 밖에서 실행하면 `not a factlog KB root: …`
> 로 종료 코드 1이 됩니다(플래그 표면 통일은 #533 소관).

경로는 어느 경로로 들어오든 `~` 확장과 절대경로 정규화를 거칩니다. 설정 파일이
없거나, JSON이 깨졌거나, `root` 필드가 비어 있으면 **크래시하지 않고 다음 순위로
떨어집니다** — 최종적으로는 현재 디렉터리입니다.

## 어느 KB가 이겼는지 확인하기

*Claude Code에 입력:*

```bash
factlog where
```

```text
active KB: /Users/me/wiki
resolved from: config file (precedence: --flag > $FACTLOG_ROOT > config > cwd)
config file: /Users/me/.config/factlog/config.json
```

`factlog lang` 으로 나레이션 언어를 설정해 두었다면 `narration language:` 줄이 함께
출력됩니다(어시스턴트의 산문에만 적용되며 엔진 출력에는 영향이 없습니다).

스크립트에서 쓸 때는 `--porcelain` 이 **활성 KB 절대경로 한 줄만** 출력합니다 —
라벨도 다른 줄도 없습니다.

*터미널에서 실행:*

```bash
export FACTLOG_ROOT="$(factlog where --porcelain)"
```

`ingest` 처럼 KB를 대상으로 삼는 명령은 플래그 없이 실행될 때 어느 KB를 어디서
가져왔는지 첫 줄에 알려 주므로, 의도치 않은 KB에 쓰는 일을 알아챌 수 있습니다.

```text
factlog ingest: target KB /Users/me/wiki (from config)
```
