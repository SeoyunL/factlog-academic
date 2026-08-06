# 소스 제외와 제거 (`ignore` · `eject`)

> 🌐 [English](ignore-eject.en.md) | **한국어**

## sync에서 소스 제외하기 (`factlog ignore`)

`/factlog sync` 는 매 실행마다 **모든** 소스를 다시 추출합니다. 특정 소스를
그 대상에서 빼려면 — 초안, 작업 중인 문서, 외부 문서 등 — KB별 **sync-ignore
목록**(`policy/sync-ignore.md`)에 추가하십시오. 무시된 소스는 **수정되더라도**
`/factlog sync`, `factlog ingest --scan`, 커버리지 누락 보고와 `/factlog ask`의
wiki 탐색 근거에서 건너뜁니다. 이미 머지된 사실은 그대로 유지됩니다(사실을 실제로
제거하려면 `factlog eject` 사용).

```bash
factlog ignore drafts/*.md sources/wip-notes.md   # add pattern(s)
factlog ignore                                     # list patterns + what they match
factlog ignore --remove drafts/*.md               # remove a pattern
```

`policy/sync-ignore.md` 는 한 줄에 글롭(glob) 하나씩 적습니다(다른 정책 파일과
같은 너그러운 형식 — `#` 주석, `-` 불릿, 백틱 인용 항목 지원. `#` 로 시작하는
패턴은 백틱으로 감싸십시오). 패턴은 소스의 전체 ref(`sources/...` /
`runs/sources/...`) 또는 소스 루트 내 경로로 매칭됩니다. 글롭 의미:
`*` 와 `?` 는 한 경로 세그먼트 안에 머물고(`/` 를 **넘지 않음**), `**` 는
세그먼트를 넘으며, 끝의 `/` 는 그 하위 트리 전체를 뜻합니다.

| 패턴 | 매칭 대상 |
|---------|---------|
| `drafts/*.md` | `sources/drafts/x.md` — 단, `sources/drafts/sub/x.md` 는 아님 |
| `drafts/**` (또는 `drafts/`) | `sources/drafts/` 아래 전부 |
| `**/*.md` | 임의 깊이의 모든 `.md` |

`factlog sources` 는 무시된 소스를 `[ignored]` 로 표시하고, 커버리지는 이를 누락이
아니라 `excluded` 로 보고합니다.

## 소스 제거 (`factlog eject`) — `ingest` 의 역연산

`factlog eject <source>` 는 적재(ingest)를 되돌립니다. `runs/sources/` 변환본을
삭제하고, 해당 소스에서 추출된 행을 `runs/*.json` 에서 제거하며, 그 소스를 인용하는
사실을 폐기합니다. 소스는 파일명, 어간(stem), 또는 KB 기준 상대 경로로 지정할 수
있습니다 — 바이너리 원본(예: `report.pdf`)을 지정하면 그 `runs/sources/<원본이름>.md`
변환본도 함께 매칭되고(변환본의 provenance 헤더로 짝을 확인), 어간만 주면 같은 어간을
가진 모든 소스가 매칭됩니다.

```bash
factlog eject report.pdf                 # delete conversion; mark citing facts superseded (kept for audit)
factlog eject report.pdf --purge         # delete the citing candidate rows instead of superseding them
factlog eject report.pdf --delete-original  # also delete the user's original under sources/
factlog eject report.pdf --dry-run       # show the planned changes, modify nothing
```

### 이름 지정 방식 — 파일명은 넓고, 경로는 좁습니다

| 지정 형태 | 매칭 범위 |
|---------|---------|
| 어간 `report` | 그 어간을 가진 **모든** 소스와 그 변환본 |
| 파일명 `report.html` | 디렉터리를 가리지 않고 그 이름을 가진 **모든** 소스와 그 변환본 |
| 경로 `sub/report.html`, `./report.html` | `sources/` 기준 그 경로의 원본에서 만들어진 변환본만 |
| KB 기준 ref `sources/sub/report.html` | 그 원본 + 그 원본에서 만들어진 변환본 |
| 절대 경로 `/kb/sources/sub/report.html` | 위와 동일(KB 기준 ref 로 환원) |

**파일명은 경로 지정이 아닙니다.** `factlog eject report.html` 은 의도적으로 넓게
매칭되므로 `sources/sub/report.html` 쪽도 함께 걸립니다. 최상위 것만 빼려면 경로로
지정하십시오 — 변환본만 지우려면 `./report.html`, 원본까지 대상에 넣으려면(즉
`--delete-original` 이 실제로 원본을 지우게 하려면) `sources/report.html` 처럼 KB
기준 ref 로 지정합니다. 위 표의 두 줄이 그 차이입니다.

경로를 주면 그 경로에서 만들어진 변환본만 매칭됩니다 — 변환본은 `runs/sources/`
아래에 원본의 서브디렉터리를 미러링하므로, `factlog eject sub/report.html` 은
`runs/sources/sub/report.html.md` 만 지우고 다른 디렉터리의 동명 원본이 만든
`runs/sources/report.html.md` 는 건드리지 않습니다.

**상대** 경로는 **적힌 그대로** 비교합니다. `..` 를 접거나 대소문자를 무시하지
않으므로, `sub/../report.html` 과 `SUB/report.html` 은 (파일시스템이 대소문자를
구분하지 않더라도) 아무것도 매칭하지 않고 종료 코드 1 로 끝납니다. 상대 경로는
심링크도 풀지 않으므로, 심링크 디렉터리 이름을 거쳐 적은 상대 경로
(`link/report.html`)는 매칭되지 않습니다. 실제 경로(`real/report.html`)나 절대
경로로 지정하십시오.

