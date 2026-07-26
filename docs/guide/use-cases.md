# 사용 사례 — 일반 작업 흐름

> 🌐 [English](use-cases.en.md) | **한국어**

[개요](concepts.md#개요)에서 나열한 사용처(보고서·슬라이드·논문·코드 문서·데이터셋·노트·wiki)를
실제 명령 흐름으로 풀면 다음과 같습니다. 어느 경우든 골격은 같습니다 —
`sync`/슬래시는 **후보(candidate)** 만 만들고, 사람이 `factlog accept` 로
확정해야 **accepted** 가 되며, 그래야 `check`/`ask` 답변의 근거가 됩니다
([신뢰 경계](concepts.md#candidate-vs-accepted--신뢰-경계) 참고). 처음이라면 자기 데이터 없이 흐름을
완주해 보는 [빠른 시작 튜토리얼](../../examples/sample-kb/README.md)부터 시작하세요.

> **모든 명령은 Claude Code 세션에 그대로 입력합니다 — 터미널을 따로 열 필요가
> 없습니다.** `/factlog ...` 는 스킬을 부르는 slash command 이고, `factlog ...` 는
> Claude 가 세션 안에서 대신 실행해 주는 CLI 명령입니다. 어느 쪽이든 같은 결정론
> 엔진을 호출하며, 후보를 accepted 로 확정할지는 여전히 **사람이 `factlog accept`
> 를 입력해서** 결정합니다.

**보고서 검증하기**

- 보고서 파일을 KB의 `sources/` 에 둔다
- `/factlog sync` 로 후보 사실을 추출한다
- `factlog review` 로 후보를 확인하고 `factlog accept` 로 승인한다
- `/factlog check` 또는 `/factlog ask` 로 accepted 사실 기준 답변을 확인한다

**슬라이드(PPT) 발표자료의 주장·근거 점검하기**

- `.pptx` 파일을 그대로 KB의 `sources/` 에 둔다 (내장 변환기가 자동으로 텍스트로 바꾼다)
- `/factlog sync` 로 슬라이드의 주장을 후보 사실로 뽑는다
- `factlog review` 로 검토하고, 근거가 분명한 주장만 `factlog accept` 로 승인한다
- `/factlog ask` 로 승인된 주장끼리 모순이 없는지 확인한다
- 슬라이드는 변환 범위에 주의한다 — 슬라이드에 표시되는 텍스트만 읽고 발표자 노트는 제외하며, **표는 셀당 한 줄로 펼쳐져 행/열 대응이 사라진다**(수치 근거를 검토할 때 원본 표를 함께 봐야 한다). 구형 `.ppt` 는 변환기가 없으니 `.pptx` 로 저장해 둔다. 자세한 내용은 [소스 파일 형식](../reference/sources.md) 참고

**논문·기술 문서의 핵심 주장 정리하기**

- 논문이나 설계 문서를 KB의 `sources/` 에 둔다
- `/factlog sync` 로 핵심 주장을 후보 사실로 추출한다
- `factlog review` 로 검토하고 `factlog accept` 로 확정한다
- `/factlog check` 로 accepted 사실에 대한 질문을 검증한다

**이미 운영 중인 wiki 유지하기**

- wiki 문서를 KB의 `sources/` 로 가져온다
- `/factlog sync` 로 문서 변경에서 후보 사실을 갱신한다
- `factlog review` 로 새 후보를 확인하고 `factlog accept` 로 승인한다
- `/factlog check` 로 누적된 accepted 사실의 일관성을 점검한다

**특정 사실의 출처 추적하기**

- `factlog search` 로 확인하려는 사실을 찾는다
- `factlog provenance` 로 그 사실이 어느 소스에서 왔는지 추적한다
- 자세한 사용법은 [사실의 출처 추적](../reference/search-provenance.md#사실의-출처-추적-factlog-provenance) 참고

**잘못 추출된 후보 정리하기**

- `/factlog sync` 로 후보 사실을 만든다
- `factlog review` 로 후보를 검토한다
- 잘못 추출된 후보는 `factlog reject` 로 폐기하고, 표현만 다듬을 후보는 `factlog amend` 로 값을 고친 뒤 `factlog accept`(또는 `factlog amend --accept`)로 승인한다
- 자세한 사용법은 [사실 검토](../reference/review.md) 참고
