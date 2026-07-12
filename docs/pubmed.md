# PubMed 가져오기 (`factlog pubmed-*`)

[PubMed](https://pubmed.ncbi.nlm.nih.gov)는 NCBI의 생의학 문헌 색인입니다. factlog는
NCBI E-utilities를 통해 **읽기 전용**으로 메타데이터를 가져옵니다(P4: NCBI에는 아무것도
쓰지 않습니다). 단건/배치 임포트·검색·갱신·철회 종결·원장 백필·MeSH 제안을 여섯 개의
명령으로 제공합니다.

가져온 항목은 `sources/<slug>.md` 원본 하나가 되고, 여전히 **후보**입니다 —
`sync → review → accept` 게이트를 거쳐야 사실이 됩니다(P1/P2). PubMed는 factlog의 사실
저장소가 아니라 입력원입니다.

> **스펙의 이름과 다릅니다.** 한국어 메인 스펙 §6.2가 `pubmed-verify-status` 로 부른
> 명령은 `pubmed-refresh` 로 shipped 됐습니다 — `openalex-refresh` /
> `arxiv-check-versions` 와 같은 계열의 verb 로 맞췄습니다. 스펙을 들고 온 독자는
> [`pubmed-refresh`](#pubmed-refresh) 를 찾으세요.

## 사전 준비

```bash
pip install 'factlog-academic[pubmed] @ git+https://github.com/SeoyunL/factlog-academic'
```

`httpx` 하나가 추가됩니다(E-utilities 응답을 받습니다). API 키는 NCBI에게는 **선택**,
factlog 배치에게는 **사실상 필요**합니다 — 아래 [API 키](#api-키-ncbi엔-선택-factlog-배치엔-사실상-필요)
를 보세요. 어느 명령이든 **contact email 은 필수**입니다(NCBI가 모든 요청에 실린 연락처를
기대하며, 익명 트래픽은 스로틀·차단 위험). 이메일 없이 요청을 쓰는 명령을 실행하면 거부합니다.

## 명령

| 명령 | 하는 일 | 네트워크 |
| --- | --- | --- |
| `pubmed-import --pmid <PMID>` | PMID로 단건/배치 임포트 | 예 |
| `pubmed-search --query ...` | 검색 후 선택 임포트 | 예(`--show-query` 는 아님) |
| `pubmed-refresh` | 원장의 PubMed 레코드가 표류했는지 확인 | 예(`--dry-run` 은 아님) |
| `pubmed-acknowledge-retraction --id <PMID>` | 철회 신호를 사람이 종결 | 예 |
| `pubmed-backfill-provenance` | front matter만 있는 논문에 원장을 만들어 줌 | 아니요 |
| `pubmed-mesh --for <slug>` | KB 논문의 MeSH를 정규 별칭 후보로 제안 | 예 |

여섯 명령 모두 `--target <KB>`(없으면 활성 KB)를 받습니다. `--porcelain`(스크립트용 탭
구분 출력)은 `pubmed-acknowledge-retraction` 을 뺀 다섯 명령(import·search·mesh·refresh·backfill)이
받고, `--dry-run`(파일을 만들지 않음)은 `pubmed-import` / `pubmed-search` /
`pubmed-refresh` / `pubmed-backfill-provenance` 넷이 받습니다.

**레이트리밋은 크레딧 예산이 아니라 IP 차단 위험입니다.** OpenAlex와 달리 NCBI는 요청 비용을
세지 않습니다 — 대신 초당 요청 상한(키 없이 3/s, 키 있으면 10/s)을 넘겨 버스트하면 **IP를
차단**합니다. 그래서 factlog는 클라이언트에서 요청 사이 최소 간격(키 없이 ~0.34초, 키 있으면
~0.1초)을 스스로 지키고, 여러 명령이 한 클라이언트를 공유해도 상한을 함께 넘지 못하게
single-flight로 직렬화합니다. 429는 `Retry-After` 를 지켜 재시도합니다.

### `pubmed-import`

```bash
factlog pubmed-import --pmid 32738937
factlog pubmed-import --pmid 32738937 --pmid pmid:33301246   # 배치. 'pmid:' 접두어 허용
factlog pubmed-import --pmid 32738937 --dry-run              # 계획만
```

- `--pmid` (필수) — **반복 가능**하고, 한 번의 실행에 **최대 200개**입니다(E-utilities가
  요청당 받는 상한). `pmid:` 접두어를 붙여도 됩니다. `0` 이나 앞자리 0이 붙은 형태는 요청을
  보내기 전에 거부합니다(PMID는 양의 정수).

문법이 틀린 PMID는 그 id만 오류가 되고 나머지 배치는 계속 진행됩니다. 전송·파싱 실패만이
실행 전체를 중단시킵니다 — 부분적으로 신뢰할 수 있는 배치란 없기 때문입니다. 연결 실패는
종료 코드 2, 그 밖의 요청 오류(400·>200개 등)는 1로 끝나고 아무것도 쓰지 않습니다.

`--dry-run` 도 네트워크는 씁니다. 제목·철회 신호·slug 를 알아야 결과를 예측할 수 있기
때문이며, 파일은 만들지 않습니다.

**정체(identity)는 PMID이고, PMID는 교차 소스 조인 키이기도 합니다.** 그래서 이미 OpenAlex나
Zotero로 임포트돼 같은 DOI나 PMID를 가진 논문이 있으면, 두 번째 파일을 쓰지 않고 그 원본의
provenance 원장에 `pubmed` 레코드를 **접습니다**(§7.3). 원본 `.md` 는 건드리지 않습니다(P4).
철회된 논문을 임포트·병합하면 어느 데이터베이스가 그렇게 말했는지 stderr에 경고가 붙습니다 —
철회는 흡수된 사실이 아니라 사람이 확인할 신호입니다(아래 참조).

### `pubmed-search`

```bash
factlog pubmed-search --query "crispr gene editing"
factlog pubmed-search --query "crispr" --show-query                 # 요청 없이 term만 출력
factlog pubmed-search --query "myocardial infarction" --mesh "Myocardial Infarction" --year 2020-2024
factlog pubmed-search --query "sepsis" --limit 50 --dry-run --all   # 검색은 하되 쓰지 않음
factlog pubmed-search --query "sepsis" --limit 50 --porcelain --all
```

- `--query` (필수) — PubMed 문법의 쿼리. 아래 [검색 쿼리의 조용한 함정](#검색-쿼리의-조용한-함정) 을
  먼저 읽으세요.
- `--mesh` — MeSH 텀으로 제한. **반복 가능**하며 AND로 묶입니다. `TERM[MeSH Terms]` 로 전송됩니다.
- `--year` — 출판 연도 또는 범위 (`2023`, `2020-2025`)
- `--limit` — 결과 수 (기본 25, 최대 200)
- `--all` — 결과 전체를 프롬프트 없이 임포트. stdin이 터미널이 아닐 때 필요합니다.

터미널에서 `--all` 없이 실행하면 결과 목록을 보여 주고 어떤 항목을 가져올지 물어봅니다.
터미널이 아니거나 `--porcelain` / `--dry-run` 이면 묻지 않고 아무것도 선택하지 않습니다 —
물을 수 없는 명령이 대신 짐작하지는 않습니다. 그래서 `--dry-run` 으로 계획을 보려면
`--dry-run --all` 이 필요합니다(`arxiv-search` / `openalex-search` 와 같습니다). 선택된 결과는
`pubmed-import` 와 **같은** 임포트 경로(`PubMedSourceWriter`)로 들어가므로, 임포트된 레코드는
여전히 후보이며 `sync → review → accept` 게이트를 거칩니다. 철회 신호가 있는 결과는 선택하기
전에 목록에 표시됩니다.

쿼리 검증(닫힌 필드 태그 집합, `--year`/`--mesh` 문법)은 **네트워크도 이메일도 KB도 필요 없이**
가장 먼저 실행됩니다 — 쿼리 오타는 자격증명이나 활성 KB를 갖추기 전에 즉시 알려 줍니다.

#### `--show-query` 와 `--dry-run` 은 다릅니다

`arxiv-search` 와 같은 구별입니다.

- `--show-query` 는 **요청을 보내지 않고** 실제로 전송될 `term` 을 출력하고 끝납니다. term은
  클라이언트가 쓰는 것과 같은 composer(`compose_query`)가 만들기 때문에 실제 전송값에서
  표류할 수 없습니다.
- `--dry-run` 은 **검색은 하되** 파일을 쓰지 않습니다 — factlog의 다른 모든 곳과,
  `openalex-search`/`arxiv-search` 와 같은 뜻입니다.

#### 검색 쿼리의 조용한 함정

이 명령이 존재하는 이유입니다. **틀린 필드 태그나 존재하지 않는 MeSH 텀은 `esearch` 를
실패시키지 않습니다.** `esearch` 는 HTTP 200과 `<Count>0</Count>` 를 돌려주고, 이는 정직한
빈 결과와 구별되지 않습니다 — 운영자는 "해당 문헌이 없다"고 읽고 그대로 믿습니다. arXiv의
같은 함정(#57)을 `arxiv/config.py` 가 닫힌 어휘와 검증기로 막는 것과 같은 계열의 버그입니다.

factlog는 세 겹으로 막습니다.

1. **필드 태그를 닫힌 집합으로 전송 전 검증.** 쿼리가 쓴 `[...]` 태그가 NLM의 검색 필드
   목록에 없으면 요청을 보내기 전에 거부합니다. `[mh:noexp]` 처럼 콜론 뒤 수식어가 붙으면
   필드 부분만 검증하고 수식어는 PubMed 몫으로 둡니다.
2. **PubMed 자신의 신호를 그대로 표면화.** PubMed는 매핑하지 못한 구/필드를
   `<ErrorList>`/`<WarningList>`(`PhraseNotFound`, `QuotedPhraseNotFound`, `FieldNotFound`)로
   알려 줍니다. 이 신호를 stderr로 그대로 올립니다 — 존재하지 않는 MeSH 텀은 정확히
   `PhraseNotFound` 를 냅니다. 다만 NCBI는 **모든** 0건 응답에
   `OutputMessage: No items found.` 를 얹습니다. 이것은 PubMed가 자발적으로 준 진단이 아니라
   0건의 정형 동반 문구이고 "Found 0 results." 가 이미 말한 것 이상을 말하지 않으므로,
   표면화 대상에서 제외합니다(태그 이름으로만 판정합니다).
3. **필터를 쓴 0건은 표면화.** `--year`/`--mesh` 를 걸었는데 0건이면, 진단 신호가 있든 없든
   항상 어떤 필터가 0으로 좁혔는지 말하고 "존재하지 않는 MeSH 텀이 바로 이 조용한 0건을
   낸다"고 안내합니다. 존재하지 않는 MeSH 텀은 진단 신호와 필터 안내가 함께 나옵니다.

필터도 신호도 없는 진짜 빈 결과(0건)는 그대로 "Found 0 results." 로 둡니다 — 정직한 0건까지
경고로 시끄럽게 만들지 않습니다.

**MeSH 텀 검증에 대한 결정.** MeSH 텀을 실제 어휘로 검증하려면 MeSH 트리(다운로드 데이터셋)가
필요하고, 이는 쿼리마다 받아올 것이 아닙니다. 이슈가 준 선택지는 셋 — 번들 서브셋 검증 / 텀
단독 esearch 지연검증 / 표면화+설명. **이 이슈는 표면화+설명(이슈가 정한 바닥선)을
택했습니다.** `--mesh` 텀은 `TERM[MeSH Terms]` 로 그대로 보내고, PubMed가 매핑하지 못하면 위
(2)가 `PhraseNotFound` 를, (3)이 필터를 건 0건을 표면화합니다. 로컬 MeSH 목록을 싣지 않으므로
그 목록이 낡거나 번들보다 새로운 텀을 잘못 거부하는 일이 없습니다 — 권위는 PubMed에 두고,
factlog의 몫은 그 침묵이 부재로 읽히지 않게 하는 것입니다.

**여러 단어 쿼리 (#89를 PubMed에 대해 답함).** arXiv와 달리 factlog는 여러 단어 쿼리를
자동으로 따옴표로 감싸지 않습니다. PubMed는 Automatic Term Mapping(ATM)으로 단어들을
MeSH·저널 색인에 매핑해 함께 검색하는데, 이것이 대개 의도한 폭넓은 재현율입니다 — 반대로
따옴표로 감싸면 ATM이 **꺼지고** 실제 쿼리가 `QuotedPhraseNotFound` 0건으로 무너질 수
있습니다. 그래서 여러 단어 쿼리는 그대로 보내되 **해석을 다시 쓰는 대신 보이게** 합니다.

- `--show-query` 는 조합된 `term` 을 보여 줍니다.
- 실제 검색은 PubMed 자신의 `<QueryTranslation>` 을 목록 끝에 표면화합니다 — PubMed가 내
  단어를 어떻게 읽었는지 그대로 보입니다.

문자 그대로의 구를 원하면 직접 따옴표로 감싸세요(`--query '"gene therapy"'`). 그때 매칭이 안
되면 (2)가 `QuotedPhraseNotFound` 를 표면화합니다.

### `pubmed-refresh`

KB의 provenance 원장(`<kb>/source-provenance/**/*.json`)에 있는 PubMed 레코드와 front
matter만 있는 PubMed 소스를 PMID로 다시 조회(`efetch`)해, 두-마커 철회 검출기를 다시 돌리고
현재 메타데이터를 원장이 기록한 값과 비교해 **보고**합니다. `openalex-refresh`(#83) ·
`arxiv-check-versions`(#78/#79)의 PubMed 사촌입니다.

```bash
factlog pubmed-refresh                      # 보고만. 원장에 아무것도 쓰지 않음
factlog pubmed-refresh --older-than 0       # 모든 레코드 강제 재확인
factlog pubmed-refresh --only-flagged       # 이미 철회로 기록된 레코드만
factlog pubmed-refresh --auto-update        # 달라진 doi/journal 을 원장에 기록
factlog pubmed-refresh --dry-run            # 무엇을 확인할지와 예상 시간만(네트워크 없음)
```

- `--older-than DAYS` — 최근 DAYS일 안에 확인한 레코드는 건너뜁니다(기본 30). 확인 시각은
  소스 파일 stat이 아니라 check-log에서 읽습니다. `0` 은 전부 재확인합니다.
- `--only-flagged` — 이미 철회로 기록된 레코드만 재확인합니다 — 라이브러리 전체를 다시 받지
  않고 **PubMed가 그새 되돌린** 철회를 싸게 잡는 방법입니다.
- `--auto-update` — 아래 [결정론 경계](#결정론-경계) 참조.
- `--dry-run` — 무엇을 확인할지와 예상 소요 시간을 보여 주되 네트워크도 치지 않고 아무것도
  쓰지 않습니다(check-log 타임스탬프조차). 키가 없으면 "키가 있었다면 얼마나 빨랐을지"를 함께
  보여 줍니다 — 키의 값이 눈에 보이는 곳입니다.

이 명령은 두 가지를 함께 비교합니다 — 좁은 식별/저널 필드 **doi, journal**(`AUTO_UPDATE_FIELDS`)과
**철회 상태**. 둘 중 어느 하나라도 어긋나면 레코드를 `changed` 로 보고합니다. 다만 **`--auto-update`
가 다시 쓰는(원장에 재기록하는) 대상은 doi, journal 뿐입니다** — 철회는 비교·표면화하되 결코
자동으로 재기록하지 않습니다(바로 아래). `doi`/`journal` 은 *전사(transcription)* 사실입니다 —
임포트 때 없던 DOI가 지금 있거나, NLM이 그새 정규화한 저널 약칭. PubMed의 답은 원장이 옮겨
적은 값에 대한 정정이지 세상에 대한 주장이 아니므로, `--auto-update` 아래에서 이것들 — 그리고
이것들만 — 이 원장에 다시 쓰입니다. `--auto-update` 없이 실행하면 check-log 타임스탬프 말고는
아무것도 쓰지 않습니다.

**철회는 자동으로 흡수되지 않습니다.** 새로 검출된 철회는 사람에게 표면화되고 기록된
`retracted` 값은 그대로 통과됩니다 — 사람이 `pubmed-acknowledge-retraction` 으로 기록할
때까지 매 실행마다 계속 올라오게 합니다. 되돌린 철회(원장은 철회, 라이브는 아님)도 값
비교라서 새 철회만큼 크게 표면화됩니다.

#### 병합·삭제된 PMID (#170)

NCBI는 PMID를 병합·삭제합니다. `pubmed-refresh` 는 한 PMID씩 조회해 파서의 네 신호(present /
merged / unparseable / deleted)를 그대로 소비하며, 어느 것도 뭉뚱그리거나 소거법으로 삭제를
추정하지 않습니다.

- **병합** — efetch가 *다른* PMID로 레코드를 돌려줬습니다(NCBI가 둘을 병합). 요청 PMID와 생존
  PMID를 **둘 다** 보고하고, **KB를 절대 조용히 재키잉하지 않습니다.** PMID는 교차 소스
  식별자라 바꾸면 향후 import가 무엇에 병합할지가 달라지므로 사람의 결정(P1)입니다. 병합은
  *제안*될 뿐 따라가지 않으며 — `--auto-update` 도 따라가지 않습니다.
- **삭제** — efetch가 빈(정상 형식) 응답을 돌려줬습니다: PMID가 상류에서 사라졌습니다. 검토용으로
  flag하되 **KB 엔트리를 절대 조용히 drop하지 않습니다.** 삭제된 PMID는 대개 신호가 있는
  제거이고, 엔트리를 지우면 무슨 일이 있었다는 증거가 사라지기 때문입니다. 그리고 **네트워크
  실패는 삭제로 오인하지 않습니다** — 연결·레이트리밋 실패는 위로 전파되어 호출자가 종료
  코드를 정하며, 흔들리는 연결이 살아 있는 논문을 사라진 것으로 flag하는 일은 없습니다.
- **unparseable** — 레코드가 오긴 왔는데 PMID로 환원되지 않았습니다. per-id 오류이지 삭제가
  **아닙니다**: 삭제는 진짜 빈 응답에서만 추론되지 소거법으로 추론되지 않습니다.

front matter만 있는(원장 없는) PMID도 여전히 **읽습니다** — 임포트 이후 나타난 철회는 진짜
소식이고 읽기는 아무것도 쓰지 않으니까요. 다만 새 철회는 원장이 없으면 종결할 수 없으므로,
노트가 `pubmed-backfill-provenance`(#172)를 가리킵니다. 이 명령은 결코 원장을 날조하지 않습니다.

### `pubmed-acknowledge-retraction`

`pubmed-refresh` 는 임포트 사이에 나타난 철회를 사람이 종결할 때까지 실행마다 계속 다시
올립니다. 이 명령이 그 종결 verb이며 — `accept`/`reject` 와 같은 P1 사람 게이트입니다.

```bash
factlog pubmed-acknowledge-retraction --id 32738937
factlog pubmed-acknowledge-retraction --id 32738937 --yes   # 확인 프롬프트 생략
```

- `--id` (필수) — 단 하나의 PMID(`pmid:` 접두어 허용). **`--all` 도 와일드카드도 없습니다.**
  영향 범위는 사람이 고른 id 하나입니다.
- `--yes` — 확인 프롬프트를 건너뜁니다. 터미널이 아닌 환경에서는 필수이며, 없으면 비대화형
  실행은 거부하고 아무것도 쓰지 않습니다.

기록할 값은 상류에만 있으므로 PubMed를 **실시간으로** `efetch` 합니다 — 캐시로 종결하면
거짓일 수 있습니다(PubMed가 이미 철회를 되돌렸을 수도). 다만 조회는 원장을 확인한 **뒤에**
일어납니다(#107): TTY/`--yes` 게이트, 원장 존재, 모든 원장의 가독성을 요청 전에 검사합니다.

- **원장이 없는 논문**(원장 이전 임포트, 또는 미임포트)은 **요청을 0회** 쓰고 거부하며
  `pubmed-backfill-provenance` 를 가리킵니다 — `acknowledge()` 는 원장을 날조하지 않습니다.
- **읽을 수 없는 원장**이 있으면, 그것이 이 id를 담고 있을 수도 있어 기록값이 불명이므로
  거부합니다.

**`--yes` 는 철회를 *기록*할 수는 있어도 *해제*할 수는 없습니다** (#106). 기록은 소리를 내는
방향이고, 해제(라이브가 더 이상 철회로 읽히지 않아 필드를 지우는 것)는 침묵시키는
방향입니다. "더 이상 철회 아님"은 진짜 되돌림일 수도, 아직 철회 마커가 나오지 않은 큐레이션
지연일 수도 있으며 코드는 이 둘을 구별하지 못합니다. `--yes` 아래에서는 사람이 노트를 보지
못하므로 해제는 거부됩니다 — 터미널에서 `--yes` 없이 프롬프트를 보고 확인해야 합니다.

종결은 원장의 `retracted` 필드(`pubmed` 레코드 아래)만 씁니다 — 병합된 top-level `retracted:`
주장은 결코 쓰지 않습니다(§6.4/§7.2). 해제는 필드를 **제거**하며(리터럴 `False` 를 쓰지
않습니다 — 임포트의 "부재=철회 아님" 관례와 어긋나므로). `.md` 는 열지 않습니다(P4): 종결
이후로는 원장이 유일한 감사 기록입니다. 연결 실패는 종료 코드 2, 그 밖의 오류는 1입니다.

### `pubmed-backfill-provenance`

`sources/*.md` 의 front matter에만 존재하는 PubMed 논문(원장 이전 임포트)에, 그 front matter가
함의하는 provenance 원장을 만들어 줍니다. **네트워크를 절대 쓰지 않고**(테스트가 `efetch`
전송이 호출되지 않음을 단언합니다), `sources/*.md` 는 건드리지 않습니다.

```bash
factlog pubmed-backfill-provenance --dry-run   # 원장을 받을 id와 거부될 id를 나열
factlog pubmed-backfill-provenance
```

- `--dry-run` — 무엇이 쓰일지 미리 봅니다. 다만 **미리보기는 실패할 쓰기를 보고할 수
  없습니다** — 쓸 수 없는 `source-provenance/` 는 실제 실행에서만 드러납니다.

이 명령이 필요한 이유는 종결에 있습니다. 원장 이전에 임포트된 논문은 front matter만 있고
원장이 없어 재임포트해도 원장이 생기지 않으므로(front matter 정체 일치에서 sidecar writer
전에 멈춤), `pubmed-acknowledge-retraction` 이 그 논문을 거부하고 바로 이 명령을 가리킵니다.
백필은 front matter가 이미 주장하는 값으로 원장을 세워 그 종결을 가능하게 합니다 — 새 주장을
만드는 것이 아니라 믿음이 저장되는 위치만 바꾸므로, acknowledge와 달리 확인 프롬프트도 `--yes`
도 TTY 게이트도 없습니다. 네트워크를 치면 이는 *refresh* 가 되어 임포트 이후 나타난 철회를
흡수하게 되므로, 백필은 그 경계를 넘지 않습니다.

원장이 담는 필드 — `doi` / `journal` / `retracted` / `retraction_notice_pmid` — 는 모두
writer가 이미 내보내는 front matter 키에서 그대로 읽습니다. `retracted` 는 `_provenance_record`
의 "`True`-또는-부재" 모양을 재현합니다: 값이 없으면 부재로 두고 리터럴 `False` 는 쓰지
않습니다. 백필이 **재현할 수 없는 단 하나**의 필드는 `retraction_verified_at`(임포트가 PubMed를
읽은 시각)입니다 — 백필은 PubMed를 언제도 읽지 않았으므로 정직한 값이 없어 지어내지 않습니다.
식별 필드가 아니라 그 부재가 divergence를 일으키지 않습니다(arXiv의 `submitted` 와 같은
비대칭).

**한 PMID를 두 `.md` 가 나눠 가져도 각자 sidecar를 받습니다** (#117). PubMed 예치본과, `pmid:`
를 교차 참조로 메아리친 다른 데이터베이스의 임포트가 한 PMID를 함께 가질 수 있습니다. 백필은
`refresh` 의 dedup 뷰를 재사용하지 않고 **`.md` 당** 하나씩 걸어(공유 #112 워커) 각 파일의 제
front matter로 제 sidecar를 씁니다 — 커버리지가 파일명 정렬 순서에 좌우되지 않도록.

**refused 되는 경우.** `imported_at` 이 없는 논문, 그리고 `pubmed_retracted` 가 YAML 불리언이
아닌 논문(`1`, `yes`, `on`)입니다. 후자는 공유 writer로 **그대로** 넘겨져 거부되며 결코
추측으로 보정되지 않습니다 — 값을 버리면 "PubMed가 이 논문을 철회로 표시 안 함"을 주장해
`.md` 가 말하려던 철회를 침묵시키고, `1` 을 참으로 읽으면 어떤 소스도 하지 않은 철회를
주장하게 되기 때문입니다. 어떤 스트링이 불리언인지는 오직 `refresh.parse_retraction_flag`
한 곳이 정하고, 백필은 자기 규칙을 더하지 않습니다(두 벌로 적히면 한쪽만 넓힐 때 refresh와
어긋나 #105가 끝내려던 반복이 영원히 돕니다). arXiv의 `version` 같은 식별 필드가 없어(PubMed는
식별 필드를 선언하지 않음, #73) `required` 가드는 `()` 입니다.

### `pubmed-mesh`

이미 KB에 있는 논문의 PubMed MeSH 텀을 정규 어휘의 **별칭 후보**로 제안합니다. major/minor로
나눠 보여 주며 정규 어휘에는 **아무것도 쓰지 않습니다** — 어느 것을 별칭으로 삼을지는 사람의
게이트(P1)가 정합니다.

```bash
factlog pubmed-mesh --for <slug>
factlog pubmed-mesh --for <slug> --porcelain
```

- `--for <slug>` (필수) — 제안 대상 소스 슬러그. `.md` 유무 무관.

**PMID는 front matter가 아니라 provenance 원장에서 읽습니다.** 논문이 이 용도의 PMID를 가지려면
PubMed가 그 논문의 원장에 `type="pubmed"` 레코드를 기여했어야 합니다(`pubmed-import` 나 PubMed
병합이 씁니다). `openalex_id` 논문이 front matter에 `pmid:` 를 메아리친 것은 교차 참조이지
PubMed provenance가 아니며, major/minor 분할은 OpenAlex가 아니라 PubMed가 권위입니다.

- **존재하지 않는 슬러그**는 *오류*입니다(읽을 게 없음).
- 슬러그는 있으나 원장에 **PubMed 레코드가 없는** 논문은 *PMID 없음* 으로 — PMID는 있으나
  **MeSH가 없는**(미색인 레코드) 논문과 범주적으로 구별해 — 보고됩니다. 둘이 다 "비었음"으로
  읽혀서는 안 됩니다.

major 텀은 논문이 *무엇에 관한지* 이므로 쓸모 있는 별칭이고, minor 텀은 그저 *언급된* 것이라
대개 약한 별칭입니다. 그리고 **qualifier로만 major인** 텀은 따로 표시합니다 — OpenAlex의
descriptor-only 읽기가 minor로 오분류할 바로 그 자리입니다(아래 #53 참조).

## 결정론 경계

이 경계를 모르면 `--auto-update` 가 무엇을 고쳤는지 오해하게 됩니다.

1. **임포트는 원장을 고쳐 쓸 권한이 없습니다** (#58, #63). 임포트는 레코드를 새로 만들 뿐이며,
   이미 있는 레코드의 필드를 임포트가 다시 쓰는 일은 없습니다. 새 값을 배우려고 상류에 다녀온
   것은 임포트가 아니라 갱신(refresh)입니다.
2. **`pubmed-refresh --auto-update` 는 `doi` / `journal` 두 필드만** 원장에 씁니다
   (`AUTO_UPDATE_FIELDS`). 다른 원장 필드도, 같은 원장의 비-PubMed 레코드도, `imported_at` 도,
   `pubmed_mesh_*` 도 움직이지 않습니다. `sources/*.md` 는 **절대 열지 않으므로** 바이트도
   `mtime_ns` 도 동일합니다(P4). 두 필드가 이미 일치하면 파일을 다시 쓰지 않는 바이트 단위
   no-op 이며, 이는 리포트가 `unchanged` 라 부르는 바로 그 조건입니다(#121).
3. **철회는 어느 모드에서도 자동으로 흡수되지 않습니다.** `--auto-update` 는 `retracted` 값을
   그대로 통과시킬 뿐 다시 쓰지 않습니다 — 다시 썼다면 다음 실행에서 "새로 철회됨"이 거짓이
   되어 사람에게 올라가는 신호가 사라집니다. 철회는 두 모드 모두에서 사람에게 보고되고
   `pubmed-acknowledge-retraction` 으로만 종결됩니다. 이는 OpenAlex의 `is_retracted`, arXiv의
   `withdrawn_by` 가 따르는 것과 같은 규칙입니다.
4. **병합된 PMID는 자동으로 따라가지 않습니다** (#170). PMID를 재키잉하면 향후 import가 무엇에
   병합할지가 달라지므로(교차 소스 식별자), `--auto-update` 도 이를 따르지 않고 제안만 합니다 —
   `--yes` 가 철회를 해제하지 못하는 것과 같은 이유(#106)입니다. 삭제된 PMID는 상류에서
   사라졌으므로 쓸 것이 없어 건너뜁니다.
5. **백필은 절대 네트워크를 쓰지 않습니다.** 임포트가 기록한 것을 기록하지 임포트 이후 PubMed가
   지금 말하는 것을 기록하지 않으므로, 임포트 뒤에 나타난 철회를 흡수하지 않습니다.
6. **임포트된 항목은 여전히 후보**입니다. `sync → review → accept` 게이트를 거쳐야 사실이
   됩니다 (P1/P2).

### 철회(retraction)는 PubMed의 사실이고, OpenAlex의 의견입니다

`arxiv-check-versions` 의 [철회(withdrawal)는 철회(retraction)가 아닙니다], `openalex-refresh`
의 [`is_retracted` 는 OpenAlex의 의견입니다] 와 함께 이 삼각형을 완성합니다.

PubMed는 저널 철회의 **사실 출처**입니다. OpenAlex는 Lancet Commission 치매 보고서를 철회된
것으로 표시하지만 PubMed에는 철회 기록이 없습니다 (#51) — OpenAlex의 `is_retracted` 는 오탐일
수 있는 *의견*이고, PubMed의 판정은 NLM의 큐레이션에 기반한 *사실*입니다. 데이터베이스들이
어긋날 때 사람이 믿을 우선순위는 **PubMed > Zotero tag > OpenAlex** 입니다: NLM의 색인이 가장
권위 있고, 사서가 손으로 단 Zotero 철회 태그가 그다음, 자동 산출된 OpenAlex 플래그가 마지막
입니다.

**그럼에도 PubMed의 판정조차 자동으로 흡수되지 않습니다.** PubMed가 사실 출처라는 것은
factlog가 그 값을 조용히 top-level `retracted:` 로 접는다는 뜻이 아닙니다. PubMed의 철회는
`pubmed_retracted` 라는 **source-scoped** 신호로 남아, 사람이 `pubmed-acknowledge-retraction`
으로 종결할 때까지 매 refresh마다 표면화됩니다. 우선순위는 "누구를 믿을지"를 정하지 "언제
사람 게이트를 건너뛸지"를 정하지 않습니다.

## 생성되는 source 파일

`sources/<slug>.md` 하나. YAML front matter + 본문(초록과 PubMed/DOI 포인터)입니다. `pmid` /
`title` 은 언제나 나타나고, 나머지 키는 값이 있을 때만 나타납니다.

```yaml
---
pmid: "32738937"
title: "..."
authors: [...]
year: 2020
journal: "..."
doi: "..."                          # 맨 키. pubmed_doi 가 아닙니다
pubmed_mesh_major: [...]            # major 토픽 descriptor (있을 때)
pubmed_mesh_minor: [...]            # minor 토픽 descriptor (있을 때)
imported_from: pubmed
imported_at: "..."
pubmed_retracted: true              # PubMed가 철회로 표시한 경우에만
pubmed_retraction_notice_pmid: "..."   # 링크 가능한 철회 고지 PMID가 있을 때만
---
```

`doi` 가 맨 키인 것은 교차 소스 색인이 그 리터럴 키를 그대로 찾기 때문이며, OpenAlex/arXiv
임포터도 같은 키로 씁니다. 철회 키는 **source-scoped** 입니다 — `openalex_is_retracted` /
`arxiv_withdrawn` 과 나란히, 맨 `retracted:` 를 쓰지 않습니다. PubMed가 철회를 표시할 때만
나타나며, 그 부재는 "철회 아님"을 뜻합니다. 철회된 논문의 본문에는 고지 PMID를 가리키는 경고
블록쿼트가 붙습니다.

### `mesh_terms` 두 필드가 왜 둘 다 있나

OpenAlex→PubMed로(또는 반대로) 임포트돼 두 소스가 다 기술한 논문은 **두 벌의 MeSH를
source-scoped로** 가집니다 — OpenAlex의 평평한 `mesh_terms` 와 PubMed의
`pubmed_mesh_major`/`pubmed_mesh_minor`. 이것은 버그도 중복도 아닙니다. factlog는 두 소스가
말했다는 사실을 각각 기록하지 하나로 화해시키지 않습니다.

둘 다 필요한 이유는 #53의 지뢰입니다. OpenAlex는 descriptor 이름은 거의 정확히
가져오지만(Jaccard 0.990) **major-topic 지위를 떨어뜨립니다**: OpenAlex는 `DescriptorName`
수준의 `MajorTopicYN` 만 읽고 `QualifierName` 수준을 버려서, 2001–2009 레코드에서는 major-topic
Jaccard가 0.10으로 붕괴합니다(그 시기엔 majorness가 qualifier에 실렸기 때문). factlog가 PubMed
자신의 피드를 그대로 먹는 이유가 바로 그 qualifier 수준을 지키기 위해서이고, `pubmed_mesh_*`
네임스페이스가 OpenAlex의 `mesh_terms` 를 덮어쓰지 않고 공존하는 이유입니다.

## API 키 (NCBI엔 선택, factlog 배치엔 사실상 필요)

NCBI에게 API 키는 **선택**입니다 — 없으면 초당 3요청, 있으면 초당 10요청입니다. 하지만 배치
임포트나 라이브러리 전체 refresh를 돌리는 factlog에게는 사실상 필요합니다: 키가 없으면 같은
작업이 3배 이상 걸립니다(`pubmed-refresh --dry-run` 이 "키가 있었다면 얼마"인지 보여 줍니다).

- **어디서 받나.** [NCBI 계정 설정](https://www.ncbi.nlm.nih.gov/account/settings/) →
  로그인 → API Key Management → Create an API Key.
- **어디에 두나.** `NCBI_API_KEY` 환경 변수, `${XDG_CONFIG_HOME:-~/.config}/factlog/pubmed.toml`,
  또는 명시적으로 지정한 경로 — 이 셋뿐입니다.
- **KB 정책 파일에서는 일부러 읽지 않습니다.** `<KB>/policy/pubmed-config.toml` 에 `api_key` 를
  둬도 factlog는 무시합니다. KB는 흔히 그 자체가 버전 관리되는 저장소(소스 재현성)라서, 거기서
  키를 읽으면 자격증명을 커밋되는 저장소로 초대하는 셈이기 때문입니다. 이는 Zotero의
  `web_api_key` 와 같은 경계이고, KB 정책 파일에서 읽어도 안전한 OpenAlex의 `email` 과
  대비됩니다.

**키는 `eutils.ncbi.nlm.nih.gov` 밖으로 나가지 않습니다.** 이 키는 NCBI E-utilities 직접 호출에만
쓰이며, 모델 제공자를 비롯한 어떤 제3자 서비스로도 전송되지 않습니다(키가 없을 때 뜨는 안내의
마지막 두 줄이 이를 명시합니다 — 키 설정을 주저하지 않도록).

`NCBI_API_KEY` 환경 변수는 **어떤 파일보다** 우선합니다. NCBI 자신의 도구가 이 변수를 읽고, CI
러너는 키를 디스크에 쓰지 않고 시크릿 환경 변수로 넘기므로, 환경을 파일보다 앞세우는 것이
자격증명을 디스크 밖에 두는 운영자의 기대와 맞습니다.

## 설정 파일 (선택)

해석 순서: **명시적으로 지정한 경로** > `<KB>/policy/pubmed-config.toml` >
`${XDG_CONFIG_HOME:-~/.config}/factlog/pubmed.toml` > 내장 기본값. (명시 경로는 라이브러리
인자입니다 — pubmed 명령들은 이를 받는 플래그를 노출하지 않습니다. 지정한 파일이 없으면
오류입니다. 가리킨 쪽이 사람이니까요.) 그리고 위에서 말한 대로 `api_key` 만은 KB 정책 파일에서
읽지 않으며, `NCBI_API_KEY` 가 설정돼 있으면 어떤 파일의 `api_key` 도 덮어씁니다.

```toml
[client]
email = "you@example.org"   # NCBI 연락처. 필수(요청 시점에), 인증은 아닙니다.
api_key = "..."             # 선택이나 권장. KB 정책 파일에 두면 무시됩니다.
tool = "factlog"            # E-utilities tool 파라미터. 바꿀 일은 드뭅니다.
```

`email` 은 factlog 정책상 필수입니다 — NCBI가 모든 요청에 실린 연락처를 기대하고, 익명
트래픽은 스로틀·차단 위험이기 때문입니다. 값의 타입이 틀리면 다른 필드는 기본값으로 되돌아가지만,
`client.email` 이 문자열이 아니면 **실패**합니다 — 모든 요청에 그대로 실리는 값이라 오타가
조용히 익명 요청으로 떨어지면 안 되기 때문입니다. 존재는 하되 비어 있는지(값이 실제로 있는지)는
설정 리더가 아니라 임포트 실행 시점에 확인합니다.

## 멱등성과 원본 불변

- 같은 논문을 다시 임포트해도 이미 있는 `pmid` 는 건너뛰므로 결과가 같습니다(P3).
- factlog는 기존 `sources/` 원본을 절대 수정하지 않습니다(P4). `--auto-update` 도,
  `pubmed-acknowledge-retraction` 도, `pubmed-backfill-provenance` 도 마찬가지입니다 —
  원장만 씁니다.
- 임포트된 항목은 후보일 뿐이며, 사람의 `accept` 게이트를 통과해야 사실이 됩니다(P1/P2).
