# Zotero 가져오기 (`factlog zotero-import`)

> 🌐 [English](zotero-import.en.md) | **한국어**

Zotero 개인 라이브러리의 **서지 메타데이터**를 factlog KB의 `sources/`로 이관하는
명령입니다. factlog는 Zotero의 대체재가 아니라 그 위에 얹히는 검증 층으로,
Zotero는 계속 쓰면서 사실 추출·근거 추적·논리 검증만 factlog가 담당합니다.

기본 명령은 **서지 메타데이터**를 이관하고(로드맵 단계 1), `--pdf`를 주면 각 항목의
**PDF 첨부 전문**까지 가져옵니다(단계 2 — 아래 참조). 어느 경우든 이관 결과는 여전히
후보(candidate)이며, 평소의 `sync → review → accept` 게이트를 거쳐야 사실이 됩니다.
Zotero 원본은 절대 수정되지 않습니다(읽기 전용).

## 사전 준비: Zotero Local API

이 통합은 Zotero **Local API**(로컬 HTTP 서버, 포트 23119)를 씁니다.

1. Zotero 7 데스크톱 앱 설치 후 실행.
2. **Edit → Settings → Advanced**에서
   **"Allow other applications on this computer to communicate with Zotero"** 체크.
3. 앱을 켜 둔 상태에서만 응답합니다(앱이 꺼져 있으면 명령이 우아하게 실패).

확인:

```bash
curl http://localhost:23119/api/users/0/collections   # JSON 배열이면 정상
```

pyzotero 의존성은 선택 설치입니다:

```bash
pip install 'factlog-academic[zotero] @ git+https://github.com/SeoyunL/factlog-academic'
```

## 사용법

```
factlog zotero-import (--collection <name> | --tag <tag> | --items <k1,k2,...>)
                      [--target <kb>] [--dry-run] [--porcelain]
```

세 선택자 중 **정확히 하나**를 지정합니다.

| 옵션 | 설명 |
|---|---|
| `--collection <name>` | 컬렉션 이름으로 이관(정확 일치 → 대소문자 무시 폴백; 다중 일치는 모호성 오류, 없으면 사용 가능한 이름 안내 — 20개까지) |
| `--tag <tag>` | 태그로 이관(정확 일치 → 대소문자 무시 폴백; 다중 일치는 모호성 오류, 없으면 사용 가능한 태그 안내 — 20개까지) |
| `--items <ids>` | 쉼표로 구분한 Zotero item 키 목록(라이브러리에 없는 키는 오류) |
| `--target <path>` | 대상 KB(기본: 활성 KB — `factlog where` 참조) |
| `--dry-run` | 파일을 만들지 않고 이관 계획(예상 파일명 포함)만 표시 |
| `--porcelain` | 스크립트용 기계 출력(탭 구분) |
| `--pdf` | 각 항목의 PDF 첨부도 `sources/`로 가져와 텍스트로 변환(아래 참조) |
| `--annotations` | 각 항목의 하이라이트·노트를 `sources/<stem>-notes.md`로 이관(아래 참조) |

컬렉션에 섞여 있는 첨부(PDF)·노트는 제외하고 **top-level 서지 아이템만** 가져옵니다.

세 선택자 모두 **라이브러리에 없는 값**은 오류(exit 1)로 알려 줍니다. 오타 하나가
"0건 이관 성공"으로 보여서 빈 KB가 CI를 통과하는 일을 막기 위해서입니다. 컬렉션과
태그는 사용 가능한 이름을 함께 안내하고(20개까지, 나머지는 `... (N more)`로 요약),
`--items`는 라이브러리에 없는 키만 열거합니다 — 라이브러리 전체 키를 나열하는 것은
도움이 되지 않기 때문입니다.

반대로 **있지만 비어 있는** 값(예: 아무 서지 항목도 달려 있지 않은 태그)은 그대로
성공(exit 0, 0건)입니다. 구분되는 것은 "없음"과 "비어 있음"입니다. `--items`에 PDF
첨부나 노트의 키를 넣은 경우도 후자입니다. 그 키는 라이브러리에 **있으므로** 오류가
아니고, 다만 서지 아이템이 아니라서 걸러집니다(`1 requested`인데 `0 item(s)`로 나오면
이 경우입니다). Zotero UI에서 첨부 키를 복사해 오면 이렇게 됩니다.