**절대** 경로는 반대로, 파일시스템에게 직접 물어서 KB 기준 ref 로 환원합니다 —
경로의 조상을 거슬러 올라가며 `sources/` 나 KB 루트와 **같은 디렉터리인 지점**을
찾습니다. 문자열을 맞춰 보는 것이 아니므로 다음 두 경우가 모두 정상 동작합니다.

- `sources/` 가 심링크인 KB. 심링크를 따라간 실제 경로로 지정해도 같은 ref 로
  환원됩니다.
- `--target` 을 원본 경로와 다른 대소문자로 적은 경우(대소문자 비구분
  파일시스템). `Path.resolve()` 는 대소문자를 정준화하지 않지만 파일시스템은
  같은 디렉터리로 봅니다.

이 판정은 `ingest` 의 것보다 **엄격합니다.** `ingest` 는 환원한 문자열을
`relative_to` 로 비교하므로(`cli.py`), 대소문자가 어긋난 `--target` 을 주면
`sources/` 안의 파일을 밖으로 보고 **평면 변환본**을 파일명만 적힌 헤더와 함께
만듭니다. `eject` 는 같은 인자를 `sources/` 안으로 환원하므로 그렇게 만들어진
변환본을 짝지을 수 없습니다 — 헤더가 파일명만 말하고 어느 디렉터리였는지는 말하지
않기 때문입니다. 이때 `--delete-original` 로 원본을 지우면 그 변환본이 고아가 되므로,
추측하는 대신 어떤 변환본이 남는지 알려주고 `factlog eject --orphans` 를 안내합니다.

`--delete-original` 로 원본까지 지우려면 원본의 KB 기준 ref(`sources/sub/report.html`)
나 절대 경로로 지정하십시오. `sources/` 기준 경로(`sub/report.html`)는 그 경로에서
만들어진 **변환본**을 가리킵니다. 이때 `--delete-original` 은 지울 원본이 0건이라고
찍으면서, 원본까지 포함시키려면 어떤 철자를 써야 하는지 함께 알려줍니다.

`sources/` 밖의 원본을 적재했다면(예: `factlog ingest /elsewhere/report.html`)
미러링할 서브트리가 없어 변환본이 `runs/sources/` 바로 아래 평면으로 만들어집니다.
그 원본을 경로로 지정하면 평면 변환본만 매칭되고, 서브디렉터리의 변환본은 그
경로에서 만들어졌을 수 없으므로 절대 걸리지 않습니다.

단, **같은 이름의 원본이 `sources/` 아래 어느 디렉터리에든 이미 있으면 아무것도
매칭하지 않고 종료 코드 1 로 끝납니다.** `ingest` 는 `sources/` 밖 원본의 파일명만
헤더에 적으므로, 평면 변환본은 그 이름을 가진 파일 중 *어느* 것에서 나왔는지 말할 수
없습니다. KB 가 그 이름의 원본을 직접 들고 있다면 그쪽이 답이고, `eject` 는 확인
프롬프트 없이 파일을 지우므로 모호한 지정은 거부합니다. 최상위뿐 아니라
`sources/sub/report.html` 처럼 하위 디렉터리에 있어도 마찬가지입니다 — `--orphans`
가 짝을 판정할 때 쓰는 기준과 같습니다.

### 사실 하나만 제거 (`--fact`)

소스 자체는 멀쩡한데 추출된 사실 하나가 잘못된 경우, 그 사실만 폐기할 수
있습니다 — 소스의 변환본과 원본은 그대로 남습니다.

```bash
factlog eject --fact "을서비스" "정식_운영" "2030.1"      # retire one fact (mark superseded)
factlog eject --fact "갑봇" "통합" "을서비스" --fact "값가" "대체" "값나"   # several at once
factlog eject --fact "을서비스" "정식_운영" "2030.1" --purge   # delete the candidate row instead
```

사실은 **모든** 소스에 걸쳐 그 `(subject, relation, object)` 트리플로 매칭됩니다.
기본값인 `superseded` 는 `runs/*.json` 을 건드리지 않으므로 폐기가 내구성을
가집니다 — 이후 `/factlog sync` 가 소스로부터 사실을 다시 주장하더라도
`merge_candidates` 가 그것을 계속 superseded 로 유지합니다. 반면 `--purge` 는 행을
삭제하고 `runs/*.json` 에서도 제거합니다. 소스가 여전히 그 사실을 주장한다면 재싱크
시 다시 추출되므로, 사실을 영구히 폐기하려면 기본값을 사용하십시오. fact 모드와
source 모드는 상호 배타적이며, `--delete-original` 은 `--fact` 와 함께 쓸 수
없습니다.

기본적으로 폐기된 사실은 `superseded` 로 표시되어(감사 목적으로
`facts/candidates.csv` 에 남음) `sources/` 아래 원본은 **유지**됩니다 — 따라서 다음
`/factlog sync` 때 다시 변환됩니다. 원본까지 제거하려면 `--delete-original` 을
넘기십시오. `accepted.dl` 은 재컴파일되어 엔진 입력에서 폐기된 사실이 즉시
빠집니다.

`runs/sources/` 변환본은 적재 출처 헤더를 통해 그것을 만들어 낸 원본과 묶여
있으므로, 두 원본이 어간을 공유하더라도 `eject report.docx` 가 `report.pptx` 의
변환본을 건드리지 않습니다. `pages/` 는 `eject` 로 재생성되지 않습니다 —
`/factlog sync` 를 실행해 맞추십시오. 기본값 `superseded` 는 현재 상태 기준의
폐기입니다. **텍스트** 원본을 `sources/` 아래 그대로 두면 다음 `/factlog sync` 가
그 사실을 다시 추출·주장하므로, 소스를 영구히 제거하려면 `--purge` 와/또는
`--delete-original` 을 넘기십시오.
