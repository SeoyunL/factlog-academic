# 단일값 관계 (`policy/single-valued.md`)

> 🌐 [English](single-valued.en.md) | **한국어**

여기 나열한 관계는 **한 subject 당 object 를 하나만** 가질 수 있습니다. 모순을 조용히
공존하는 두 사실이 아니라 **에러**로 만드는 장치이며, 평범한 노트 위키가 해주지 못하는 바로
그 일입니다.

```
# policy/single-valued.md
published_year
`연구 유형`
```

한 줄에 관계명 하나입니다. `#` 주석과 `-` 불릿을 쓸 수 있고, 공백이 든 이름은 백틱으로
감쌉니다. 나열하지 않은 관계는 한 subject 에 여러 object 를 가질 수 있으며, `cites` 나
`mentions` 같은 관계에는 그것이 옳은 기본값입니다.

같은 (subject, 단일값 관계) 에 서로 다른 object 두 개가 단언되면 `CONFLICT` 로 보고되고,
사람이 해소할 때까지 KB 는 컴파일을 거부합니다. 모순을 **보는** 방법은 `factlog status`
(`conflicts: N`), `tools/check_conflicts.py` (각 모순과 해소 단계를 출력), 또는 Claude Code
안에서 `/factlog check` 입니다. **해소**는 행을 물리려면 `factlog eject --fact SUBJECT
RELATION OBJECT`, 값을 고치려면 `factlog amend SUBJECT RELATION OBJECT --set-object NEW`
입니다. `facts/candidates.csv` 를 손으로 고치는 것은 이 KB 가 세운 사람 게이트를
우회하므로 하지 않습니다. 두 값이 상위-하위유형이라면 어느 쪽도 틀리지 않았습니다 —
[`policy/value-hierarchy.md`](value-hierarchy.md) 에 관계를 선언하면 두 행 모두 유지됩니다.

`factlog init` 이 주석 처리된 예시와 함께 이 파일을 만들어 둡니다.
