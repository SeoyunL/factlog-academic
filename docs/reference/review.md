# 사실 검토

> 🌐 [English](review.en.md) | **한국어**

## 사실 검토 (`factlog review` / `accept` / `reject`)

추출은 사실을 `candidate` 또는 `needs_review` 로 표시하며, `confirmed`/`accepted`
사실만 엔진 입력이 됩니다. `facts/candidates.csv` 를 직접 손대지 않고 승격하거나
폐기할 수 있습니다.

*터미널에서 실행 (후보를 사람이 검토·승인하는 게이트):*

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

상태가 아니라 사실의 **값 자체를 교정**하려면 `factlog amend` 를 사용하십시오.

*터미널에서 실행:*

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