`--items`는 **전부 아니면 전무**입니다. 키 하나라도 라이브러리에 없으면 나머지 유효한
키도 이관되지 않습니다. 키를 배치로 넘기는 스크립트라면 오타 하나가 배치 전체를
멈춘다는 뜻이므로, 부분 실패를 감수하려면 키를 나눠 호출하십시오.

`--tag` 값은 **리터럴 태그 이름**으로만 조회합니다. Zotero의 `tag` 파라미터는 검색
문법이라 **선두 `-`는 부정**(그 태그가 붙지 **않은** 항목 전부), **`||`는 OR**(합집합)로
해석됩니다. 이름 자체가 `-`로 시작하거나 `||`를 포함하는 태그는 리터럴로 조회할 수 없어
오류(exit 1)로 거부합니다 — Local API에 이런 문자를 이스케이프할 수단이 없기 때문입니다.
그대로 넘기면 "틀린 항목이 잔뜩" 들어오고 `Errors: 0`으로 성공하는 것보다, 명시적 실패가
낫습니다. 내부 하이픈(예: `Computer Science - Performance`)은 리터럴이라 정상 조회됩니다.

### 예시

```bash
# 컬렉션 이관
factlog zotero-import --collection "neurosymbolic AI"

# 먼저 계획만 확인
factlog zotero-import --collection "neurosymbolic AI" --dry-run

# 태그 / 개별 항목
factlog zotero-import --tag "to-review"
factlog zotero-import --items "KH78JUPE,64DA4TQJ"

# 서지 + PDF 전문까지 한 번에
factlog zotero-import --collection "neurosymbolic AI" --pdf

# 하이라이트·노트까지
factlog zotero-import --collection "neurosymbolic AI" --annotations
```

이관 후 후보 사실을 추출하려면 `/factlog sync`를 실행합니다.

## 출력

사람용:

```
Connecting to Zotero (Local API)...
Found collection "neurosymbolic AI": 10 item(s)
Importing to KB: /home/user/wiki

  ✓ Independence Is Not an Issue in Neurosymbolic AI (64DA4TQJ) - imported
  ↷ Reasoning in Neurosymbolic AI (XFGIZTV9) - skipped (already imported (zotero_key match))
  ...

Summary:
  Imported: 9
  Skipped:  1
  Errors:   0

Next step: run '/factlog sync' to extract candidate facts.
```

기계용(`--porcelain`): 탭 구분, 순서 무관, LF 종료. 하드 에러(연결/설정) 시 stdout은
비어 있고 종료 코드가 0이 아닙니다.

```
imported	9
skipped	1
errors	0
dry_run	0
target	/home/user/wiki/sources
```

`--dry-run --porcelain`에서는 각 아이템 앞에 예상 파일명 행이 추가됩니다:

```
item	imported	64DA4TQJ	faronius-2025-independence-is-not-an-issue-in-neurosymbolic-ai.md
...
```

`--pdf`를 주면 PDF 배치 카운트 행이 추가됩니다:

```
pdf_placed	9
pdf_skipped	1
pdf_errors	0
```

`--annotations`를 주면 주석 카운트 행이 추가됩니다(`written`=신규, `updated`=변경 재작성):

```
annotations_written	6
annotations_updated	0
annotations_skipped	0
annotation_errors	0
```

**종료 코드**: `0` 정상 · `1` 요청/설정/아이템/PDF/주석 오류 또는 변환 실패(일부 실패
포함) · `2` Local API 연결 실패 **또는** 잘못된 사용법(선택자 누락/상호배타 등 argparse
오류). `1`과 `2`는 stderr 메시지로 구분합니다.

## 생성되는 source 파일

항목당 하나의 마크다운이 `sources/<slug>.md`로 만들어집니다. 파일명 규칙은
`{첫저자성}-{연도}-{제목축약}.md`(저자 없음 → `anonymous`, 연도 없음 → `n-d`,
충돌 시 `-2`/`-3`). 상단 YAML front matter에 provenance가 실립니다:

