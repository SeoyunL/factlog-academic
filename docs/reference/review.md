# 사실 검토

> 🌐 [English](review.en.md) | **한국어**

## 사실 검토 (`factlog review` / `accept` / `reject`)

추출은 사실을 `candidate` 또는 `needs_review` 로 표시하며, `confirmed`/`accepted`
사실만 엔진 입력이 됩니다. `facts/candidates.csv` 를 직접 손대지 않고 승격하거나
폐기할 수 있습니다.

*Claude Code에 입력 (후보를 사람이 검토·승인하는 게이트):*

```bash
factlog review                       # list the pending queue (candidate + needs_review)
factlog review --status needs_review # narrow to one pending status
factlog accept Acme uses FastAPI     # pending → accepted (compiled into accepted.dl)
factlog accept Acme                  # accept every pending fact about a subject ('-' wildcards a position)
factlog reject Acme uses Datadog     # pending → superseded (retired, kept for audit)
factlog accept Acme uses FastAPI --dry-run
```

`accept`/`reject` 는 **대기(pending) 행만** 변경합니다. `confirmed`/`accepted`/
`superseded` 와 일치하는 항목은 보고만 되고 그대로 유지됩니다(대기 상태가 아닌
사실을 폐기하려면 `factlog eject` 를 사용). 둘 다 `accepted.dl` 을 재컴파일합니다.

`accept`/`reject` 는 `candidates.csv` 뿐 아니라 그 근거인 `runs/*.json` 에도 결정을
기록합니다. merge 가 `candidates.csv` 를 `runs/*.json` 으로부터 재구성하므로, 그렇게
하지 않으면 결정이 다음 sync 에서 조용히 사라집니다. **이 기록은 게이트가 실제로
바꾼 행에만 적용됩니다.** 여기서 "행"은 트리플이 아니라 merge 와 동일한 사실 동일성
`(주어, 관계, 목적어, source 파일)` 입니다(`#앵커` 는 무시). 따라서 같은 트리플이
서로 다른 문서에서 주장됐다면, 한쪽 문서의 결정이 다른 문서의 근거 행을 건드리지
않습니다. 대기 상태가 아니어서 "skipped" 로 보고된 행 역시 `runs/*.json` 에서 그대로
남습니다. `amount` 객체는 merge 와 같은 정규 형태 `amount(N,"단위")` 로 비교되므로
`amount(7,억)` 과 `amount(7,"억")` 은 한 사실입니다.

주어·관계·목적어·source 는 모두 **NFC 로 정규화**해 비교하고 저장합니다. 그래서
눈에 같아 보이지만 유니코드 표기(NFC/NFD)만 다른 한글 값은 **하나의 사실**입니다 —
한쪽을 `accept` 하면 다른 표기의 근거 행에도 결정이 닿고, `candidates.csv` 에는 NFC
로 접힌 한 행만 남습니다. 붙여넣기나 macOS 파일명에서 표기가 섞여 들어와도 merge 가
같은 사실로 접으므로, 사람이 손으로 표기를 맞출 필요가 없습니다. (이 정체성은 엔진의
그룹화 축과도 일치합니다 — 엔진 역시 NFC 로 접습니다.) 이전 표기 정책으로 쌓인
`candidates.csv` 를 NFC 로 다시 접으려면 일회성 명령 `factlog migrate-unicode` 를
쓰십시오. 기본은 충돌 리포트만 출력해 안전하고, `--resolve-status=priority` 를 줄
때만 즉시 `candidates.csv` 를 재작성합니다(대화형 확인 없음). 이 명령은 `--target`
없이 활성 KB 로 가므로 priority 를 쓸 때는 `--target` 로 대상을 확인하십시오.
priority 는 은퇴된(superseded) 행을 confirmed/accepted 로 덮어 되살릴 수 있으니,
은퇴 유지가 필요하면 그 그룹은 `amend` 로 개별 처리하십시오. 또한 충돌 그룹만
접으므로 짝 없는 단독 NFD 행은 그대로 남습니다 — 전 필드 NFC 통일을 완결하려면
재-merge(`/factlog sync` 또는 `merge_candidates.py`)를 돌리십시오.

