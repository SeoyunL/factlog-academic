# OpenAlex 가져오기 (`factlog openalex-*`)

[OpenAlex](https://openalex.org)는 학술 저작(work)을 담은 공개 서지 데이터베이스입니다.
factlog는 검색·단건 임포트·인용 그래프 탐색·메타데이터 갱신·원장 백필을 여섯 개의
명령으로 제공합니다.

가져온 항목은 `sources/<slug>.md` 원본 하나가 되고, 여전히 **후보**입니다 —
`sync → review → accept` 게이트를 거쳐야 사실이 됩니다(P1/P2). OpenAlex는 factlog의
사실 저장소가 아니라 입력원입니다.

## 사전 준비

```bash
pip install 'factlog[openalex]'
```

httpx 하나만 추가됩니다. OpenAlex API는 **인증이 없어** API 키나 계정이 필요 없습니다.

## 명령

| 명령 | 하는 일 | 크레딧 |
| --- | --- | --- |
| `openalex-search --query ...` | 자유 텍스트 검색 후 선택 임포트 | 검색 1회당 **10** |
| `openalex-import --work-id \| --doi` | 단건 임포트 | 0 |
| `openalex-cite --for <slug>` | 인용 그래프 한 단계 탐색 | 방향당 1 |
| `openalex-refresh` | 원장의 OpenAlex 레코드 재조회·비교 | 레코드당 0 |
| `openalex-acknowledge-retraction --id <id>` | 철회 신호를 사람이 종결 | 0 |
| `openalex-backfill-provenance` | front matter만 있는 저작에 원장을 만들어 줌 | 0 |

모든 명령은 `--target <KB>`(없으면 활성 KB)를 받고, `acknowledge-retraction` 을 뺀
나머지는 `--porcelain`(스크립트용 탭 구분 출력)을 받습니다. `search` / `import` /
`cite` / `backfill-provenance` 는 `--dry-run`(파일을 만들지 않음)을 받습니다.

> `--dry-run` 만으로 계획이 출력되는 명령은 `openalex-import` 와
> `openalex-backfill-provenance` 입니다. `search` 는
> `--dry-run` 이 대화형 선택을 끄기 때문에 아무것도 선택되지 않으므로 `--dry-run --all`
> 이 필요하고, `cite` 는 `--auto-import` 없이는 `--dry-run` 을 보기도 전에 반환하므로
> `--dry-run --auto-import` 가 필요합니다.

### 크레딧 예산

OpenAlex는 요청 수가 아니라 **크레딧**으로 제한합니다 — 하루 약 1000 크레딧입니다.
검색은 결과를 몇 건 받든 **1회당 10 크레딧**이므로, `--limit` 을 아끼는 것은 비용을
아끼지 않습니다. 필요한 만큼 한 번에 넉넉히 요청하세요. 단건 조회(`GET /works/{id}`)는
0 크레딧이라 `import` 와 `refresh` 는 사실상 무료입니다.

남은 예산이 적어지면 `search` / `import` / `cite` 가 경고를 출력합니다(`refresh` 와
`acknowledge-retraction` 은 출력하지 않습니다). 예산이 소진되면 다섯 명령 모두
실패하고 아무것도 쓰지 않습니다.

### `openalex-search`

```bash
factlog openalex-search --query "neurosymbolic AI"
factlog openalex-search --query "neurosymbolic AI" --year 2020-2025 --limit 50
factlog openalex-search --query "dementia prevention" --type article --dry-run --all   # 계획만
factlog openalex-search --query "graph neural network" --all   # 프롬프트 없이 전부
```

- `--query` (필수) — 검색 문자열
- `--year` — 연도 또는 범위 (`2023`, `2020-2025`)
- `--type` — 저작 유형 (`article`, `book`, `dataset` …)
- `--limit` — 결과 수 (기본 25, 최대 200). 비용은 동일합니다.
- `--all` — 결과 전체를 프롬프트 없이 임포트. stdin이 터미널이 아닐 때 필요합니다.

터미널에서 `--all` 없이 실행하면 결과 목록을 보여 주고 어떤 항목을 가져올지 물어봅니다.
터미널이 아니거나 `--porcelain` / `--dry-run` 이면 묻지 않고 아무것도 선택하지 않습니다 —
물을 수 없는 명령이 대신 짐작하지는 않습니다. 철회 신호가 있는 결과는 목록에 표시됩니다.

### `openalex-import`

```bash
factlog openalex-import --work-id W2741809807
factlog openalex-import --doi 10.1007/s10462-023-10448-w
```

`--work-id` 와 `--doi` 는 배타적입니다. 둘 중 하나는 필수입니다.

### `openalex-cite`

이미 KB에 있는 소스에서 인용 그래프를 한 단계 넓힙니다.

```bash
factlog openalex-cite --for smith-2023-neurosymbolic                      # 이 논문을 인용한 저작
factlog openalex-cite --for smith-2023-neurosymbolic --direction cited    # 이 논문이 인용한 저작
factlog openalex-cite --for smith-2023-neurosymbolic --direction both --limit 50
factlog openalex-cite --for smith-2023-neurosymbolic --auto-import        # 나열된 저작을 모두 임포트
```

- `--for <slug>` (필수) — 출발점이 되는 factlog 소스 슬러그. `openalex_id` front matter가
  있어야 합니다.
- `--direction citing|cited|both` — 기본 `citing`
- `--limit` — 방향당 결과 수
- `--auto-import` — 나열된 저작을 확인 없이 임포트합니다. 인용 그래프는 빠르게
  넓어지므로 `--dry-run --auto-import` 로 규모를 먼저 확인하세요(`--auto-import` 가
  없으면 결과만 출력하고 반환하므로 `--dry-run` 은 아무 일도 하지 않습니다).

### `openalex-refresh`

KB의 provenance 원장(`<kb>/source-provenance/*.json`)에 있는 OpenAlex 레코드를 다시
조회해 현재 메타데이터와 비교하고, 달라진 점을 **보고**합니다.

```bash
factlog openalex-refresh                      # 보고만. 원장에 아무것도 쓰지 않음
factlog openalex-refresh --older-than 0       # 모든 레코드 강제 재확인
factlog openalex-refresh --auto-update        # 달라진 doi/work_type/journal 을 원장에 기록
```

- `--older-than DAYS` — 최근 DAYS일 안에 확인한 레코드는 건너뜁니다(기본 30). 확인
  시각은 소스 파일이 아니라 check-log에서 읽습니다. `0` 은 전부 재확인합니다.
- `--auto-update` — 아래 [결정론 경계](#결정론-경계) 참조.

비교 대상은 원장이 저장하는 필드뿐입니다: `doi`, `work_type`, `journal`,
`is_retracted`. `cited_by_count` 는 변동이 잦은 지표라 애초에 원장에 없고, 따라서
구조적으로 divergence가 될 수 없습니다.

OpenAlex는 저작을 병합하기도 합니다. `W_a` 를 요청했는데 `W_b` 로 답이 오면 그것은
필드 변경이 아니라 **정체(identity) 변경**이므로 `id superseded` 라는 별도 신호로
보고하고, `--auto-update` 도 원장의 키를 바꾸지 않습니다.

### `openalex-acknowledge-retraction`

`openalex-refresh` 는 철회 신호를 사람이 종결할 때까지 계속 다시 올립니다(레코드를
실제로 확인하는 실행마다 — `--older-than` 안에 이미 확인한 레코드는 건너뜁니다).
이 명령이 그 종결 verb입니다.

```bash
factlog openalex-acknowledge-retraction --id W2741809807
factlog openalex-acknowledge-retraction --id W2741809807 --yes   # 확인 프롬프트 생략
```

- `--id` (필수) — 단 하나의 OpenAlex work id (`W2741809807` 또는 `openalex.org/W...` URL).
  `--all` 도 와일드카드도 없습니다. 영향 범위는 사람이 고른 id 하나입니다.
- `--yes` — 확인 프롬프트를 건너뜁니다. 터미널이 아닌 환경에서는 필수이며, 없으면
  비대화형 실행은 거부하고 아무것도 쓰지 않습니다.

실행할 때마다 **실시간으로** `GET /works/{id}` 를 조회합니다(0 크레딧). 캐시로 기록한
철회는 거짓일 수 있기 때문입니다 — OpenAlex가 이미 철회를 되돌렸을 수도 있습니다.
연결 실패, 예산 소진, 레코드 없음, 병합으로 인한 id 변경이면 종료 코드는 0이 아니고
아무것도 쓰지 않습니다.

OpenAlex가 철회를 되돌린 경우 이 명령이 원장의 키를 **제거**해 신호를 멈춥니다.
원본 `.md` 는 열지 않습니다 — 종결 이후로는 원장이 유일한 감사 기록입니다.

### `openalex-backfill-provenance`

`sources/*.md` 의 front matter에만 존재하는 OpenAlex 저작(#84 이전 임포트)에, 그 front
matter가 함의하는 provenance 원장을 만들어 줍니다. **네트워크를 쓰지 않고**,
`sources/*.md` 는 건드리지 않습니다.

```bash
factlog openalex-backfill-provenance --dry-run   # 원장을 받을 id와 거부될 id를 나열
factlog openalex-backfill-provenance
```

이 명령이 필요한 이유는 종결에 있습니다. #84 이전에 임포트된 저작은 front matter만 있고
원장이 없어 재임포트해도 원장이 생기지 않습니다(front matter의 정체 일치에서 sidecar writer
전에 멈춥니다). 원장이 없으면 결정을 적을 곳이 없으므로 `openalex-acknowledge-retraction`
이 그 저작을 **거부**하고(front matter만 있어 결정을 적을 원장이 없다며) 바로 이 명령을
가리킵니다. 백필은 front
matter가 이미 주장하는 값으로 원장을 세워 그 종결을 가능하게 합니다 — 새 주장을 만드는 것이
아니라 믿음이 저장되는 위치만 바꾸므로, acknowledge와 달리 **확인 프롬프트도 `--yes` 도 TTY
게이트도 없습니다.** API 클라이언트를 만들지 않고 front matter를 **읽기만** 합니다(P4: 모든
`.md` 는 바이트도 `mtime_ns` 도 동일하게 유지).

- `--dry-run` — 무엇이 쓰일지 미리 봅니다. 다만 **미리보기는 실패할 쓰기를 보고할 수
  없습니다** — 쓸 수 없는 `source-provenance/` 는 실제 실행에서만 드러납니다.

**arXiv와 달리 잃는 값이 없습니다.** 원장이 담는 필드 `doi` / `work_type` / `journal` /
`is_retracted` 는 모두 writer가 이미 내보내는 front matter 키(`doi` / `type` / `journal` /
`openalex_is_retracted`)를 가지므로, 백필된 원장은 임포트가 썼을 레코드와 필드 하나까지
같습니다. `submitted` 처럼 복원 불가능한 필드가 있는 arXiv와의 이 비대칭은 두 writer의
성질이지 이 명령의 사정이 아닙니다.

**OpenAlex는 식별(identifying) 필드를 선언하지 않습니다**(#73). 그래서 arXiv의
`version` 처럼 "읽을 수 없으면 거부"할 식별 필드가 없고, 백필이 읽을 수 없는 식별 필드를
써서 가짜 충돌을 만드는 위험 자체가 생기지 않습니다. 대신 front matter가 진실한 원장을
줄 수 없는 저작은 두 경우에 거부됩니다.

- `imported_at` 이 없는 저작.
- `openalex_is_retracted` 가 원장의 값 공간 밖(YAML 불리언 `true`/`false` 가 아닌 `1`,
  `yes`, `on` 등)인 저작. 이 값은 공유 writer로 **그대로** 넘겨져 거부되며, 결코 추측으로
  보정되지 않습니다 — 값을 버리면 "OpenAlex가 이 저작을 철회로 표시하지 않았다"고
  주장하게 되어 `.md` 가 말하려던 철회를 침묵시키고, `1` 을 참으로 읽으면 어떤 소스도 하지
  않은 철회를 주장하게 되기 때문입니다. 이 거부는 그저 신호를 미루는 것이 아니라 그 자체가
  신호입니다(`openalex-refresh` 는 같은 값을 비교용 불리언으로 좁혀 아무것도 떠올리지
  않으므로, 백필이 거부하지 않으면 그 저작의 철회는 어디에도 드러나지 않습니다).

다시 실행하면 바이트도 `mtime_ns` 도 동일한 no-op 입니다(이미 있는 레코드는 다시 쓰지
않습니다). 한 id의 읽기/쓰기 오류는 그 저작에 대해서만 보고되며 배치 전체를 죽이지 않습니다.

## 결정론 경계

이 경계를 모르면 `--auto-update` 가 무엇을 고쳤는지 오해하게 됩니다.

1. **임포트는 원장을 고쳐 쓸 권한이 없습니다** (#58, #63). 임포트는 레코드를 새로
   만들 뿐이며, 이미 있는 레코드의 필드를 임포트가 다시 쓰는 일은 없습니다.
2. **`openalex-refresh --auto-update` 는 `doi` / `work_type` / `journal` 세 필드만**
   원장에 씁니다. `sources/*.md` 는 절대 건드리지 않습니다(P4: 바이트도 `mtime_ns` 도
   동일하게 유지). 다른 원장 필드도, `imported_at` 도 움직이지 않고, 변경이 없는
   실행은 바이트 단위로 동일합니다.
3. **철회(`is_retracted`)는 두 모드 어디서도 자동 흡수되지 않습니다.** `--auto-update`
   는 이 값을 그대로 통과시킬 뿐 다시 쓰지 않습니다 — 다시 썼다면 다음 실행에서
   "새로 철회됨"이 거짓이 되어 사람에게 올라가는 신호가 사라집니다. 철회는 두 모드
   모두에서 사람에게 보고되고, `openalex-acknowledge-retraction` 으로만 종결됩니다 (#93).
4. **임포트된 항목은 여전히 후보**입니다. `sync → review → accept` 게이트를 거쳐야
   사실이 됩니다 (P1/P2).

### `is_retracted` 는 OpenAlex의 의견입니다

철회 여부는 factlog KB가 주장하는 사실이 아니라 **OpenAlex가 표시한 값**입니다.
OpenAlex는 Lancet Commission 치매 보고서를 철회된 것으로 표시하지만 PubMed에는 철회
기록이 없습니다 (#51). 그래서 front matter 키는 `openalex_is_retracted` 이고, 어느
데이터베이스가 그렇게 주장했는지 읽는 쪽(사람이든 추출 단계든)이 볼 수 있습니다.
맨 `retracted:` 키는 쓰지 않습니다.

arXiv 프리프린트가 저자에 의해 철회(withdrawn)되는 것과는 별개의 절차이며, 공유되는
후속 처리도 없습니다.

## 생성되는 source 파일

`sources/<slug>.md` 하나. YAML front matter + 본문(초록과 OpenAlex/DOI 포인터)입니다.

키는 값이 있을 때만 나타납니다(스키마이지 실제 저작이 아닙니다).

```yaml
---
openalex_id: W2741809807
type: article
title: "..."
authors: [...]
year: 2018
journal: "..."
doi: "..."
pmid: "..."
arxiv_id: "..."
tags: [...]
mesh_terms: [...]
cited_by_count: 1024
abstract_complete: true
primary_topic: "..."          # + primary_topic_score / _subfield / _field / _domain
imported_from: openalex
imported_at: "..."
openalex_is_retracted: true   # OpenAlex가 그렇게 표시한 경우에만
openalex_concepts: [{name: ..., score: 0.9312, level: 2}, ...]
---
```

`arxiv_id` 는 `openalex_arxiv_id` 가 아니라 맨 키입니다 — 교차 소스 색인이 이 키를
그대로 찾고, arXiv 임포터도 같은 키로 씁니다. `openalex_concepts` 는 점수와 레벨까지
전부 남깁니다(측정된 값은 버리지 않습니다). `openalex_` 접두어가 붙는 키는
`openalex_id`, `openalex_is_retracted`, `openalex_concepts` 셋이며, 그중
`openalex_is_retracted` 는 그것이 OpenAlex의 주장임을 드러내려고 그렇습니다 — 위 참조.

## 설정 파일 (선택)

해석 순서: `<KB>/policy/openalex-config.toml` >
`~/.config/factlog/openalex.toml`(`XDG_CONFIG_HOME` 존중) > 내장 기본값.

```toml
[client]
email = "you@example.org"   # 신원 표기(선택). 인증이 아닙니다.

[import]
default_limit = 25          # 1..200, 기본 25
max_limit = 200             # API 상한 200
skip_duplicates = true      # 같은 openalex_id 는 재임포트 시 건너뜀(멱등)
include_abstract = true     # 초록을 본문에 포함
```

> **Zotero와 다른 점**: OpenAlex에는 **secrets 경계가 없습니다.** API에 인증이 없고
> `email` 은 모든 요청의 쿼리 문자열에 실려 가는 신원 표기용 예의일 뿐입니다(과다
> 사용자에게 연락하려고 OpenAlex가 요청합니다). 따라서 커밋되는 KB 정책 파일에 둬도
> 안전합니다 — KB 정책 파일에서 읽기를 거부하는 Zotero의 `web_api_key` 와 대비됩니다.

값의 타입이 틀리면 기본값으로 되돌아갑니다. 다만 `client.email` 이 문자열이 아니면
실패합니다 — 요청 URL에 그대로 실리는 값이라 오타가 조용히 익명 요청으로 떨어지면
안 되기 때문입니다.

## 멱등성과 원본 불변

- 같은 저작을 다시 임포트해도 이미 있는 `openalex_id` 는 건너뛰므로 결과가 같습니다(P3).
- factlog는 기존 `sources/` 원본을 절대 수정하지 않습니다(P4). `--auto-update` 도
  마찬가지입니다 — 원장만 씁니다.
- 임포트된 항목은 후보일 뿐이며, 사람의 `accept` 게이트를 통과해야 사실이 됩니다(P1/P2).
