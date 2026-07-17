# 활성 KB (설정해 둔 KB를 어디서든 대상으로)

> 🌐 [English](active-kb.en.md) | **한국어**

`factlog init`/`setup`(또는 `factlog use <kb>`) 이후, 선택한 KB가 **활성 KB**로
기록됩니다. 그래서 `ingest`/`ask`/`sync` 및 도구들이 어느 작업 디렉터리에서든
그 KB를 대상으로 동작합니다 — `--target`/`--wiki` 가 필요 없습니다.

*터미널에서 실행:*

```bash
factlog use ~/wiki        # make ~/wiki the active KB (recorded in config)
factlog where             # show the active KB and how it was resolved
factlog sources           # list registered sources (original, conversion, fact count)
factlog status            # KB state: facts by status, vocabulary, conflicts, logic freshness, engine
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

경로는 어느 경로로 들어오든 `~` 확장과 절대경로 정규화를 거칩니다. 설정 파일이
없거나, JSON이 깨졌거나, `root` 필드가 비어 있으면 **크래시하지 않고 다음 순위로
떨어집니다** — 최종적으로는 현재 디렉터리입니다.

## 어느 KB가 이겼는지 확인하기

*터미널에서 실행:*

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
