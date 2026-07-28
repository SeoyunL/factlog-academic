# AGENTS.md

## 협업 표기 원칙

커밋 메시지, PR 제목, 브랜치 이름, 릴리스 노트처럼 저장소 이력과 공개 협업 기록에
남는 메타데이터에는 어떤 에이전트와 협업했는지 등록하거나 표기하지 않는다.

코드 변경에 대한 책임은 커밋 작성자, PR 작성자, 리뷰어, 머지 권한자에게 있다. 어떤
도구나 에이전트를 사용했는지는 책임 주체를 대체하지 않으며, 권한 있는 사람이 보낸
PR과 커밋 사용자 정보만으로 변경의 소유권과 책임을 추적하기에 충분하다.

따라서 다음과 같은 표기는 사용하지 않는다.

- 커밋 메시지의 `Generated with ...`, `Co-authored-by: ... agent`
- PR 제목이나 본문에 특정 에이전트명으로 작성 사실을 강조하는 문구
- 브랜치 이름, 태그, 변경 로그에 에이전트명을 포함하는 관례

필요한 경우에는 구현 의도, 검증 방법, 남은 위험처럼 코드 리뷰에 직접 도움이 되는
정보를 기록한다.

## 주석·문서의 주장 규율

주석이 코드보다 많이(또는 적게) 주장하는 결함이 `hooks/gate_check.sh` 계열에서만
다섯 번, `tools/ask_router.py` 에서 세 번 나왔다(#601 전수 점검). 개별 수정으로는
계속 재발했으므로 규칙으로 남긴다. 이 규칙은 새 주석과, 기존 주석을 건드리는 변경에
적용한다.

### 자동 의심 문구

다음 표현이 든 문장은 **뮤테이션이나 직접 측정으로 증명되기 전에는 쓰지 않는다.**

```
defensive / not load-bearing / changes no result / fails no test /
equivalent mutant / unreachable / cannot happen / incidentally /
the only ... / structurally / 항상 / 절대 / 모든 경우
```

수량·전칭 주장("N건", "두 경우", "모든 …")도 같은 계열이다. #589 에서 `"two cases"`
개수 표기가 두 번 어긋났다.

### 판정은 양방향이다

1. **과대** — 코드가 보장하지 않는 것을 주장한다. 검토자를 잘못 안심시킨다.
2. **과소** — 코드가 보장하는 것을 "안 한다"고 주장한다. 다음 사람에게 그 코드를
   지울 명시적 근거를 준다. **이쪽이 더 위험하다.** #594 의 `"defensive, not
   load-bearing, changes no result, and a mutant that removes it fails no test"` 는
   네 문장이 모두 틀렸고, load-bearing 인 NFC 폴드를 죽은 코드로 보이게 만들고
   있었다. `_ASCII_MIN` 주석(#601)도 같은 형태였다.
3. **근거 서술** — "측정했다 / N개 표본으로 골랐다"가 재현 가능한가. 재현되지 않으면
   근거 없는 상수다(#577 의 `"Six is where those two facts meet"`).

### 쓸 때 지킬 것

- **증명 방법을 문장 옆에 같이 적는다.** 무엇을 어떻게 죽였고, 어느 스위트가 몇 건
  움직였는지, 어느 커밋에서 쟀는지. 그래야 다음 사람이 다시 재는 대신 **전제가
  바뀌었는지**를 확인할 수 있다. 예: `tools/ask_router.py` 의 `coverage_hint` 방어
  가드 주석, 같은 파일 `decomposition_candidates` 의 두 조건.
- **측정과 보장을 갈라 적는다.** 이 저장소가 만들지 않는 프로그램의 성질은 보장이
  아니라 한 버전에 대한 측정이다 — `hooks/gate_check.sh` 의 matcher 주석(#596),
  `"a property of a program this repo does not ship, so it is a measurement, not a
  guarantee"`.
- **측정에는 정의와 시점을 붙인다.** 한 단어가 여러 뜻이면 재현이 안 된다(#601 에서
  "PROSE 도달 수"가 front matter 포함 여부에 따라 갈렸다). 살아 있는 KB나 자라는
  스위트를 상대로 잰 수치에는 **언제 잰 값인지** 적는다(`66 rows` 는 지금 77,
  `134 excerpts` 는 지금 169).
- **확인이 불가능하면 "확인 불가"라고 적는다.** 추측을 측정처럼 쓰지 않는다. 좋은
  형태가 `hooks/gate_check.sh` 의 `-ot` 잔여 위험에 있다 — `"That window is not
  measured here and is not claimed to be impossible."`
- **테스트 주석에서 판별력을 주장할 때는 그것을 죽이는 뮤턴트를 이름으로 적는다.**
  이중 방어라 안 죽는 행은 그렇다고 적는다(`tests/test_gate_check.sh` CASE 20 의
  DOUBLE-DEFENCE 표기). 새 deny 행에는 짝 allow 행을 두거나 사유를 단언한다(같은
  파일 CASE 11).
- **사용자 대면 보장 문구(`skills/*/SKILL.md`, `docs/`)에는 예외를 같이 적는다.**
  사용자는 없는 안전장치를 믿는다. #591 이 그 계열이었고, 게이트의 bootstrap 예외가
  같은 형태로 빠져 있었다(#601).
