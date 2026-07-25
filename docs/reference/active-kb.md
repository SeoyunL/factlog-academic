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
| 1 | 명령줄 플래그 | `--target <경로>` (`--wiki <경로>` 는 어디서나 통하는 별칭) | (표시되지 않음 — 아래 참고) |
| 2 | 환경 변수 | `export FACTLOG_ROOT=<경로>` | `env ($FACTLOG_ROOT)` |
| 3 | 활성 KB 설정 | `factlog use <경로>` (또는 `factlog init`/`setup` 이 자동 기록) | `config file` |
| 4 | 현재 디렉터리 | (아무것도 지정하지 않았을 때의 폴백) | `current directory` |

1순위가 `factlog where` 출력에 나타나지 않는 이유는, `where` 자신이 `--target` 을
받지 않기 때문입니다. 플래그는 그 플래그를 준 **명령 하나에만** 적용되므로,
`where` 는 언제나 2~4순위 중 하나로 해석된 결과를 보고합니다.

> **`tools/` 의 플래그 표면은 하나입니다.** KB를 지정하는 번들 스크립트는 모두
> `--target` 을 받고, `--wiki` 는 그 별칭으로 받습니다 — 같은 dest를 공유하는 한
> 개의 옵션이므로 둘을 함께 넘겨도 오류가 아니고 **명령줄에서 뒤에 온 철자가**
> 이깁니다(#533). 오타 플래그(`--targt <경로>`)는 무시되지 않고 종료 코드 2로
> 거부됩니다. 무시하면 실패하지 않고 2~4순위로 해석된 KB를 대상으로 그냥 실행되기
> 때문입니다. 플래그가 없는 `tools/` 스크립트는 애초에 KB를 다루지 않는 둘뿐입니다
> — `refresh_arxiv_categories.py`(공개된 arXiv 분류 체계를 받아 소스 트리와 비교)와
> `spike_fallback_precision.py`(캐시된 API 응답에 대한 측정).

> **positional 경로를 받는 도구는 한 순위가 더 있습니다.** `tools/validate.py` 는
> KB 경로를 위치 인자로도 받는데(`validate.py <경로>`), 이 인자는 **1순위와 2순위
> 사이**에 들어갑니다 — `--target`/`--wiki` > positional > `$FACTLOG_ROOT` > 활성 KB
> 설정 > 현재 디렉터리. 셸 하니스(`tests/*.sh`)와 `merge_candidates` 의 위임 호출이
> KB를 이 자리로 넘기기 때문에, 설정보다 아래에 두면 **호출자가 명시한 KB 대신 활성
> KB를 검사**하게 됩니다. 빈 값(`validate.py ""`)은 다음 순위로 떨어지지 않고 그
> 자리에서 거부됩니다(종료 코드 1) — 안 그러면 미설정 변수 하나가 조용히 대상을
> 바꿔 버립니다.

> **손대 쓰는 두 도구는 3순위로 해석된 KB를 거부합니다.** `tools/finalize.py` 와
> `tools/merge_candidates.py` 는 위 표대로 해석은 하지만, 3순위(활성 KB 설정)로만
> 정해진 KB를 **현재 디렉터리가 그 KB 밖일 때** 거부합니다(종료 코드 1).
> `merge_candidates` 가 `facts/candidates.csv`, `pages/`,
> `decisions/open-questions.md` 를 다시 쓰고, `finalize` 는 그것들을 직접 쓰지 않고
> `merge_candidates` 를 체인한 뒤 `facts/accepted.dl` 을 재컴파일합니다. 그래서
> 아무도 겨냥하지 않은 실행이 활성 KB를 조용히 덮어쓰고 그 KB의 로직 리포트를
> 무효화하는 일을 막습니다. 겨냥하는 방법은 세 가지입니다 — `--target`/`--wiki`
> 로 이름 대기, `$FACTLOG_ROOT` 로 이름 대기, 그 KB 안에서 실행하기. 거부 메시지가
> 해석된 경로와 두 가지 지정 방법(플래그, `export FACTLOG_ROOT`)을 함께 찍어 줍니다
> — "그 KB 안에서 실행하기"는 겨냥 방법이긴 하지만 메시지에는 나오지 않습니다.

> **빈 플래그값은 이 표를 타지 않고 거부됩니다.** `--target ""`/`--wiki ""` —
> export하지 않은 `$FACTLOG_ROOT` 를 그대로 넘겼을 때 생기는 형태입니다 — 에 대해
> #533이 통일한 스크립트는 모두 아무것도 읽거나 쓰기 전에 *the KB-root flag
> (--target/--wiki) was empty* 로 종료 코드 1을 냅니다. `validate.py` 는 자기
> 문구인 `--target was empty` 로 답합니다. 빈 값은 어느 쪽이든 호출자의 버그이고,
> 다음 순위로 떨어뜨리면 호출자는 KB를 지정했다고 믿는데 실제로는 설정 KB가
> 대상이 됩니다.
>
> `merge_candidates.py --wiki ""` 가 그중 가장 날카로운 사례였습니다. 루트를 **두
> 번** 해석해서, 가드는 3순위를 보는데 실제 쓰기 경로는 빈 인자를 다시 읽어 **현재
> 디렉터리**로 떨어졌고, 그래서 거부 없이 현재 디렉터리에 쓰면서 출처는
> `(from config)` 로 찍고 종료 코드 0으로 끝났습니다(#546). 이제는 한 번의 해석이
> 가드·안내 줄·쓰기 경로를 모두 먹입니다. 남은 차이는 `finalize.py --target ""`
> 입니다. 이쪽은 빈 값을 여전히 다음 순위로 흘려보내므로, 설정 KB **안에서**
> 실행하면 거부하지 않고 그 KB를 finalize 합니다.

> **`compile_facts.py`/`run_logic_check.py` 는 플래그 없이 3순위 KB를 그대로 쓰고,
> 둘 다 대상을 먼저 밝힙니다.** 각각 `<도구>: target KB <루트> (from <출처>)` 를
> 아무것도 하기 전에 출력합니다 — 손대 쓰는 도구들이 찍는 것과 같은 줄입니다. 둘 다
> 그 KB 자신의 산출물을 다시 유도하므로 겨냥하지 않은 실행도 겨냥한 실행과 같은
> 바이트를 씁니다. 그럼에도 이 줄이 필요한 이유는 남은 위험이 **읽기** 쪽이기
> 때문입니다 — KB A를 검사했다고 믿으면서 KB B의 리포트를 읽는 일.
>
> **두 도구의 처리는 의도적으로 다릅니다.** 각자가 파괴할 수 있는 크기에 맞춘
> 것입니다. `compile_facts.py` 에는 파괴적인 단계가 하나 있습니다 — single-valued
> 충돌을 만나면, 확정 행끼리 모순되는 KB의 엔진 입력을 아무도 신뢰하지 않도록
> 게이트가 `facts/accepted.dl` 을 삭제합니다(#212/#327). 아무도 겨냥하지 않은
> 실행(3순위로 해석됐고 현재 디렉터리가 그 KB 밖일 때)에서는 이제 그 **삭제를
> 거부**하고 파일을 남깁니다. 종료 코드는 여전히 1이고 아무것도 컴파일하지
> 않습니다(#547). **겨냥한** 실행(`--target`, `$FACTLOG_ROOT`, 또는 그 KB 안에서
> 실행)은 종전대로 삭제하므로, 문서화된 모든 흐름에서 #212 불변식은 그대로입니다.
> `run_logic_check.py` 는 아무것도 거부하지 않습니다. 파괴하는 것이 없고(유일한
> 쓰기인 `facts/logic_report.txt` 는 검사를 돌리는 행위 **그 자체**라서, 오래된
> 리포트를 신선해 보이게 만들 수가 없습니다), `hooks/gate_check.sh` 가 자신의 DENIED
> 메시지에서 처방하는 명령이 바로 이것이기 때문입니다 — 그 훅도 지킬 KB를 같은 3순위로
> 정하므로, 여기서 거부하면 게이트가 스스로 처방한 해법이 못 돌게 됩니다.
>
> 그 거부가 받아들인 절충은 이렇습니다. 그 KB에는 **모순 이전 스냅샷**
> `accepted.dl` 이 남고, 겨냥한 실행이 충돌을 풀 때까지 `/factlog ask` 는 그
> 스냅샷으로 계속 답합니다. 종료 코드 1과 명시적인 문구가 이를 알립니다. 오래된 KB는
> 다음 겨냥한 실행이 치유하지만, 아무도 겨냥하지 않은 KB에서 엔진 입력을 통째로
> 빼앗은 상태는 그렇지 않습니다.

> 설정 티어를 아예 보지 않는 스크립트는 이제 없습니다 —
> 스킬과 `finalize` 가 둘 다 무인자로 부르는 `tools/generate_logic_policy.py` 는
> `$FACTLOG_ROOT` 와 현재 디렉터리만 보느라 설정만 해 둔 상태에서
> `not a factlog KB root: …` 로 종료 코드 1이 됐지만, 이제 형제들과 같은 네 순위를
> 따릅니다(#533).

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
