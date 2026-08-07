# 결정론과 한계

> 🌐 [English](determinism.en.md) | **한국어**

스킬은 곧 프롬프트이므로, 모델은 각 단계를 실행하도록 *유도*될 뿐 *강제*되지는
않습니다. factlog는 신뢰성이 필수인 모든 단계 — 사실 컴파일, wirelog 로직 체크,
정책 컴파일, 검증 — 를 모델의 판단이 아니라 **스킬이 실행하고 신뢰하도록 지시받는
번들 스크립트**로 유지합니다. 로직 체크 리포트는 언제나 엔진이 생성하며, 모델이
서술하지 않습니다.

### AC4 — 오래된 편집 방지 (두 단계)

factlog는 두 가지 서로 다른 메커니즘으로 신선도(freshness)를 강제합니다.

| 단계 | 메커니즘 | 보장하는 것 |
|-------|-----------|-------------------|
| **훅으로 강제** | `facts/logic_report.txt` 가 없거나, 대상 파일보다 오래되었거나, 엔진이 돌지 못한 실행을 기록하고 있을 때, `PreToolUse` 훅이 `facts/accepted.dl` 또는 `facts/query.dl` 로의 모든 `Write`/`Edit` 를 거부합니다(`/factlog check` → `run_logic_check.py` 로 갱신) | 로직 리포트가 오래된 상태에서는 엔진의 컴파일된 입력을 덮어쓸 수 없습니다 — 훅이 파일에 손대기 전에 도구 호출을 차단합니다 |
| **SKILL 규율 (최선 노력)** | `SKILL.md` 는 어떤 결론을 말하기 전에 Claude가 `run_logic_check.py` 를 실행하고 `facts/logic_report.txt` 를 그대로 보여 주도록 지시합니다 | 모델은 엔진 리포트를 드러내도록 *유도*되지만 *강제*될 수는 없습니다(R10: "완전히 보장할 수 없음") — 원시 리포트에 대한 사람의 검토가 최종 검증 단계입니다 |

이 두 단계는 상호 보완적입니다. 훅은 결정론적 빈틈을 메우고, SKILL 규율은
엔지니어링적 강제가 불가능한 서술(narration) 계층을 담당합니다.

> **기존 KB 를 쓰고 있었다면 한 번은 `/factlog check` 가 필요합니다.** 이전
> 버전에서는 훅이 Claude Code 의 실제 페이로드를 읽지 못해 이 거부가 한 번도
> 발동하지 않았습니다. 이번 수정 이후로는, `facts/accepted.dl` 이나
> `facts/query.dl` 이 이미 있는데 `facts/logic_report.txt` 가 없거나 낡은 KB
> 에서는 `/factlog check` 로 리포트를 먼저 갱신해야 엔진 입력을 편집할 수
> 있습니다.

훅은 신선도 외에 **한 가지 사유로 더** 거부합니다. 훅은 Claude Code 가 보내는
도구 페이로드에서 대상 경로를 읽어 판정하는데, `Write`/`Edit` 호출이면서
페이로드의 `tool_input` 이 객체로는 들어왔는데 그 안에서도 최상위에서도 읽을 수
있는 경로 키가 없으면 — 즉 그 쓰기가 엔진 입력을 겨냥하지 *않는다*는 것을 보일 수
없으면 — 통과시키지 않고 거부합니다. 빈 문자열 `file_path` 도 여기 해당합니다.

이 조건은 좁습니다. `tool_input` 이라는 봉투 키 **자체가** 없어지거나 이름이
바뀌는 경우는 여기 들어오지 않고 통과합니다 — 그 형태의 변화는 모든 세션의 모든
`Write`/`Edit` 를 한꺼번에 때리므로, 거부하면 게이트가 아니라 전역 쓰기 장애가
되기 때문입니다. 대신 그 갈래는 "검사를 건너뛰었다" 는 알림을 남깁니다.

`FACTLOG_GATE_ALLOW_UNREADABLE_PAYLOAD=1` 은 위의 거부 **한 갈래만** 면제합니다.
이 변수로 **신선도 거부도 Python 부재 거부도 풀리지 않습니다.** 이건 세션 안에서
모델이 스스로 할 수 있는 일이 아닙니다 — 훅은 Claude Code 프로세스의 환경을
물려받고 그 환경은 Claude Code 가 시작될 때 고정되므로, **사람이** 설정한 뒤 새
세션을 시작해야 합니다. `settings.json` 의 `env` 블록에 넣거나 Claude Code 를
띄우기 전에 export 하세요. 그리고 읽지 못한 페이로드 형태를 업스트림에 알려
주세요.

### 리포트를 만들 수 없는 KB 에서 거부가 풀리지 않을 때

신선도 거부 메시지는 `/factlog check` 를 안내하지만, `/factlog check` 자체가
실패하는 KB 가 있습니다. 예를 들어 `facts/query.dl` 은 있는데
`facts/accepted.dl` 이 없으면 로직 체크가 엔진을 띄우기 전에 멈춥니다. 엔진
`pyrewire` 가 없거나 버전이 낮을 때도 마찬가지입니다.

