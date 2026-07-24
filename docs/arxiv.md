# arXiv 가져오기 (`factlog arxiv-*`)

> 🌐 [English](arxiv.en.md) | **한국어**

[arXiv](https://arxiv.org)는 프리프린트 저장소입니다. factlog는 단건 임포트·검색·버전
추적·철회 종결·원장 백필을 다섯 개의 명령으로 제공합니다.

가져온 항목은 `sources/<slug>.md` 원본 하나가 되고, 여전히 **후보**입니다 —
`sync → review → accept` 게이트를 거쳐야 사실이 됩니다(P1/P2). arXiv는 factlog의 사실
저장소가 아니라 입력원입니다.

## 사전 준비

```bash
pip install 'factlog-academic[arxiv] @ git+https://github.com/SeoyunL/factlog-academic'
```

`httpx` 와 `feedparser` 둘이 추가됩니다(Atom 응답을 파싱합니다). arXiv API는 **인증이
없어** API 키도 계정도 등록도 필요 없습니다.

## 명령

| 명령 | 하는 일 | 네트워크 |
| --- | --- | --- |
| `arxiv-import --id <id>` | id로 단건/다건 임포트 | 예 |
| `arxiv-search --query ...` | 검색 후 선택 임포트 | 예(`--show-query` 는 아님) |
| `arxiv-check-versions` | 원장의 arXiv 레코드가 최신 버전인지 확인 | 예 |
| `arxiv-acknowledge-withdrawal --id <id>` | 철회 신호를 사람이 종결 | 예 |
| `arxiv-backfill-provenance` | front matter만 있는 논문에 원장을 만들어 줌 | 아니요 |

다섯 명령 모두 `--target <KB>`(없으면 활성 KB)를 받습니다. `--porcelain`(스크립트용 탭
구분 출력)은 `arxiv-acknowledge-withdrawal` 을 뺀 넷이 받고, `--dry-run`(파일을 만들지
않음)은 `arxiv-import` / `arxiv-search` / `arxiv-backfill-provenance` 셋이 받습니다.

**크레딧 예산 같은 것은 없습니다.** OpenAlex와 달리 arXiv는 요청 비용을 세지 않으므로
factlog도 arXiv 쪽에서는 예산을 추적하지 않습니다. 대신 arXiv가 권고하는 **요청 간 3초
지연**(`request_delay`, 설정으로 바꿀 수 있음)을 factlog가 클라이언트에서 스스로 지킵니다.
arXiv가 강제하지 않는 예의이므로 아무도 대신 지켜 주지 않습니다.

**arXiv가 밀어낼 때(HTTP 503/429)는 `Retry-After` 를 지킵니다.** 서버가 지정한 대기를
그대로 기다렸다가 재시도하고(한 번의 시도 + 최대 두 번의 재시도), 헤더가 없거나 읽을 수
없으면 2초·4초 지수 백오프로 물러납니다. 서버가 2초보다 짧은 대기를 요청해도 **최소 2초는
기다립니다** — `Retry-After` 는 최솟값이라 더 오래 기다리는 것도 준수이고, `request_delay`
를 0으로 낮춰 둔 설정에서도 재시도 간격의 바닥이 남아 있어야 하기 때문입니다. 다만
**서버가 요청한 대기가 60초를 넘으면
재시도하지 않고 즉시 멈춥니다.** 대기를 60초로 잘라서 다시 두드리면 서버가 지정한 창 안에서
요청하는 셈이고, 그러면서 화면에는 서버가 말한 숫자를 안내하게 되어 안내와 동작이
어긋납니다. 이때 오류 메시지는 arXiv가 요청한 대기와 실제로 시도한 횟수를 함께 알려 주므로,
그만큼 기다렸다 같은 명령을 다시 실행하면 됩니다.

### `arxiv-import`

```bash
factlog arxiv-import --id 2311.09277
factlog arxiv-import --id 2311.09277v1                  # 특정 버전을 id에 인라인으로 핀 (해당 버전이 실제로 있어야 함)
factlog arxiv-import --id 2311.09277 --id hep-th/9901001
factlog arxiv-import --id 2311.09277 --dry-run          # 계획만
```

- `--id` (필수) — **반복 가능**하고, 한 번의 실행에 **최대 100개**입니다(arXiv가 요청당
  받는 상한). 넘기면 실행 전체가 실패하고 아무것도 쓰지 않습니다.
- **버전은 별도 플래그가 아니라 id 에 인라인으로 핀합니다** (`2311.09277v1`).
  `--version` 같은 플래그는 없습니다.

문법이 틀린 id는 그 id만 오류가 되고 나머지 배치는 계속 진행됩니다. 전송 실패만이 실행
전체를 중단시킵니다 — 부분적으로 신뢰할 수 있는 배치란 없기 때문입니다.

`--dry-run` 도 네트워크는 씁니다. 제목·철회 신호·slug 를 알아야 결과를 예측할 수 있기
때문이며, 파일은 만들지 않습니다.

### `arxiv-search`

```bash
factlog arxiv-search --query "chain of thought"                       # all:"chain of thought" 로 전송됨
factlog arxiv-search --query "chain of thought" --show-query          # 요청 없이 쿼리만 출력
factlog arxiv-search --query "neurosymbolic AI" --category cs.AI --year 2020-2025 --limit 50
factlog arxiv-search --query "diffusion model" --dry-run --all        # 검색은 하되 쓰지 않음
factlog arxiv-search --query "graph neural network" --all             # 프롬프트 없이 전부
```

- `--query` (필수) — 검색 문자열. 아래 [검색 쿼리의 조용한 함정](#검색-쿼리의-조용한-함정) 을
  먼저 읽으세요.
- `--category` — arXiv 카테고리로 제한. **반복 가능**하며 AND로 묶입니다 (`cs.CL`).
- `--year` — 제출 연도 또는 범위 (`2023`, `2020-2025`)
- `--limit` — 결과 수 (기본 25, 최대 200)
- `--sort submitted|updated|relevance` — 정렬 순서(`submitted` 는 최신순). 기본값은
  주지 않으며, 주지 않으면 arXiv의 기본 정렬을 따릅니다.
- `--all` — 결과 전체를 프롬프트 없이 임포트. stdin이 터미널이 아닐 때 필요합니다.

터미널에서 `--all` 없이 실행하면 결과 목록을 보여 주고 어떤 항목을 가져올지 물어봅니다.
터미널이 아니거나 `--porcelain` / `--dry-run` 이면 묻지 않고 아무것도 선택하지 않습니다 —
물을 수 없는 명령이 대신 짐작하지는 않습니다. 그래서 `--dry-run` 으로 계획을 보려면
`--dry-run --all` 이 필요합니다(`openalex-search` 와 같습니다). 철회된 결과는 선택하기
**전에** 목록에서 표시됩니다.

#### `--show-query` 와 `--dry-run` 은 다릅니다

- `--show-query` 는 **요청을 보내지 않고** 실제로 전송될 `search_query` 를 출력하고
  끝납니다. 함께 `max_results` 를, `--sort` 를 준 경우 `sortBy` 도 출력합니다. 쿼리
  문자열은 클라이언트가 쓰는 것과 같은 composer가 만들기 때문에 실제 전송값에서 표류할 수
  없습니다.
- `--dry-run` 은 **검색은 하되** 파일을 쓰지 않습니다 — factlog의 다른 모든 곳과,
  `openalex-search` 와 같은 뜻입니다.

### 검색 쿼리의 조용한 함정

**여러 단어를 그냥 넘기면 arXiv는 그것을 구(phrase)로 읽지 않습니다.** 셸이 따옴표를
먹어 버리므로 `--query "chain of thought"` 는 arXiv에 세 단어로 도착하고, arXiv는 이를
느슨하게 매칭합니다. factlog는 그런 쿼리를 구로 감싸 `all:"chain of thought"` 로
보냅니다. 실측값(#89):

| 전송된 `search_query` | totalResults |
| --- | --- |
| `chain of thought` | 87,029 |
| `all:"chain of thought"` | 5,669 |
| `chain` | 71,394 |

감싸지 않은 형태는 `chain` 한 단어의 결과와 대체로 같습니다. 아무 오류도 나지 않고,
운영자는 "chain-of-thought 논문이 87,029편"이라 읽은 뒤 자기 구가 구로 검색되지 않았다는
사실을 끝내 알지 못합니다. `openalex-search` 에는 이 함정이 없습니다 — OpenAlex의
`search=` 는 자유 텍스트를 받아 구 처리를 직접 합니다.

**감쌌다는 사실은 알려 줍니다.** 감싸기가 일어나면 factlog가 무엇을 감쌌는지 stderr에
출력합니다 — 운영자가 타이핑한 것을 조용히 고쳐 쓰는 일은 조용히 잘못 검색하는 것과 같은
배신입니다. `--show-query` 로 전송될 쿼리를 미리 볼 수도 있습니다.

**감싸기를 끄는 법.** **여러 단어**로 이루어졌고 그 자체로 아무것도 표현하지 않는 쿼리만
감쌉니다. 다음 넷 중 하나면 쿼리는 그대로 전송됩니다.

- **한 단어** — 애초에 감싸지 않습니다. `--query transformer` 는 `transformer` 로 전송되지
  `all:"transformer"` 가 되지 않습니다. 구로 감쌀 여러 단어가 없기 때문입니다.
- **필드 프리픽스** (`ti:`, `au:`, `abs:`, `co:`, `jr:`, `cat:`, `rn:`, `id:`, `all:`) —
  이미 arXiv의 언어로 말하고 있으므로 그대로 둡니다. 느슨한 매칭이 필요하면 이걸 씁니다.
- **불리언 연산자** (`AND` / `OR` / `ANDNOT`) — 구조를 표현하고 있으므로 그대로 둡니다.
  감쌌다면 뜻이 조용히 바뀝니다: 실측으로 `chain AND thought` 는 6,015편,
  `all:"chain AND thought"` 는 5,669편입니다.
- **이미 들어 있는 큰따옴표** — 의도적으로 인용한 것이므로 그대로 둡니다. 이것은 감싸기를
  끄는 것이지 **느슨하게 매칭하는 것이 아닙니다**: 여전히 구로 검색됩니다. 느슨한 매칭은
  필드 프리픽스나 불리언으로만 얻습니다.

**거부되는 두 경우.** 둘 다 요청을 보내기 전에 종료 코드 1로 멈춥니다.

- **따옴표 개수가 홀수** — arXiv는 짝 없는 따옴표를 무시하고 느슨하게 매칭하므로, 이 함수가
  막으려는 바로 그 과다 매칭이 예고 없이 통과해 버립니다.
- **여러 단어 쿼리 안의 백슬래시** — 뒤에 붙일 닫는 따옴표를 이스케이프해 버립니다. arXiv의
  이스케이프 규칙은 문서화돼 있지 않으므로, 거부하는 편이 정직합니다. 직접
  `all:"..."` 를 써서 넘기세요.

#### 존재하지 않는 값에 arXiv는 `200 OK` + "0건" 으로 답합니다

`cat:cs.NOTAREALCAT` 도, 아예 없는 필드인 `bogusfield:x` 도 오류가 아니라 "결과 0건"으로
돌아옵니다. 운영자는 이걸 "그런 논문은 없다"로 읽습니다. 그래서 factlog는 요청을 **보내기
전에** 값을 검증합니다. 거부 메시지를 보게 되는 이유가 이것입니다.

- `--category` 와 쿼리 안의 `cat:` 값은 arXiv 공식 분류표에 대조합니다.
- 쿼리의 필드 프리픽스는 위 아홉 개 중 하나여야 합니다.
- `--year` 는 뒤집힌 범위(`2025-2020`)도, arXiv 수명(1991 ~ 내년) 밖의 연도도 거부합니다.
  둘 다 arXiv는 200 + 0건으로 답하기 때문입니다. 문법 자체가 깨진 경우에만 arXiv가 500을
  냅니다 — 너무 늦고 너무 거칩니다.
- `--sort` 의 잘못된 값은 arXiv도 400으로 거부하는 몇 안 되는 값입니다.

### `arxiv-check-versions`

KB의 provenance 원장(`<kb>/source-provenance/*.json`)에 있는 arXiv 레코드를 arXiv의 현재
버전과 비교해 **보고**합니다.

```bash
factlog arxiv-check-versions                    # 보고만
factlog arxiv-check-versions --older-than 0     # 모든 레코드 강제 재확인
factlog arxiv-check-versions --auto-update      # 버전 추적 필드만 원장에 기록
```

- `--older-than DAYS` — 최근 DAYS일 안에 확인한 레코드는 건너뜁니다(기본 30). 확인 시각은
  소스 파일이 아니라 check-log에서 읽습니다. `0` 은 전부 재확인합니다.
- `--auto-update` — 아래 [결정론 경계](#결정론-경계) 참조.

`--auto-update` 없이 실행하면 check-log 타임스탬프 말고는 아무것도 쓰지 않습니다.

#### `no-version` 은 `unchanged` 가 아닙니다

버전을 비교할 수 없는 논문은 **`no-version`** 이라는 자기 자신의 상태로 보고됩니다 —
`unchanged` 도 `changed` 도 아닙니다 (#121).

`unchanged` 가 아닌 이유: 비교된 것이 없습니다. 이 논문은 이 명령이 존재하는 이유인 그
신호에서 조용히 빠져 있었고, `Version changed: 0` 을 읽은 운영자는 그 논문에 수리가
필요하다는 것조차 알 길이 없었습니다. `changed` 도 아닌 이유: "버전이 None 에서 7로
바뀌었다"에서 `None` 은 파이썬 값이지 원장이 기록한 적 있는 버전이 아닙니다.

**고치는 방법은 원인마다 다릅니다.** 네 가지 논문이 여기에 도달하고, 답도 넷입니다. 하나의
remedy를 넷 모두에 붙이는 것이 곧 아무 일도 하지 않는 명령을 처방하는 길입니다(#116).
리포트는 각 논문에 해당하는 답을 직접 출력합니다.

| 논문의 상태 | 고치는 명령 |
| --- | --- |
| 원장에 arXiv 레코드가 있는데 `version` 이 없다 | `arxiv-check-versions --auto-update` 가 채웁니다 |
| 원장은 있으나 arXiv 레코드가 없다 (`arxiv_id` 만 메아리친 OpenAlex 임포트) | `factlog arxiv-import --id <id>` — `--auto-update` 는 채울 레코드가 없습니다 |
| 원장이 없고, front matter에 `arxiv_version` 이 있다 | `factlog arxiv-backfill-provenance` 가 front matter로 원장을 만듭니다 |
| 원장이 없고, front matter에 `arxiv_version` 도 없다 | **어떤 명령도 이 논문을 고치지 못합니다.** 사람이 그 논문의 `sources/*.md` front matter에 `arxiv_version: <N>` 을 손으로 추가해야 비로소 `arxiv-backfill-provenance` 가 원장을 만들 수 있습니다 |

마지막 줄이 중요합니다. `arxiv-import` 는 `already imported (arxiv_id match)` 로 건너뛰고,
`arxiv-backfill-provenance` 는 버전 없는 논문을 `refused` 합니다. **어떤 명령도** 이 논문을
고치지 못하며, 여기서 아무 명령이나 이름 붙이는 것은 같은 거짓말을 장소만 옮기는 일입니다.
막힌 것을 푸는 것은 명령이 아니라 사람입니다. 사람이 그 논문의 `sources/*.md` front matter에
`arxiv_version: <N>` 을 손으로 추가하면(`<N>` 은 그 논문의 실제 arXiv 버전으로, arXiv 페이지
`https://arxiv.org/abs/<id>` 에서 사람이 직접 읽습니다 — factlog가 대신 조회하지 않습니다),
그때 `arxiv-backfill-provenance` 가 front matter로 원장을 만들 수 있습니다. arXiv에 질의해
버전을 채우면 백필이 네트워크 refresh가 되어 기본 경로의 "네트워크 없음" 보장을 깨므로,
이 값은 명령이 아니라 사람이 넣습니다.

원장이 있으나 **파싱되지 않는** 경우도 있습니다. 읽지 못한 파일의 내용에 대해서는 아무것도
주장할 수 없으므로, 리포트는 그 논문을 `Could not check:` 아래로 보내고 remedy를 제시하지
않습니다. 사람이 손으로 원장을 고치기 전까지는 어떤 명령도 이 논문의 버전을 기록하지
못합니다.

#### `version-conflict`: 한 논문의 소스들이 서로 다른 버전을 주장할 때

한 논문의 소스 두 개가 서로 **다른** `arxiv_version` 을 기록하면 — `a.json` 이 v3, `b.json`
이 v7 — 그것은 **충돌**이지 `max` 의 입력이 아닙니다 (#137). 이 저장소는 다른 모든 곳에서
충돌을 해결하지 않고 **보고하거나 거부**합니다 (`add_source` 는 `ProvenanceConflict` 를
던지고, 백필은 읽을 수 없는 식별 필드를 쓰느니 거부합니다). 예전에는 이 불일치를 `max` 로
접었고, arXiv가 높은 값을 서빙하면 세 버전 뒤처진 논문을 `unchanged` 라고 보고했습니다 —
이 명령이 내야 할 신호의 정반대입니다.

그래서 이런 논문은 **`version-conflict`** 라는 자기 자신의 상태로 보고됩니다. `unchanged`
도 `changed` 도 `no-version` 도 아닙니다. 리포트는 각 소스와 그 소스가 가진 값을 이름
붙여 출력하고, 어떤 명령도 이를 조용히 해결하지 않습니다 — 두 값 중 하나를 고르는 것이
곧 `max` 가 했던 추측이기 때문입니다. `--auto-update` 도 충돌은 쓰지 않습니다(버전 없는
레코드는 채우지만, 충돌은 사람이 소스를 직접 조율할 때까지 실행마다 계속 올라옵니다).
자기 모순인 KB에서 명령은 0이 아닌 코드로 끝납니다.

충돌 판정은 **두 fold 와 그 혼합** 모두에서 동시에 이뤄집니다 — sidecar 원장들끼리,
front matter들끼리, 그리고 **한쪽은 원장·한쪽은 front matter**(`a.json: v3` 옆에 원장 없는
`b.md: v7`, 이슈가 말한 "one of each")인 경우까지. 원장 없는 `.md` 는 버전을 front matter에만
기록하므로, 다른 소스의 원장이 이미 그 id를 덮고 있어도 그 버전이 fold에 합류합니다. 그래서
`arxiv-backfill-provenance` 가 `.md` 를 하나씩 sidecar로 바꾸는 중간 상태(front matter 둘 →
하나만 backfill → 둘 다 sidecar)가 **모두 conflict로 같게 읽힙니다** — 원장이 하나 생겼다고
논문의 의미가 `changed`/`unchanged` 에서 conflict로 뒤집히지 않습니다 (#117). porcelain에서는
기존 `status` 열의 새 값(`version-conflict`)으로 나타나고 `reason` 열이 각 소스와 값을
담으며, 충돌이 없는 KB의 출력은 #137 이전과 바이트 단위로 동일합니다.

### `arxiv-acknowledge-withdrawal`

`arxiv-check-versions` 는 철회 신호를 사람이 종결할 때까지 실행마다 계속 다시 올립니다.
이 명령이 그 종결 verb입니다.

```bash
factlog arxiv-acknowledge-withdrawal --id 2311.09277
factlog arxiv-acknowledge-withdrawal --id 2311.09277 --yes   # 기록만. 해제는 못 합니다
```

- `--id` (필수) — 단 하나의 arXiv id. **버전 핀은 거부합니다** (`2311.09277v2`) —
  정체(identity)는 베이스 id입니다. `--all` 도 와일드카드도 없습니다. 영향 범위는 사람이
  고른 id 하나입니다.
- `--yes` — 확인 프롬프트를 건너뜁니다. 터미널이 아닌 환경에서 **기록**하려면 필수입니다.
  **철회를 기록할 수는 있어도 해제할 수는 없습니다** (#106) — 해제는 `--yes` 를 붙여도
  거부되므로 터미널이 필요합니다.

기록할 값은 상류에만 있으므로 arXiv를 **실시간으로** 조회합니다. 다만 조회는 원장을 확인한
**뒤에** 일어납니다: KB에 없는 id 와 front matter만 있는 논문은 요청을 보내기 전에 거부되고
(`No request was made`), 원장과 arXiv의 값이 이미 같으면(둘 다 철회 없음이거나 같은 agent)
프롬프트도 쓰기도 없이 0으로 끝납니다.

**`--yes` 는 철회를 *기록*할 수는 있어도 *해제*할 수는 없습니다** (#106). 아래
[결정론 경계](#결정론-경계) 3번을 보세요.

**원장이 없는 논문(front matter만 있는, #82 이전 임포트)은 종결할 수 없습니다** — 결정을
적을 곳이 없습니다. 여기서 갈립니다.

- front matter에 `arxiv_version` 이 **있으면**: `factlog arxiv-backfill-provenance` 로 원장을
  만든 뒤 다시 종결하세요.
- front matter에 `arxiv_version` 이 **없으면**: 백필이 그 논문을 `refused` 하므로 원장이
  생기지 않고, **어떤 명령으로도 이 논문의 철회를 종결할 수 없습니다.** 사람이 그 논문의
  `sources/*.md` front matter에 `arxiv_version: <N>` 을 손으로 추가해야 합니다(`<N>` 은
  arXiv 페이지 `https://arxiv.org/abs/<id>` 에서 사람이 직접 읽는 실제 버전 번호이며,
  factlog가 대신 조회하지 않습니다). 그러면 `arxiv-backfill-provenance` 로 원장을 만든 뒤
  종결할 수 있습니다. 백필의 `required=("version",)` 이 그렇게 정해져 있으며, 그 이유는
  아래 `arxiv-backfill-provenance` 절에 있습니다.

### `arxiv-backfill-provenance`

`sources/*.md` 의 front matter에만 존재하는 arXiv 논문(#82 이전 임포트)에, 그 front
matter가 함의하는 provenance 원장을 만들어 줍니다. **네트워크를 쓰지 않고**,
`sources/*.md` 는 건드리지 않습니다.

```bash
factlog arxiv-backfill-provenance --dry-run   # 원장을 받을 id와 거부될 id를 나열
factlog arxiv-backfill-provenance
```

- `--dry-run` — 무엇이 쓰일지 미리 봅니다. 다만 **미리보기는 실패할 쓰기를 보고할 수
  없습니다** — 쓸 수 없는 `source-provenance/` 는 실제 실행에서만 드러납니다.

`arxiv_version` 을 읽을 수 없는 논문은 **거부**됩니다. `version` 은 식별(identifying)
필드이고, 백필은 읽을 수 없는 식별 필드를 쓰지 않습니다. 없는 버전을 없는 채로 적으면
나중에 진짜 값을 가진 임포트가 `None != 7` 을 보고 divergence로 판정해 오류를 내기
때문입니다 — 백필 스스로가 만들어 낸 *가짜* 충돌입니다. `imported_at` 이 없는 논문도
거부됩니다.

반대로 `withdrawn_by` 는 필수가 아닙니다. 철회되지 않은 논문 — 즉 압도적 다수 — 에서
`None` 이 정당한 값이기 때문입니다.

## 결정론 경계

이 경계를 모르면 `--auto-update` 가 무엇을 고쳤는지 오해하게 됩니다.

1. **임포트는 원장을 고쳐 쓸 권한이 없습니다** (#58, #63). 임포트는 `add_source` 를
   호출하고, `add_source` 는 어긋나는 기존 항목을 수정하기를 거부합니다. 새 값을 배우려고
   상류에 다녀온 것은 임포트가 아니라 갱신(refresh)입니다.
2. **`arxiv-check-versions --auto-update` 는 `version` / `last_updated` / `comment` 세
   필드만** 원장에 씁니다(`AUTO_UPDATE_FIELDS`). 다른 원장 필드도, 같은 원장에 있는 비-arXiv
   레코드도, `imported_at` 도 움직이지 않습니다. `sources/*.md` 는 **절대 열지 않으므로**
   바이트도 `mtime_ns` 도 동일하게 유지됩니다(P4). 세 필드가 이미 일치하면 파일 자체를 다시
   쓰지 않는 바이트 단위 no-op 입니다.
3. **철회는 어느 모드에서도 자동으로 흡수되지 않습니다.** `withdrawn_by` 는
   `AUTO_UPDATE_FIELDS` 에 없으므로 `--auto-update` 가 이를 기록하는 일은 없습니다. 기록해
   버리면 다음 실행에서 "새로 철회됨"이 거짓이 되어 사람에게 올라가는 신호가 사라집니다.
   철회는 사람에게 보고되고 `arxiv-acknowledge-withdrawal` 로만 종결됩니다.
   `--auto-update` 가 철회된 논문의 *버전* 을 갱신하는 일은 있습니다(철회는 흔히 새 버전으로
   배포됩니다). 철회 자체는 P1 사람 게이트에 남습니다.

   그리고 **`--yes` 는 철회를 기록할 수는 있어도 해제할 수는 없습니다** (#106). arXiv가
   철회를 보고하지 않는다는 사실은 "철회가 취소됐다"일 수도 있고 "철회 문장을 읽지
   못했다"(잘린 초록, 정규식 밖의 표현)일 수도 있으며, 코드는 이 둘을 구별하지 못합니다.
   기록은 소리를 내는 방향이고 해제는 침묵시키는 방향입니다. 해제는 사람이 프롬프트에서
   노트를 직접 보고 확인해야 합니다 — `--yes` 로는 거부되고 아무것도 쓰지 않습니다.
4. **버전 없는 원장 레코드는 `no-version` 이라는 자기 자신의 상태**로 보고됩니다 (#121).
   `unchanged` 로 뭉뚱그리지 않습니다. 보고된 집합과 쓰이는 집합은 같은 집합입니다 —
   명령은 리포트가 이름 붙이지 않은 필드를 쓰지 않습니다.
5. **소스들이 서로 다른 버전을 주장하면 `version-conflict`** 라는 자기 자신의 상태로
   보고됩니다 (#137). `--auto-update` 는 이를 해결하지 않습니다 — 두 기록된 값 중 하나를
   고르는 것은 갱신의 권한이 아니라 추측입니다. 버전 없는 레코드는 갱신이 정당하게 배운
   값을 채우지만, 충돌은 사람이 소스를 조율할 때까지 남습니다.
6. **임포트된 항목은 여전히 후보**입니다. `sync → review → accept` 게이트를 거쳐야 사실이
   됩니다 (P1/P2).

### 철회(withdrawal)는 철회(retraction)가 아닙니다

arXiv의 **withdrawn** 은 프리프린트에 대한 **저자 또는 arXiv 관리자**의 행위입니다.
저널이 게재 논문을 취소하는 **retraction**(OpenAlex의 `is_retracted`)과는 별개의 절차이며,
공유되는 후속 처리도 없습니다. factlog는 둘을 서로 다른 필드, 서로 다른 종결 명령으로
다룹니다.

기록되는 agent는 `author` 아니면 `admin` 둘 중 하나입니다. 관리자는 저자권 분쟁이나
부적절한 내용 때문에도 논문을 내리므로, 맨 불리언 하나였다면 본문이 일어난 적 없는 저자의
행위를 주장하게 됩니다. 그래서 front matter 키는 `arxiv_withdrawn` 과
`arxiv_withdrawn_by` 이고, 맨 `withdrawn:` 은 쓰지 않습니다.

## 생성되는 source 파일

`sources/<slug>.md` 하나. YAML front matter + 본문입니다. `arxiv_id` / `arxiv_version` /
`title` 은 언제나 나타나고, 나머지 키는 값이 있을 때만 나타납니다.

```yaml
---
arxiv_id: 2311.09277          # 베이스 id. 버전은 별도 키
arxiv_version: 2
title: "..."
authors: [...]
year: 2023
primary_category: cs.CL
tags: [...]                   # arXiv 카테고리, primary 가 맨 앞
doi: "..."                    # 맨 키. arxiv_doi 가 아닙니다
journal: "..."
preprint: true                # 항상 참
imported_from: arxiv
imported_at: "..."
arxiv_withdrawn: true         # 철회된 경우에만
arxiv_withdrawn_by: author    # author | admin
---
```

정체(identity)의 키는 `arxiv_id` 이지 버전이 붙은 형태가 아닙니다 — 버전을 키로 삼으면 같은
논문의 새 버전이 두 번째 파일로 들어옵니다. `doi` 가 맨 키인 것은 교차 소스 색인이 그
리터럴 키를 그대로 찾기 때문이며, OpenAlex 임포터도 같은 키로 씁니다.

`preprint: true` 는 **항상** 붙고 뒤집히지 않습니다. 이 레코드는 arXiv 예치본이며, 다른
무엇이 존재하든 프리프린트입니다. 게재 여부는 `journal` 과 `doi` 가 사실로 말합니다 —
동기화를 유지해야 하는 불리언이 아닙니다.

## 설정 파일 (선택)

해석 순서: **명시적으로 지정한 경로** > `<KB>/policy/arxiv-config.toml` >
`${XDG_CONFIG_HOME:-~/.config}/factlog/arxiv.toml` > 내장 기본값. (명시 경로는 라이브러리
인자입니다 — arXiv 명령들은 이를 받는 플래그를 노출하지 않습니다. 지정한 파일이 없으면
오류입니다. 가리킨 쪽이 사람이니까요.)

```toml
[client]
email = "you@example.org"   # 신원 표기(선택). 인증이 아닙니다.
request_delay = 3.0         # 요청 간 초. 기본 3.0

[import]
default_limit = 25          # 1..200, 기본 25
max_limit = 200             # factlog 정책 상한 200
skip_duplicates = true      # 같은 arxiv_id 는 재임포트 시 건너뜀(멱등)
include_abstract = true     # 초록을 본문에 포함
```

> **secrets 경계가 없습니다.** arXiv API에는 인증이 없고 `email` 은 모든 요청의
> **User-Agent** 에 실려 가는 신원 표기용 예의일 뿐입니다(과다 사용자에게 연락하려는
> 것입니다). 그래서 커밋되는 KB 정책 파일에 둬도 안전합니다 — KB 정책 파일에서 읽기를
> 거부하는 Zotero의 `web_api_key` 와 대비됩니다. OpenAlex와 달리 arXiv는 이를 쿼리
> 문자열이 아니라 User-Agent 헤더에 싣습니다.

`request_delay` 를 권고치보다 낮춰도 arXiv는 밀어내지 않습니다. 낮춘 값은 그대로 적용되며,
위험은 낮춘 사람이 집니다.

값의 타입이 틀리면 기본값으로 되돌아갑니다. 다만 `client.email` 이 문자열이 아니면
실패합니다 — 모든 요청의 User-Agent에 그대로 실리는 값이라 오타가 조용히 익명 요청으로
떨어지면 안 되기 때문입니다.

## 멱등성과 원본 불변

- 같은 논문을 다시 임포트해도 이미 있는 `arxiv_id` 는 건너뛰므로 결과가 같습니다(P3).
- factlog는 기존 `sources/` 원본을 절대 수정하지 않습니다(P4). `--auto-update` 도,
  `arxiv-acknowledge-withdrawal` 도, `arxiv-backfill-provenance` 도 마찬가지입니다 —
  원장만 씁니다.
- 임포트된 항목은 후보일 뿐이며, 사람의 `accept` 게이트를 통과해야 사실이 됩니다(P1/P2).
