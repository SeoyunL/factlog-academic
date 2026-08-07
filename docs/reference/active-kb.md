# 활성 KB (설정해 둔 KB를 어디서든 대상으로)

> 🌐 [English](active-kb.en.md) | **한국어**

`factlog use <kb>`(또는 아직 활성 KB가 없을 때의 첫 `factlog init`/`setup`)로 고른
KB가 **활성 KB**로 기록됩니다. 그래서 `ingest`/`ask`/`sync` 및 도구들이 어느 작업
디렉터리에서든 그 KB를 대상으로 동작합니다 — `--target`/`--wiki` 가 필요 없습니다.

*Claude Code에 입력:*

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
| 3 | 활성 KB 설정 | `factlog use <경로>` (활성 KB가 아직 없으면 첫 `init`/`setup` 이 기록) | `config file` |
| 4 | 현재 디렉터리 | (아무것도 지정하지 않았을 때의 폴백) | `current directory` |

1순위가 `factlog where` 출력에 나타나지 않는 이유는, `where` 자신이 `--target` 을
받지 않기 때문입니다. 플래그는 그 플래그를 준 **명령 하나에만** 적용되므로,
`where` 는 언제나 2~4순위 중 하나로 해석된 결과를 보고합니다.

경로는 어느 경로로 들어오든 `~` 확장과 절대경로 정규화를 거칩니다. 설정 파일이
없거나, JSON이 깨졌거나, `root` 필드가 비어 있으면 **크래시하지 않고 다음 순위로
떨어집니다** — 최종적으로는 현재 디렉터리입니다.

## KB를 만드는 일과 활성 KB를 고르는 일은 별개입니다

`init`/`setup` 은 KB를 **만드는** 명령이고, 활성 KB를 **고르는** 명령은 `use` 입니다.
그래서 `init`/`setup` 이 활성 KB 설정을 어떻게 다루는지는 다음 네 경우뿐입니다.

| 상황 | 활성 KB 설정 |
|------|---------------|
| 설정 파일이 아직 없음 | 방금 만든 KB로 기록 (첫 실행 경험 그대로) |
| 대상이 이미 기록돼 있음 | 그대로 (설정 파일을 다시 쓰지도 않음) |
| 다른 KB가 기록돼 있음 | **그대로 유지**. 만든 KB는 기록되지 않고, 바꾸는 방법을 함께 출력 |
| 설정 파일을 읽을 수 없음 | **손대지 않음**. 아래 "깨진 설정 파일" 참고 |

```text
factlog init: created /tmp/scratch
factlog init: active-KB config unchanged: /Users/me/wiki — /tmp/scratch was created but is NOT recorded there
  to record it in the config: factlog use /tmp/scratch   (or re-run with --activate)
```

문구가 "활성 KB" 가 아니라 "활성 KB **설정**" 인 것은 정확성 때문입니다. 이 결정은
설정 파일에 대한 것이고, 실제로 어느 KB가 대상이 되는지는 `$FACTLOG_ROOT` 까지 함께
봐야 정해집니다. 그래서 두 가지를 따로 알려 줍니다.

`$FACTLOG_ROOT` 가 **설정 파일과 다른 값**을 가리키면(즉 실제로 설정을 앞지르고
있으면) 그 사실을 적습니다. 비교 대상은 방금 만든 KB가 아니라 **설정 파일** 입니다 —
환경 변수와 설정이 같은 값이면 앞지르는 것이 없으므로 이 줄은 나오지 않습니다.

```text
  note: $FACTLOG_ROOT=/tmp/envkb outranks the config in this session (factlog where)
```

그리고 플래그 없이 실행한 명령이 방금 만든 KB에 **닿지 않으면**, 실제로 어디로 가는지와
어디서 온 값인지를 적습니다. 이쪽은 설정이 아니라 `factlog where` 와 같은 해석
결과(`$FACTLOG_ROOT` > 설정 > 현재 디렉터리)를 봅니다.

```text
  a flagless command would target /Users/me/wiki (from $FACTLOG_ROOT), not /tmp/scratch
    — pass --target /tmp/scratch, or point $FACTLOG_ROOT at /tmp/scratch
```

`setup` 의 마지막 줄도 같은 질문을 씁니다. 설정 파일이 무엇을 기록하고 있든, 다음에
칠 `/factlog sync` 가 어디로 가는지가 사용자가 실제로 묻는 것이기 때문입니다.