이때에도 로직 체크는 대개 `facts/logic_report.txt` 를 쓰며, 그 리포트는 실패를
기록합니다. 앞부분은 이렇게 생겼습니다.

```
Logic Check Report
==================
status: engine-did-not-run
engine: wirelog / pyrewire
input: facts/accepted.dl
reason: missing facts/accepted.dl; run tools/compile_facts.py first
reason type: FactlogError
...
```

`status:` 줄은 "이 리포트는 KB 에 대해 아무것도 말하지 않는다" 는 뜻입니다.
숫자는 하나도 싣지 않습니다 — `engine facts: 0` 은 엔진이 돌았는데 아무것도
못 찾았다는 뜻이 되므로 쓰지 않습니다. 직전 성공 실행의 리포트가 있었다면
덮어쓰므로, 예전 결과를 이번 실행의 결과로 잘못 읽을 일도 없습니다.

"대개" 인 이유는 두 가지입니다. 로직 체크가 시작되기도 전에 죽으면 — 예를 들어
엔진 패키지 자체를 불러오지 못하면 — 리포트를 쓸 코드에 닿지 못합니다. 그리고
`facts/` 에 쓸 수 없으면 리포트 쓰기는 포기하고 원래 오류를 그대로 보여 줍니다.
리포트보다 원인 진단이 우선이기 때문입니다. 두 경우 모두 직전 리포트가 그대로
남으므로, 리포트의 내용이 이번 실행의 것인지 확인하려면 `/factlog check` 의
출력을 함께 보세요.

**이 리포트로는 거부가 풀리지 않습니다.** 엔진까지 가지 못한 실행은 엔진 입력을
편집해도 된다는 근거가 아니므로, `status:` 줄이 있는 동안 게이트는 계속
거부합니다. 달라지는 것은 거부 메시지가 방금 실패한 명령을 다시 가리키는 대신
원인을 그대로 알려 준다는 점입니다. 하나는 그대로입니다. 그 KB 에서
`facts/query.dl` 이나 `facts/accepted.dl` 을 **처음** 만드는 쓰기는 리포트가
없을 때와 똑같이 허용됩니다.

훅은 `Write` 와 `Edit` 에만 걸리므로 복구는 **Bash 로** 합니다. 컴파일을 먼저
돌려 `facts/accepted.dl` 을 만들면 풀립니다. 단 `facts/candidates.csv` 가 없으면
컴파일 자체가 `missing facts/candidates.csv` 로 멈추므로, 없을 때만 헤더 한 줄을
먼저 만들어 줍니다. `factlog init` 은 이 파일을 다시 만들어 주지 않습니다.

```bash
cd <KB 루트>
[ -f facts/candidates.csv ] || \
  echo 'subject,relation,object,source,status,confidence,note' > facts/candidates.csv
"${CLAUDE_PLUGIN_ROOT}"/tools/factlog_python.sh "${CLAUDE_PLUGIN_ROOT}"/tools/compile_facts.py
"${CLAUDE_PLUGIN_ROOT}"/tools/factlog_python.sh "${CLAUDE_PLUGIN_ROOT}"/tools/run_logic_check.py
```

빈 `candidates.csv` 로 컴파일하면 사실이 0건인 `accepted.dl` 이 만들어집니다.
그것으로 로직 체크가 돌고 리포트가 생겨 거부가 풀립니다 — 기존 사실을 지우지
않습니다. 이미 `candidates.csv` 가 있다면 그대로 컴파일됩니다.

작성 중이던 질의를 버려도 된다면 `facts/query.dl` 을 다른 이름으로 옮기는 것도
방법입니다. 엔진 입력이 없으면 거부의 전제가 사라집니다.

로직 체크가 다른 이유로 실패한다면 그 오류를 먼저 해결해야 합니다. 게이트를
우회해 엔진 입력을 고치는 것은 이 거부가 막으려는 바로 그 동작이므로, 탈출구는
두지 않았습니다.

### 규모와 성능

**성능 때문에 KB를 비울 필요는 없습니다.** 로직 체크 비용은 총 사실 수보다
**엔티티 간 관계**(A→B에서 B가 다시 subject가 되는 엣지) 수에 더 민감합니다 —
엔진이 도달성(path)을 계산하기 때문입니다. object가 대체로 리터럴인 속성 위주
KB는 수만~수십만 사실까지 저렴하게 확장되고, 촘촘한 엔티티 그래프(인용·의존망
등)는 그보다 빨리 무거워질 수 있습니다. 따라서 지켜볼 지표는 총 사실 수가 아니라
**엔티티↔엔티티 엣지 수**입니다.

무거워지더라도 해법은 "비우기"가 아닙니다. 관계 설계를 조정하고,
`factlog ignore`(재추출 제외)와 멱등 ingest로 반복 비용을 관리하면 됩니다.
정확성과 중복 방지는 규모와 무관하게 그대로 유지됩니다.