경계: `candidates.csv` 는 `confirmed` 인데 `runs/*.json` 은 아직 `candidate` 인
드리프트(#233 이전 KB)를 되돌리는 일은 `accept`/`reject` 의 부수효과가 아닙니다.
`accept`/`reject` 는 자기가 방금 내린 결정만 기록하며, 드리프트 복구는 별도 명령이
할 일입니다.

상태가 아니라 사실의 **값 자체를 교정**하려면 `factlog amend` 를 사용하십시오.

*Claude Code에 입력:*

```bash
factlog amend Widget codename Draft --set-object Falcon --set-note "name finalized" --accept
factlog amend Acme uses FastApi --set-object FastAPI    # fix a typo
```

위치 트리플이 사실을 식별하고(정확히 일치), `--set-subject` / `--set-relation` /
`--set-object` / `--set-note` 가 새 값을 줍니다(최소 하나, 또는 `--accept`). amend
는 `candidates.csv` **와** 그 근거가 되는 `runs/*.json` 을 **둘 다** 갱신하므로
편집이 `/factlog sync` 후에도 살아남습니다(사실의 값은 `runs/*.json` 에 있으며,
merge 가 그로부터 `candidates.csv` 를 재구성합니다). `--accept` 는 `accepted` 로
승격까지 합니다. 신뢰도는 편집할 수 없습니다. `--dry-run` 으로 미리 볼 수 있습니다.

### 상태의 종류

사실의 `status` 는 세 부류로 나뉩니다.

| 부류 | 상태 값 | 의미 |
|------|---------|------|
| **대기(pending)** | `candidate`, `needs_review` | 추출됐지만 아직 사람의 결정을 기다리는 중. `factlog review` 큐에 뜹니다. |
| **엔진 입력** | `accepted`, `confirmed` | 사람이 확정한 사실. **이 두 상태만 `accepted.dl` 로 컴파일**되어 엔진 입력이 됩니다. |
| **폐기(retired)** | `superseded` | 물러난 사실. 감사(audit)를 위해 `candidates.csv` 에 남지만 엔진 입력이 아니며, 모순 검출에서도 무시됩니다. |

### 상태 전이표

| 현재 상태 | `accept` | `reject` | `amend --set-*` | `amend --accept` |
|-----------|----------|----------|-----------------|------------------|
| `candidate` | → `accepted` | → `superseded` | 값 교정 (상태 유지) | 값 교정 + → `accepted` |
| `needs_review` | → `accepted` | → `superseded` | 값 교정 (상태 유지) | 값 교정 + → `accepted` |
| `accepted` | 변경 없음 (보고 후 종료 코드 1) | 변경 없음 (보고 후 종료 코드 1) | 값 교정 가능 | 값 교정 (이미 `accepted`) |
| `confirmed` | 변경 없음 (보고 후 종료 코드 1) | 변경 없음 (보고 후 종료 코드 1) | 값 교정 가능 | 값 교정 + → `accepted` |
| `superseded` | 변경 없음 (보고 후 종료 코드 1) | 변경 없음 (보고 후 종료 코드 1) | **대상 아님** — `no fact matches` (종료 코드 1) | **대상 아님** — `no fact matches` (종료 코드 1) |

읽는 법:

- **`accept`/`reject` 는 대기 상태에서만 나가는 간선을 만듭니다.** 대기가 아닌 행만
  일치하면 아무것도 바꾸지 않고 안내와 함께 종료 코드 1로 끝납니다.

  ```text
  factlog accept: 1 matching row(s) are not pending (already confirmed/accepted/superseded);
  nothing to change. Use `factlog eject` to retire a non-pending fact.
  ```

- **`amend` 는 상태가 아니라 값을 다룹니다.** 그래서 `accepted`/`confirmed` 처럼 이미
  확정된 사실의 오타도 고칠 수 있습니다 — `accept`/`reject` 로는 손댈 수 없는
  영역입니다.
- **`superseded` 행은 `amend` 의 대상이 아닙니다.** 이전 `amend` 가 남긴 묘비
  (tombstone)를 다시 겨냥하면 폐기된 값이 되살아나므로, `amend` 는 폐기되지 않은
  행만 찾습니다. 일치하는 살아 있는 행이 없으면 `no fact matches` 입니다.

전이가 **일어나지 않는** 경우도 표에 있습니다. 어떤 명령도 `accepted` → `candidate`
같은 역방향 강등을 하지 않으며, 대기 상태로 되돌리는 간선은 없습니다.

전이가 없거나(일치 행 없음, 대기 아님) 인자가 잘못된 경우의 종료 코드는 다음과
같습니다.

| 상황 | 종료 코드 |
|------|-----------|
| 전이 성공 | 0 |
| `--dry-run` (미리보기만) | 0 |
| 트리플에 일치하는 행 없음 (`no fact matches`) | 1 |
| 일치하지만 전부 대기 아님 (`nothing to change`) | 1 |
| 상태는 저장됐으나 `accepted.dl` 재컴파일 실패 | 1 |
| 인자 오류 (트리플 항이 3개 초과, 하나도 안 줌, `amend` 에 `--set-*`/`--accept` 없음) | 2 |

재컴파일이 실패해도 **상태 변경 자체는 이미 `candidates.csv` 에 저장된 뒤**이며,
`/factlog check` 로 `accepted.dl` 만 다시 만들면 됩니다.

> **내구성(durability):** 사람이 한 `accept`(및 `amend --accept`)는 `reject`/
> `superseded` 와 같은 방식으로 재머지 후에도 보존됩니다 — `/factlog sync` 가
> 여러분의 결정을 되돌리지 않습니다.