```markdown
---
zotero_key: "64DA4TQJ"
item_type: "preprint"
title: "Independence Is Not an Issue in Neurosymbolic AI"
authors: ["Faronius Håkan Karlsson", "Martires Pedro Zuidberg Dos"]
year: "2025"
doi: "10.48550/arXiv.2504.07851"
tags: ["Computer Science - Artificial Intelligence", "neurosymbolic AI"]
imported_from: zotero
imported_at: "2026-07-08T01:12:31+00:00"
---

# Independence Is Not an Issue in Neurosymbolic AI

## Abstract
...

## Original source
- Zotero item: `zotero://select/library/items/64DA4TQJ`
- DOI: 10.48550/arXiv.2504.07851
```

## PDF 전문 가져오기 (`--pdf`)

`--pdf`를 주면 서지 이관에 더해 각 항목의 **PDF 첨부**를 가져와 전문을 확보합니다.

동작:

1. 항목의 **저장형 PDF 첨부**(Zotero에 저장된 것; 웹/로컬 링크 첨부는 제외)를 Local
   API로 다운로드해 `sources/<stem>-<attkey>.pdf`로 저장합니다. `<stem>`은 그 항목의
   서지 `.md`와 같아 서로 짝지어지고, 첨부 키(`<attkey>`)로 유일해져 첨부가 여러 개여도
   안정적입니다. 이미 있으면 다시 받지 않습니다(멱등, 원본 미덮어쓰기).
2. 저장한 PDF를 factlog의 기존 **`ingest` 파이프라인**으로 변환해
   `runs/sources/<stem>-<attkey>.pdf.txt`(provenance 헤더 포함)를 만듭니다. 이는
   수동으로 PDF를 넣고 `/factlog sync`를 돌릴 때와 정확히 같은 경로입니다. 변환은
   `pdftotext`(poppler)가 필요합니다.
3. `sync`는 `runs/sources/`도 읽으므로, 이 전문에서도 후보 사실이 추출됩니다. 서지
   `.md`(메타데이터·`zotero_key`)와 PDF 전문은 공유하는 `<stem>` 파일명으로 짝지어져
   함께 유지됩니다.

```bash
factlog zotero-import --collection "neurosymbolic AI" --pdf
factlog zotero-import --collection "neurosymbolic AI" --pdf --dry-run   # 변환 없이 계획만
```

- 스캔 PDF(텍스트 레이어 없음)는 `pdftotext`가 빈 텍스트를 내며, import 시 인라인으로
  도는 `ingest` 출력과 이후 `factlog status`가 "converted-but-empty (likely
  scanned/needs OCR)"로 표시합니다(OCR은 범위 밖).
- **저작권**: 원본 PDF 바이너리가 `sources/`에 저장되므로, KB를 버전관리한다면
  `.gitignore`에 `*.pdf`(또는 `sources/**/*.pdf`)를 넣어 커밋을 막으세요. 변환 텍스트가
  놓이는 `runs/`는 이미 생성물이라 커밋 대상이 아닙니다.
- 부분 실패는 첨부 단위로 격리됩니다 — PDF 하나를 못 받아도 나머지는 계속되며, 실패
  개수가 요약/`pdf_errors`에 반영됩니다.

## 하이라이트·노트 가져오기 (`--annotations`)

`--annotations`를 주면 각 항목의 **PDF 하이라이트**와 **노트**를 항목당
`sources/<stem>-notes.md`로 이관합니다(서지 `<stem>.md`와 짝지어짐).

동작:

1. 항목의 노트(`note`)와 PDF 첨부의 하이라이트(**image/ink를 제외한 모든 주석 타입** —
   highlight/underline/note/text 등)를 Local API로 수집합니다.
2. 하이라이트는 페이지 라벨·강조 구절(인용)·메모로, 노트는 HTML을 텍스트로 풀어
   `## Highlights` / `## Notes` 섹션에 씁니다. front matter에 `zotero_key`와
   `source_kind: annotations` 마커가 실립니다.
3. **변환 불필요** — 이미 markdown이라 `sync`가 `sources/*.md`를 직접 읽어 candidate를
   추출합니다.

**P1 경계**: 하이라이트·노트는 candidate로 직접 쓰이지 않고 **소스 텍스트**로만
들어갑니다. candidate는 여전히 `sync`(LLM 추출)와 사람의 `accept` 게이트를 거칩니다 —
에이전트는 결론을 내리지 않습니다.

```bash
factlog zotero-import --collection "neurosymbolic AI" --annotations
factlog zotero-import --collection "neurosymbolic AI" --annotations --dry-run
```

