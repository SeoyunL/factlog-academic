# 소스 제외와 제거 (`ignore` · `eject`)

> 🌐 [English](ignore-eject.en.md) | **한국어**

## sync에서 소스 제외하기 (`factlog ignore`)

`/factlog sync` 는 매 실행마다 **모든** 소스를 다시 추출합니다. 특정 소스를
그 대상에서 빼려면 — 초안, 작업 중인 문서, 외부 문서 등 — KB별 **sync-ignore
목록**(`policy/sync-ignore.md`)에 추가하십시오. 무시된 소스는 **수정되더라도**
`/factlog sync`, `factlog ingest --scan`, 커버리지 누락 보고에서 건너뜁니다. 이미
머지된 사실은 그대로 유지됩니다(사실을 실제로 제거하려면 `factlog eject` 사용).

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

### `eject` 를 거치지 않고 소스 파일을 직접 지웠다면

파일 관리자나 `rm` 으로 `sources/` 아래 파일을 지우면 그 소스를 인용하던 행은
`runs/*.json` 에 그대로 남습니다. 이후 merge 는 그 행을 `facts/candidates.csv` 에
쓰기 전에 드롭하고 stderr 로 알립니다.

```
  skip row: source 'sources/doomed.md' not found in sources/ ... (3 rows)
```

그 경고는 터미널이 스크롤되면 사라지고, `facts/candidates.csv` 는 이미 드롭된
**이후** 상태이므로 orphan citation(디스크에 없는 파일을 인용하는 사실)으로도 잡히지
않습니다. 그래서 커버리지 리포트는 `runs/*.json` 을 직접 읽어 이 상태를 별도로
보고합니다.

```
coverage: 12 source(s); 11 covered, 1 text gap(s), 0 binary needing conversion, 0 orphan citation(s), 1 run-cited source(s) missing
  RUN ROWS cite a missing source (dropped at merge, 3 row(s)): sources/doomed.md
```

이 필드는 해당 상태가 없으면 요약 줄에 출력되지 않으며, 종료 코드에도 영향을 주지
않습니다(`--strict` 는 여전히 텍스트 누락에만 반응합니다).

#### 정리 방법은 행이 `candidates.csv` 에 남았는지에 달려 있습니다

리포트가 소스별로 둘 중 하나를 알려주므로 출력 문구를 그대로 따르면 됩니다.

**(1) 행이 `candidates.csv` 에 남아 있는 경우** — 사람이 판정한 행
(`confirmed`/`accepted`/`needs_review`)은 [#218](https://github.com/SeoyunL/factlog-academic/issues/218)
래칫이 rebuild 를 거부하므로 유령 행이 표에 그대로 남습니다. 이때는
`factlog eject --orphans` 가 그 소스를 인식하고 한 번에 정리합니다.

```
  RUN ROWS cite a missing source (1 row(s); candidates.csv still carries rows for it): sources/doomed.md
  run rows cite 1 missing source(s) (1 row(s) total) that candidates.csv still carries; retire them with `factlog eject --orphans`
```

**(2) 행이 이미 사라진 경우** — 미판정(`candidate`) 행은 조용히 rebuild 되어 표에서
사라집니다. `eject` 는 인용 집합을 `facts/candidates.csv` 에서 만들기 때문에 이
소스를 보지 못하고 `no orphaned sources found` 로 끝납니다.

```
  RUN ROWS cite a missing source (dropped at merge, 3 row(s)): sources/doomed.md
  run rows cite 1 missing source(s) (3 row(s) total); inspect runs/*.json — `factlog eject --orphans` does not cover these (see #559)
```

> **(2) 는 `factlog eject --orphans` 로 정리되지 않습니다**(수정은
> [#559](https://github.com/SeoyunL/factlog-academic/issues/559) 에서 별도로
> 다룹니다). `runs/*.json` 을 직접 확인하거나, 지운 파일을 `sources/` 에 되돌린 뒤
> merge 를 다시 돌리고 `factlog eject <source> --purge --delete-original` 로 정식
> 제거하십시오.

읽을 수 없는 `runs/*.json` 이 있으면 그 파일은 집계에서 빠지며, 빠졌다는 사실을
stderr 에 한 줄로 알립니다(merge 역시 그 파일을 읽지 못합니다).

```
  skipped unreadable runs/2026-01-02-r.json — its rows are NOT in the counts above (merge cannot read it either)
```
