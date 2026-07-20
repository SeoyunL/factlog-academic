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