- **멱등·신선도**: `<stem>-notes.md`의 내용은 Zotero 상태의 순수 함수(import 시각 없음)라,
  변화 없으면 그대로 두고(skipped) 하이라이트가 늘면 다시 씁니다(updated). 요약에
  `written`(신규)/`updated`(재작성)/`skipped`를 구분해 보고합니다. `skipped`에는 무변경뿐
  아니라 아래 P4(사용자 소유 파일 미덮어쓰기) 케이스도 포함됩니다.
- **P4**: 사용자가 직접 만든 `<stem>-notes.md`(우리 마커 없음)는 덮어쓰지 않고 건너뜁니다.
- 부분 실패는 항목 단위로 격리됩니다 — 한 항목의 주석 수집이 실패해도 나머지는 계속되며
  `annotation_errors`에 반영됩니다.
- `--pdf`와 함께 쓸 수 있습니다: `--pdf --annotations`로 전문 + 주석을 한 번에.

## BibTeX 내보내기 (`factlog export --bibtex`)

이관·검증한 소스를 LaTeX/Word에서 인용하려면 provenance를 BibTeX로 내보냅니다
(Zotero→factlog 단방향을 넘어 인용 왕복을 닫음). 읽기 전용, 결정론(파일명 순).

```bash
factlog export --bibtex                 # BibTeX (LaTeX)
factlog export --csl -o refs.json       # CSL-JSON (Pandoc/Zotero/Word)
```

`--bibtex`|`--csl` 중 하나를 지정합니다. `sources/*.md` front matter의 provenance를 항목당
한 엔트리로 냅니다(id/citation key=슬러그 stem, 저자·연도·저널·DOI·PMID).
주석 소스(`source_kind: annotations`)와 provenance 없는 파일은 제외합니다. stdout 출력은
순수 BibTeX/JSON(진행 메시지는 stderr)이라 `> refs.bib`로 바로 저장할 수 있습니다.

### 엔트리 타입 결정 순서

