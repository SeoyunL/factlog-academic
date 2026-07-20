# 값 어휘 감사 (`tools/value_audit.py`)

> 🌐 [English](value-audit.en.md) | **한국어**

관계 이름은 정책이 관리합니다. 그러나 **값은 관리되지 않습니다** — 추출할 때마다
하나씩 들어올 뿐이고, 같은 것이 두 문자열로 두 번 들어와도 아무도 알아채지
못합니다. 실제 KB에 `IL-10` 과 `기타(IL-10)` 이 둘 다 accepted 상태로 있었고, 그래서
`relation(P, "염증지표", "IL-10")?` 는 4건 중 3건만 돌려줬습니다. 나머지 하나는 다른
문자열 뒤에 숨어 있었습니다. 조용한 누락 — 이 KB가 막으려는 바로 그 실패입니다.

## 실행

```bash
python3 tools/value_audit.py --wiki ~/wiki                  # 보고 (기본은 항상 exit 0)
python3 tools/value_audit.py --wiki ~/wiki --strict         # 확실한 질의 누수가 있으면 non-zero
python3 tools/value_audit.py --wiki ~/wiki --all-statuses   # 엔진 입력이 아닌 후보 행까지 전부
```

- `--wiki` 는 KB 루트입니다. 생략하면 `$FACTLOG_ROOT` → **활성 KB 설정**(`factlog use`
  로 지정) → 현재 디렉터리 순으로 폴백합니다. 활성 KB가 설정돼 있으면 아무 디렉터리에서
  실행해도 **현재 디렉터리가 아니라 활성 KB**가 대상입니다 — `--strict` 결과를 CI 판정에
  쓴다면 `factlog where` 로 대상 KB를 먼저 확인하거나 `--wiki` 를 명시하십시오.
- 기본 감사 대상은 **엔진 입력**(`confirmed`/`accepted`) 행뿐입니다. `--all-statuses` 를
  주면 아직 승인되지 않은 후보까지 포함해 훨씬 시끄러워집니다.
- `candidates.csv` 가 아직 없는 새 KB에서는 `value_audit: no candidate facts` 를 출력하고
  exit 0 입니다. 자동화 안에서 traceback 을 내지 않기 위한 의도된 동작입니다.

## 발견 유형

값은 **같은 관계 안에서만** 비교하며(`염증지표` 의 값은 `대상질환` 의 값과 아무 상관이
없습니다), 모든 발견은 유사도 추측이 아니라 규칙입니다.

| 발견 | 의미 |
|---|---|
| **split wrapper** | `기타(IL-10)` 이 `IL-10` 과 공존 — 한 값이 두 번 기록됨. 지금 질의가 새고 있음. |
| **wrapper value** | `기타(INFLA-score)` — 감싼 안쪽 값이 아직 독립된 값으로 존재하지 않아, 제 이름(`INFLA-score`)으로 질의하면 아무것도 안 나옴. |
| **placeholder** | `기타`, `불명`, `미상`, `N/A`, `unknown`, `-` — 정보가 없고, 원문이 말한 바를 가림. |
| **spelling duplicate** | 대소문자·공백·문장부호를 접으면 같은 값(`IL-8` / `il 8`). 질의 누수다 — 단, **identity 관계**(아래)에서는 주체가 다른 충돌이 중복 *레코드* 의심이 된다. |

## `--strict` 의 의미

`--strict` 는 **증명 가능한 질의 누수**가 있을 때만 exit 1 입니다. 세는 것은 split
wrapper 와, `kind == "split"` 인 spelling duplicate 뿐입니다. identity 관계에서 서로
다른 주체가 같은 접힘값을 공유한 경우는 사람이 볼 가치는 있어도 누수가 아니므로 CI
게이트를 실패시키지 않습니다. wrapper value 와 placeholder 도 `--strict` 를 실패시키지
않습니다 — 위생 문제지 누수의 증명은 아니기 때문입니다.

## identity 관계 (`policy/identity-relations.md`)

제목이나 DOI는 논문 하나를 지목하지만, 발행연도나 연구유형은 그렇지 않습니다. 앞의
것들을 여기 선언합니다.

```markdown
# policy/identity-relations.md
제목
DOI
```

