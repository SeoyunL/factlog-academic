# 검토·검증 시작하기 (초심자용)

`/factlog sync`까지 했는데 "그다음 뭘 하지?"에서 막혔다면 이 문서를 보세요. **명령만으로**
끝까지 갈 수 있고, 처음엔 **파일을 직접 편집할 필요가 전혀 없습니다.**

## 큰 그림

```
소스(sources/) → [추출] → 후보(candidate) → [사람 승인] → 사실(accepted) → [검증]
```

- **추출·검증**은 Claude Code에서 `/factlog ...` 슬래시 명령으로 (LLM/엔진이 함).
- **승인**은 사람이 터미널에서 `factlog ...` 명령으로 (신뢰 경계 = P1).

## 최소 경로 (이것만 하면 됩니다)

| 순서 | 위치 | 명령 | 하는 일 |
|---|---|---|---|
| 1 | Claude Code | `/factlog sync` | 소스에서 **후보** 추출 → `facts/candidates.csv` |
| 2 | 터미널 | `factlog review` | 후보 목록 확인 (아직 사실 아님) |
| 3 | 터미널 | `factlog accept ...` | **사람 승인** → 사실로 확정 |
| 4 | Claude Code | `/factlog check` | 결정론 엔진으로 논리 검증 |

`review`는 마지막 줄에서 **정확히 어떤 accept 명령을 쓸지** 알려줍니다. 그대로 복사해 쓰면 됩니다.

## 자주 막히는 3가지 — 실은 안 막혀도 됩니다

### 1. "candidates.csv의 status를 accepted로 바꿔라"

> **CSV를 직접 열지 마세요.** `factlog accept` 명령이 상태를 바꿔 줍니다. (`candidates.csv`는
> 엔진 산출물이라 손으로 편집하면 안 됩니다.)

```bash
factlog review                          # 각 후보를  "주어 / 관계 / 목적어"  로 보여줌
factlog accept --dry-run "FastAPI"      # 무엇이 승인될지 먼저 미리보기
factlog accept "FastAPI"                # 주어가 FastAPI인 후보 승인
factlog accept - "uses" -               # 관계가 uses인 후보 전부 (한 자리만 구체값이면 OK)
factlog reject "잘못된 주어"             # 폐기
factlog amend "A" "rel" "B" --set-object "B2" --accept   # 값 고쳐서 승인
```
인자는 `주어 [관계 [목적어]]` 이고 각 자리에 `-`를 쓰면 와일드카드입니다. 단 **최소 한 자리는
구체값**이어야 합니다(`- - -` 전부 와일드카드는 안 됨 — 눈으로 확인 없이 무더기 승인하지 말라는
취지). `review`가 보여준 `주어 / 관계 / 목적어`를 그대로 넣으면 됩니다.

### 2. "logic-policy.md에 Datalog 규칙을 정의하라"

> **초심자는 건너뛰세요. 규칙이 하나도 없어도 됩니다.** 규칙이 비어 있어도 `/factlog check`는
> 정상 동작합니다(승인한 사실의 컴파일·기본 무결성만 검사, 위반 0건으로 통과).

Datalog 규칙은 **"A이면서 B이면 모순"** 같은 *추가 논리 제약*을 걸고 싶을 때만 쓰는 **고급/선택**
기능입니다. 안 써도 factlog의 추출→승인→검증 루프는 완전히 돌아갑니다. 나중에 필요해지면
그때 배우면 됩니다.

### 3. "canonical-alias / 표준 별칭을 정의하라"

> **초심자는 건너뛰세요.** 같은 대상을 여러 이름으로 부를 때(예: `FastAPI` = `fast api`)
> 하나로 통일하는 **고급/선택** 기능입니다. 기본 사용에는 필요 없습니다.

## 충돌(conflict)이 났다면

`/factlog check`가 "두 사실이 모순"이라고 하면, 초심자는 가장 단순하게:

- 잘못된 후보를 **`factlog reject`** 로 폐기하거나,
- **`factlog amend ... --accept`** 로 값을 고쳐서 다시 승인하세요.

> 참고: 오래된 사실을 **감사 기록으로 남기며** 물러나게 하는 `superseded` 처리는 고급 주제이며,
> 현재는 전용 명령이 없습니다(관련 개선은 이슈로 추적 중). 처음엔 위의 reject/amend로 충분합니다.

## 한 줄 요약

**`/factlog sync` → `factlog review` → `factlog accept` → `/factlog check`.**
Datalog도, 별칭도, CSV 편집도 처음엔 필요 없습니다.