연동마다 타입을 담는 front matter 키가 다릅니다. Zotero의 `item_type`만 읽던 동안
OpenAlex/arXiv/PubMed 레코드가 전부 기본 타입으로 떨어졌기 때문에(#384), 아래 순서로
**먼저 답하는 키 하나**를 채택합니다.

| 순서 | 키 | 쓰는 연동 | 예 |
|---|---|---|---|
| 1 | `item_type` | Zotero | `journalArticle` → `@article` / `article-journal` |
| 2 | `type` (단, `imported_from: openalex`일 때만) | OpenAlex | `conference-paper` → `@inproceedings` / `paper-conference` |
| 3 | `preprint: true` | arXiv | → `@misc` / `article` |
| 4 | (아무 키도 없을 때) `journal` 유무 | PubMed | 저널명이 있으면 `@article` / `article-journal` |

4단계는 **타입을 선언한 키가 하나도 없을 때만** 동작합니다. 선언된 타입을 `journal`로
덮어쓰지 않는다는 뜻이고, 이유는 두 가지입니다. Zotero는 `publicationTitle`을 item type과
무관하게 `journal`로 옮기므로 잡지·신문 기사가 저널 논문으로 잘못 기재되고, arXiv 기탁본은
`journal`이 후속 게재를 기록해도 여전히 preprint이기 때문입니다(#60). 매핑이 없는 타입은
4단계로 추측하지 않고 기본값(`@misc` / `document`)으로 두며, 필요하면 매핑 표
(`factlog/bibtex.py`의 `_ENTRY_TYPES`, `factlog/csl.py`의 `_CSL_TYPES`)에 추가합니다.

`type` 키는 provenance 원장에서 *소스 이름*을 담는 RESERVED 키이기도 해서(#73),
OpenAlex writer가 실제로 남긴 레코드에서만 신뢰합니다.

### 게재지(`journal`)가 들어가는 필드

표준 BibTeX는 게재지 필드를 엔트리 타입별로 좁게 정의합니다. `journal`은 **`@article`
전용**이라, 다른 타입에 붙이면 조용히 버려집니다. 그래서 front matter의 `journal` 값이
**무엇인지**(정기간행물인지, 이 글을 담은 더 큰 저작인지, 발행 기관인지)를 먼저 판정하고
두 exporter가 그 판정을 함께 씁니다.

| 게재지 성격 | 해당 타입 | BibTeX 필드 | CSL 변수 |
|---|---|---|---|
| 정기간행물 | journal/magazine/newspaper 기사 | `journal` | `container-title` |
| 수록 저작 | 학회 논문, 단행본 장, 사전·백과 항목 | `booktitle` | `container-title` |
| 발행 기관 | 보고서 | `institution` | `publisher` |
| 학위 수여 기관 | 학위논문 | `school` | `publisher` |
| 비정형 | preprint, dataset, software, 미분류 | `howpublished` | `container-title` |
| 시리즈 | 단행본 전체 | `series` | `collection-title` |

`@inproceedings`/`@incollection`은 `booktitle`을 요구하므로, 이전처럼 `journal`에 넣으면
게재지를 잃는 동시에 `Warning--empty booktitle`까지 발생합니다.

**어떤 역할도 값을 버리지 않습니다.** 여섯 역할 모두 값을 *다른 이름의 필드로 옮길* 뿐
삭제하지 않습니다. 단행본 전체는 담긴 상위 저작이 없지만, 그 위치의 값은 그 책이 속한
시리즈 이름이므로 `series`/`collection-title`로 보냅니다. 잘못 놓인 값은 사람이 되살릴
수 있지만 버려진 값은 되살릴 수 없습니다.

**비정형(preprint 등)의 CSL 변수는 왜 `container-title`인가**: 비정형 레코드의 CSL
타입은 `article`(preprint)입니다 — CSL 1.0.2에 `preprint` 타입이 없고, #60이 게재 후에도
deposit의 재분류를 금지하기 때문입니다. 렌더링을 먼저 실측해 이 축이 선택을 강제하는지
확인했고, **강제하지 않습니다.** `Nature 585, 357 (2020)`을 담은 preprint 1건을
`pandoc 3.10 --citeproc`으로 렌더해 게재지가 남는지(Y)/사라지는지(N) 본 결과:

| 변수 | chicago | apa | ieee | nature | ama | 계 |
|---|---|---|---|---|---|---|
| `container-title` | Y | Y | **N** | Y | Y | 4/5 |
| `publisher` | Y | Y | Y | **N** | Y | 4/5 |

**완전한 무승부입니다.** `container-title`은 IEEE가 버리고, `publisher`는 nature가
버립니다(`nature.csl`의 `type="article"` 분기가 `publisher`를 아예 참조하지 않습니다).
`genre`를 함께 내보내면 preprint 신분 표기도 양쪽 4/5로 같습니다.

따라서 렌더링은 결정 근거가 못 되고, 남는 기준은 **값이 무엇이냐**입니다. arXiv의
`journal_ref`나 Zotero preprint의 `publicationTitle`은 **정기간행물 이름이지 출판사가
아닙니다.** `publisher`는 우연히 출력되는 거짓 진술이고, `container-title`은 IEEE가
우연히 무시하는 참인 진술입니다. 그래서 `container-title`을 씁니다. 이는 `main`이 이미
내보내던 값이기도 해서 CSL 출력이 그만큼 덜 흔들립니다.

**남는 손실을 양쪽 다 적습니다.** `container-title`을 택했으므로 **IEEE에서는 preprint의
게재지가 렌더되지 않습니다.** 반대로 `publisher`를 택했다면 nature에서 사라졌을
것입니다. 어느 쪽도 5/5는 아닙니다.

BibTeX 쪽은 `@misc`가 정의하는 유일한 게재지 필드가 `howpublished`이므로 그대로 씁니다.
즉 이 역할에서 두 포맷은 **의도적으로 다른 필드명**을 씁니다(dataset/software에서 이미
그러하듯이). 그 결과 pandoc으로 BibTeX→CSL 왕복을 하면 `publisher`가 나와 우리가 직접
내보내는 CSL과 어긋나는데, **정확한 쪽은 우리가 직접 내보내는 출력**이며 손실이 있는
제3자 변환에 맞추려고 그것을 틀리게 만들지는 않습니다.

한편 고전 BibTeX 스타일(plain/unsrt/alpha)은 `journal`을 조용히 버리고 `howpublished`는
출력하므로, BibTeX 쪽은 이전보다 **개선**입니다.

**`genre: "Preprint"`**: CSL에 `preprint` 타입이 없으므로 preprint 신분을 `genre`로
함께 내보냅니다. 실측상 apa가 `[Preprint]`를 새로 붙이고(없으면 누락) 나머지 스타일은
변화가 없어 순이득입니다. dataset·software·미분류는 게재지 역할만 같을 뿐 preprint가
아니므로 `genre`를 붙이지 않습니다.

### 기존 KB 영향

Zotero 전용 KB의 출력도 바뀝니다. 재실행하면 반영되며, 별도 마이그레이션 명령은
필요 없습니다. Zotero itemType 13종을 `main`과 이 브랜치에서 각각 내보내 비교한
실측입니다 — **BibTeX 12종, CSL 8종**이 바뀝니다(`journalArticle`은 양쪽 다 불변).

| itemType | BibTeX 이전 → 이후 | CSL 이전 → 이후 |
|---|---|---|
| `journalArticle` | 변화 없음 (`@article` / `journal`) | 변화 없음 (`article-journal` / `container-title`) |
| `magazineArticle` | `@misc` / `journal` → `@article` / `journal` | `document` → `article-magazine` |
| `newspaperArticle` | `@misc` / `journal` → `@article` / `journal` | `document` → `article-newspaper` |
| `encyclopediaArticle` | `@misc` / `journal` → `@incollection` / `booktitle` | `document` → `entry-encyclopedia` |
| `dictionaryEntry` | `@misc` / `journal` → `@incollection` / `booktitle` | `document` → `entry-dictionary` |
| `conferencePaper` | `journal` → `booktitle` | 변화 없음 (`container-title`) |
| `bookSection` | `journal` → `booktitle` | 변화 없음 (`container-title`) |
| `report` | `journal` → `institution` | `container-title` → `publisher` |
| `thesis` | `journal` → `school` | `container-title` → `publisher` |
| `book` | `journal` → `series` | `container-title` → `collection-title` |
| `preprint` | `journal` → `howpublished` | `container-title` 유지 + `genre` 추가 |
| `blogPost`·`webpage` | `journal` → `howpublished` | 변화 없음 (`container-title`) |

타입이 바뀐 네 건(`magazineArticle`·`newspaperArticle`·`encyclopediaArticle`·
`dictionaryEntry`)은 매핑이 없어 기본값으로 떨어지던 것을 정식 매핑으로 채운 결과입니다.
필드가 바뀐 건들은 해당 타입이 정의하지 않는 필드를 쓰고 있던 것을 고친 것입니다 —
`main`은 `@book`/`@incollection`/`@inproceedings`/`@techreport`/`@phdthesis`에도
`journal`을 내고 있었습니다.

## 설정 파일 (선택)

`~/.config/factlog/zotero.toml` 또는 KB의 `policy/zotero-config.toml`:

```toml
[connection]
mode = "local"       # 현재는 local만 지원
local_port = 23119   # 23119 고정(다른 값은 거부됨)

[import]
skip_duplicates = true    # 같은 zotero_key는 재이관 시 건너뜀(멱등)
include_abstract = true   # 초록을 본문에 포함
```

> **보안**: web 자격증명(`web_user_id`/`web_api_key`)은 사용자 레벨 파일에서만
> 읽고 KB 정책 파일에서는 무시합니다. KB가 별도로 버전관리될 때 API 키가 커밋되는
> 것을 막기 위함입니다. (현재는 Local API만 지원하므로 web 자격증명은 아직 쓰이지
> 않습니다.)

## 멱등성과 원본 불변

- 같은 컬렉션을 다시 이관해도 이미 있는 `zotero_key`는 건너뛰므로 결과가 같습니다(P3).
- factlog는 Zotero 라이브러리와 기존 `sources/` 원본을 절대 수정하지 않습니다(P4).
- 이관된 항목은 후보일 뿐이며, 사람의 `accept` 게이트를 통과해야 사실이 됩니다(P1/P2).

## 아직 지원하지 않는 것

스캔 PDF의 OCR, image/ink 주석, 비-PDF 첨부(스냅샷/HTML) 변환, 독립(부모 없는) 노트,
양방향 동기화, 그룹 라이브러리, Web API.
(PDF 텍스트 변환은 `--pdf`, 하이라이트·노트는 `--annotations`로 지원 — 위 참조.)