한 줄에 관계명 하나이고, `#` 주석과 `-` 불릿을 쓸 수 있으며, 공백이 든 이름은 백틱으로
감쌉니다(다른 정책 파일과 같은 문법입니다). 이름은 NFC로 정규화해 읽으므로 macOS에서
NFD로 저장된 파일도 accepted 사실과 맞물립니다.

identity 관계에서 두 주체가 접힘값을 공유하면 중복 *레코드*일 가능성이 큽니다 — 수리
방법이 다르고, `--strict` 도 실패하지 않습니다. 그 밖의 관계에서는 값이 여러 주체에
공유되는 게 정상이므로, 충돌은 한 값이 두 표기로 쪼개진 것 — 질의 누수이고 `--strict` 가
실패합니다. 선언이 하나도 없으면 모든 관계가 범주형이므로 충돌은 누수로 보고됩니다.
조용한 것보다 시끄러운 게 낫고, 리포트가 어느 관계를 선언하라고 알려줍니다.

identity는 **선언**하는 것이지 추론하는 것이 아닙니다. 감사기도 어느 관계가 여기 속하는지
추측하지 않습니다. 데이터에서 유도하면("모든 값이 한 주체에만") 자기부정에 빠집니다 —
진짜 중복 레코드가 하나만 있어도 그 관계가 비단사가 되어 범주형으로 뒤집히고, 그러면 중복
레코드가 게이트를 실패시킵니다. 이 분류가 면제해 주려던 바로 그 경우입니다. 사실이 두
개뿐인 KB가 우연히 단사가 되는 문제도 있습니다. 값이 주체 하나를 지목하는 관계만
선언하십시오. **여러 주체가 공유하는 범주는 절대 선언하지 마십시오** — 이 감사기가 잡으려는
누수를 영구히 면제하게 됩니다.

`factlog init` 이 주석 처리된 예시와 함께 이 파일을 만들어 둡니다 — 전부 주석이므로
선언은 0개이고, 파일이 없는 것과 동작이 같습니다. **기존 KB에는 이 파일이 없으므로** 모든
관계가 범주형으로 시작하고 제목 충돌이 누수로 보고됩니다. `policy/identity-relations.md`
를 직접 만들어 identity 관계를 선언하십시오(서지라면 제목과 DOI).

## 고치는 방법

자동으로 병합하지 않습니다. 모든 발견은 **사람의 판단**을 위한 보고입니다.
`factlog amend <subject> <relation> <object> --set-object <정본>` 으로 고치면
`candidates.csv` 와 뒷단 `runs/*.json` 이 함께 갱신됩니다.

split wrapper 와 wrapper value 에는 리포트가 `fix:` 줄로 `amend` 명령의 뼈대를 함께
출력합니다. 다만 그 줄의 `<subject>` 는 리터럴이므로, 실제 주체 이름으로 바꿔서
실행해야 합니다. spelling duplicate 와 placeholder 에는 `fix:` 줄이 없습니다 — 어느
표기가 정본인지, 그 값이 무엇으로 대체돼야 하는지는 도구가 판단하지 않습니다.

## 잡지 못하는 것

랩퍼 규칙은 의도적으로 좁으므로, 깨끗한 리포트가 완전성의 증명은 아닙니다.

- 미탐 형태: `others: X`, 괄호 없는 `기타 X`, `기타(X) 등`
- 숫자는 접지 않습니다 — `1.5` 는 `15` 가 아닙니다
- `etc` 는 랩퍼 단어로 보지 않습니다 — `ETC (electron transport chain)` 은 실재하는 값입니다
- 관계를 가로지르는 비교는 하지 않습니다 — 설계상 그렇습니다

`tools/entity_audit.py` 는 이웃한 점검입니다. KB 전체에서 *엔티티* 분열을 토큰 공유
휴리스틱으로 찾으므로 범위가 넓고 훨씬 시끄럽습니다(같은 KB에서 후보 2275건). 바로
조치할 수 있는 정밀한 관계별 발견이 필요하면 `value_audit` 을 쓰십시오.

## 관련 문서

- [값 계층](value-hierarchy.md) — 두 값이 상위-하위유형이면 분열이 아닙니다
- [단일값 관계](single-valued.md) — 한 subject 당 object 하나, `CONFLICT`
- [사실 검토](review.md) — `factlog amend` 를 포함한 사람 게이트
