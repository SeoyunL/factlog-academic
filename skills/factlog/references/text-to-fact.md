# Text-to-Fact 운영 기준

이 문서는 Claude Code가 `sources/`에서 fact 후보를 추출할 때 따르는 기준입니다.

## 역할

당신은 fact 추출자입니다. 문서를 요약하는 것이 아니라, 원문에서 직접 확인되는 관계 후보를 `facts/candidates.csv`에 기록합니다.

## 입력 범위

- 입력 문서는 `sources/` 아래의 파일만 사용합니다.
- `pages/`, `facts/`, `decisions/`의 기존 내용은 중복 확인과 갱신 판단에만 사용합니다.
- `sources/` 밖의 임시 파일, 로그, 개인 메모는 fact 추출 입력으로 사용하지 않습니다.

## 지원 파일 포맷

fact 추출은 `sources/` 아래 파일을 **텍스트로 읽어** 수행합니다. 엔진(`merge_candidates.py`)은 `sources/`의 모든 파일을 source 경로로 추적하지만 내용을 파싱하지는 않으므로, 텍스트로 읽히는 파일만 실제로 추출됩니다. 비텍스트 파일이 있으면 `merge_candidates.py`가 경고를 출력합니다.

- **직접 지원**: `.md`, `.markdown`, `.txt` 등 UTF-8 텍스트. 원문 그대로 읽습니다.
- **plain text로 지원**: `.rst`, `.org`, `.csv`, 소스 코드 등 그 밖의 UTF-8 텍스트. 별도 파싱 없이 raw text로 취급합니다.
- **직접 미지원(변환 필요)**: `.docx`, `.pptx`, `.xlsx`, `.hwp`, 바이너리 `.pdf`, 이미지. `sources/`에 그대로 두면 source 경로로만 등록되고 fact는 생성되지 않습니다(조용한 미인제스트). pandoc·textutil 등으로 Markdown/텍스트로 변환한 뒤 변환본을 `sources/`에 넣으십시오.

## 출력 위치

- candidate fact는 `facts/candidates.csv`에 씁니다.
- 사람이 읽을 개념 설명은 `pages/`에 씁니다.
- 중복 개념, 애매한 관계명, 출처 부족, 충돌 후보는 `decisions/open-questions.md`에 씁니다.

## CSV 스키마

`facts/candidates.csv`의 헤더는 반드시 다음 한 줄입니다.

```csv
subject,relation,object,source,status,confidence,note
```

각 행은 검증 가능한 관계 명제 하나만 담습니다.

`confidence`는 source 문서가 해당 subject-relation-object 관계를 얼마나 직접 뒷받침하는지 나타내는 0.00부터 1.00 사이의 점수입니다.
명시적으로 쓰여 있으면 높게, 관계명 선택이나 동일 개념 판단이 필요하면 낮게 둡니다.

## 상태 기준

- `confirmed`: source 문서가 subject-relation-object 관계를 직접 뒷받침합니다.
- `needs_review`: 관계명 선택, 동일 개념 여부, 출처 강도, 충돌 여부를 사람이 봐야 합니다.

LLM 출력은 최종 accepted fact가 아닙니다. 이 파일은 candidate fact 목록입니다.
`needs_review` 행을 만들었다면 같은 판단 이유를 `decisions/open-questions.md`의 알맞은 섹션에 bullet로도 남기십시오.

## 금지 항목

다음 항목은 fact로 저장하지 마십시오.

- API key, private token, password, session secret
- 주민등록번호, 전화번호, 이메일 주소 같은 개인정보 원문
- 고객명, 계정명, 내부 URL처럼 공개하면 안 되는 원문 식별자
- 문서에 없는 배경지식 또는 추론 결과
- "관련 있음"처럼 재사용하기 어려운 넓은 관계명

## 출처 표기

`source`는 항상 `sources/filename.md#section-name` 형식으로 씁니다. 섹션을 특정할 수 없으면 `sources/filename.md`까지는 반드시 남깁니다.

## 정리 원칙

- `sources/`에서 사라진 문서를 근거로 하는 candidate fact는 제거하거나 `decisions/open-questions.md`에 검토 대상으로 남깁니다.
- 같은 의미의 concept가 여러 이름으로 등장하면 자동 병합하지 말고 중복 후보로 기록합니다.
- 기존 페이지의 문장을 바꿀 때는 출처가 여전히 유효한지 먼저 확인합니다.
- `facts/candidates.csv`에 `needs_review`가 하나라도 있으면 `decisions/open-questions.md`에는 최소 한 개 이상의 검토 bullet이 있어야 합니다.