임시 KB 하나를 만들려고 `init` 을 돌렸다가 원래 쓰던 KB를 잃는 일이 없도록 하기
위해서입니다. 만들면서 바로 활성화하려면 `--activate` 를, 활성 KB가 없는 상태에서도
설정 파일을 만들고 싶지 않으면(스크립트·임시 KB) `--no-activate` 를 씁니다. 두 플래그를
같이 주면 사용 오류(종료 코드 2)입니다.

이미 활성 KB가 있는 상태에서 `init`/`setup` 이 활성 KB를 옮겨 주기를 기대하던
방식이라면, `--activate` 를 붙이거나 뒤이어 `factlog use <경로>` 를 실행하세요.
바뀐 동작은 출력에 그대로 적히므로, 조용히 달라지지는 않습니다.

`--target` 을 생략했을 때의 대상도 바뀌었습니다. 예전에는 언제나 `~/wiki` 였고, 지금은
`$FACTLOG_ROOT` 와 활성 KB 설정을 먼저 봅니다. 그래서 둘 중 하나가 설정된 환경에서
맨손 `init`/`setup` 을 돌리던 스크립트는 `~/wiki` 가 아니라 그쪽을 대상으로 잡습니다.
전과 같은 자리를 원하면 `--target ~/wiki` 를 명시하세요. 어느 쪽이든 고른 대상과 그
출처가 매 실행 첫 줄에 출력됩니다.

설정된 활성 KB의 경로가 지금 존재하지 않더라도(외장 볼륨을 마운트하지 않은 경우 등)
`init` 은 그 설정을 가져가지 않습니다. 옮기는 것은 언제나 사용자의 명시적인 행동입니다.

### 깨진 설정 파일

설정 파일이 **있는데 읽히지 않으면**(JSON이 잘렸거나, 객체가 아니거나, 권한이 없거나)
`init`/`setup` 은 그 파일에 쓰지 않습니다. 읽히지 않는 바이트가 바로 사용자의 KB
경로일 수 있고, 쓰기는 그 경로와 `lang` 까지 함께 지웁니다 — 마운트되지 않은 볼륨을
가리키는 경우보다 더 되돌리기 어렵습니다.

```text
factlog init: active-KB config at /Users/me/.config/factlog/config.json could not be read
  — leaving it untouched; /tmp/scratch was created but is NOT recorded there
  repair that file, or overwrite it deliberately: factlog use /tmp/scratch
```

`setup --lang` 도 같은 이유로 함께 보류됩니다. 언어를 기록하는 쓰기가 설정 파일 전체를
다시 세우므로, 읽지 못한 root 바이트를 똑같이 지우기 때문입니다 — 방금 "손대지 않았다" 고
적어 놓고 손대는 셈이 됩니다.

파일을 고쳤거나 버려도 좋다면 `--activate` 로 덮어쓸 수 있고, 그 경우 무엇을 덮어썼는지
출력합니다(이때는 설정이 다시 온전해지므로 같은 실행의 `--lang` 도 정상 적용됩니다). 반면 파일이 **읽히기는 하는데** 기록된 root가 없으면(`{"lang": "ko"}`,
`{"root": ""}`) 잃을 경로가 없으므로 첫 실행과 똑같이 기록합니다. `lang` 은 보존됩니다.

`--target` 을 생략하면 `init`/`setup` 도 다른 명령과 같은 순서로 대상을 정합니다:
`$FACTLOG_ROOT` > 활성 KB 설정 > `~/wiki`. 현재 디렉터리는 이 사슬에 없습니다 —
아무 데서나 실행한 `init` 이 그 자리에 KB 뼈대를 흩뿌리는 편이 더 나쁜 기본값이기
때문입니다. 명시하지 않아 골라진 대상은 어디서 왔는지와 함께 출력됩니다.

사슬에서 빼는 것만으로는 부족합니다. `factlog where --porcelain` 은 아무것도 설정돼
있지 않으면 현재 디렉터리를 출력하고, 스킬은 그 값을 `$FACTLOG_ROOT` 로 export 하라고
합니다. 그래서 현재 디렉터리가 환경 변수를 통해 돌아올 수 있습니다. 대상을 **명시하지
않았고**, 그 대상이 현재 디렉터리이며, 그 디렉터리에 이미 다른 파일이 있고, 아직 factlog
KB가 아니라면 — `init` 은 만들지 않고 멈춥니다. 정말 그 자리에 만들 생각이라면
`--target <경로>` 로 이름을 대면 됩니다. 비어 있는 디렉터리나 이미 KB인 디렉터리는
잃을 것이 없으므로 그대로 진행합니다.

```text
factlog init: no --target given; using /Users/me/wiki (from the active KB config)
```

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
