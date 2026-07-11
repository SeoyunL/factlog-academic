# PubMed 가져오기 (`factlog pubmed-*`)

[PubMed](https://pubmed.ncbi.nlm.nih.gov)는 NCBI의 생의학 문헌 색인입니다. factlog는
NCBI E-utilities를 통해 **읽기 전용**으로 메타데이터를 가져옵니다(P4: NCBI에는 아무것도
쓰지 않습니다).

가져온 항목은 `sources/<slug>.md` 원본 하나가 되고, 여전히 **후보**입니다 —
`sync → review → accept` 게이트를 거쳐야 사실이 됩니다(P1/P2). PubMed는 factlog의 사실
저장소가 아니라 입력원입니다.

## 사전 준비

```bash
pip install 'factlog[pubmed]'
```

`httpx` 가 추가됩니다(E-utilities 응답을 받습니다). **API 키는 선택**입니다 — 없으면
초당 3요청, `NCBI_API_KEY`(또는 사용자 설정 파일)로 키를 주면 초당 10요청으로 올라갑니다.
키는 KB에 커밋되는 정책 파일에서는 읽지 않습니다(자격증명 경계).

## `pubmed-search`

```bash
factlog pubmed-search --query "crispr gene editing"
factlog pubmed-search --query "crispr" --show-query                 # 요청 없이 term만 출력
factlog pubmed-search --query "myocardial infarction" --mesh "Myocardial Infarction" --year 2020-2024
factlog pubmed-search --query "sepsis" --limit 50 --porcelain --all
```

- `--query` (필수) — PubMed 문법의 쿼리. 아래 [조용한 0건 함정](#조용한-0건-함정) 을 먼저 읽으세요.
- `--mesh` — MeSH 텀으로 제한. **반복 가능**하며 AND로 묶입니다. `TERM[MeSH Terms]` 로 전송됩니다.
- `--year` — 출판 연도 또는 범위 (`2023`, `2020-2025`)
- `--limit` — 결과 수 (기본 25, 최대 200)
- `--all` — 결과 전체를 프롬프트 없이 선택. stdin이 터미널이 아닐 때 필요합니다.
- `--target` / `--dry-run` / `--porcelain` — 다른 명령과 공통.

> **선택 임포트는 #166(‘PubMedSourceWriter’)과 함께 랜딩합니다.** 지금 이 명령은
> `arxiv-search` 가 #80(비대화형 목록)과 #81(선택·임포트)로 나뉘어 랜딩한 것과 같은 방식으로
> **검색·가드·목록**까지를 제공합니다. 그때까지 `--all`/선택은 무엇이 선택됐는지 보고하고
> 파일은 쓰지 않습니다(임포트 호출은 하나의 국소 seam이며, #166 머지 뒤 그 자리에서
> 공유 임포트 경로로 이어집니다).

### `--show-query` 와 `--dry-run` 은 다릅니다

`arxiv-search` 와 같은 구별입니다.

- `--show-query` 는 **요청을 보내지 않고** 실제로 전송될 `term` 을 출력하고 끝납니다. term은
  클라이언트가 쓰는 것과 같은 composer(`compose_query`)가 만들기 때문에 실제 전송값에서
  표류할 수 없습니다.
- `--dry-run` 은 **검색은 하되** 파일을 쓰지 않습니다 — factlog의 다른 모든 곳과,
  `openalex-search`/`arxiv-search` 와 같은 뜻입니다.

## 조용한 0건 함정

이 명령이 존재하는 이유입니다. **틀린 필드 태그나 존재하지 않는 MeSH 텀은 `esearch` 를
실패시키지 않습니다.** `esearch` 는 HTTP 200과 `<Count>0</Count>` 를 돌려주고, 이는 정직한
빈 결과와 구별되지 않습니다 — 운영자는 "해당 문헌이 없다"고 읽고 그대로 믿습니다. arXiv의
같은 함정(#57)을 `arxiv/config.py` 가 닫힌 어휘와 검증기로 막는 것과 같은 계열의 버그입니다.

factlog는 세 겹으로 막습니다.

1. **필드 태그를 닫힌 집합으로 전송 전 검증.** 쿼리가 쓴 `[...]` 태그가 NLM의 검색 필드
   목록에 없으면 요청을 보내기 전에 거부합니다(`잘못된 필드 태그` → 요청 전 거부).
2. **PubMed 자신의 신호를 그대로 표면화.** PubMed는 매핑하지 못한 구/필드를
   `<ErrorList>`/`<WarningList>`(`PhraseNotFound`, `QuotedPhraseNotFound`, `FieldNotFound`)로
   알려 줍니다. 이 신호를 stderr로 그대로 올립니다 — 존재하지 않는 MeSH 텀은 정확히
   `PhraseNotFound` 를 냅니다.
3. **필터를 쓴 0건은 표면화.** `--year`/`--mesh` 를 걸었는데 0건이면, PubMed가 아무 신호를
   주지 않아도, 어떤 필터가 0으로 좁혔는지 말하고 "존재하지 않는 MeSH 텀이 바로 이 조용한
   0건을 낸다"고 안내합니다.

필터도 신호도 없는 진짜 빈 결과(0건)는 그대로 "Found 0 results." 로 둡니다 — 정직한 0건까지
경고로 시끄럽게 만들지 않습니다.

### MeSH 텀 검증에 대한 결정

MeSH 텀을 **실제 어휘로** 검증하려면 MeSH 트리(다운로드 데이터셋)가 필요하고, 이는 쿼리마다
받아올 것이 아닙니다. 이슈가 준 선택지는 셋 — 번들 서브셋 검증 / 텀 단독 esearch 지연검증 /
표면화+설명. **이 이슈는 표면화+설명(이슈가 정한 바닥선)을 택했습니다.** `--mesh` 텀은
`TERM[MeSH Terms]` 로 그대로 보내고, PubMed가 매핑하지 못하면 위 (2)가 `PhraseNotFound` 를,
(3)이 필터를 건 0건을 표면화합니다. 로컬 MeSH 목록을 싣지 않으므로 그 목록이 낡거나, 번들보다
새로운 텀을 잘못 거부하는 일이 없습니다 — 권위는 PubMed에 두고, factlog의 몫은 그 침묵이
부재로 읽히지 않게 하는 것입니다.

### 여러 단어 쿼리 (#89를 PubMed에 대해 답함)

**arXiv와 달리 factlog는 여러 단어 쿼리를 자동으로 따옴표로 감싸지 않습니다.** PubMed는
Automatic Term Mapping(ATM)으로 단어들을 MeSH·저널 색인에 매핑해 함께 검색하는데, 이것이
대개 의도한 폭넓은 재현율입니다 — 반대로 따옴표로 감싸면 ATM이 **꺼지고** 실제 쿼리가
`QuotedPhraseNotFound` 0건으로 무너질 수 있습니다. 그래서 여러 단어 쿼리는 그대로 보내되
**해석을 다시 쓰는 대신 보이게** 합니다.

- `--show-query` 는 조합된 `term` 을 보여 줍니다.
- 실제 검색은 PubMed 자신의 `<QueryTranslation>` 을 목록 끝에 표면화합니다 — PubMed가 내 단어를
  어떻게 읽었는지 그대로 보입니다.

문자 그대로의 구를 원하면 직접 따옴표로 감싸세요(`--query '"gene therapy"'`). 그때 매칭이 안
되면 (2)가 `QuotedPhraseNotFound` 를 표면화합니다.
