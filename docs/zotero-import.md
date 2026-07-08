# Zotero 가져오기 (`factlog zotero-import`)

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
pip install 'factlog[zotero]'
```

## 사용법

```
factlog zotero-import (--collection <name> | --tag <tag> | --items <k1,k2,...>)
                      [--target <kb>] [--dry-run] [--porcelain]
```

세 선택자 중 **정확히 하나**를 지정합니다.

| 옵션 | 설명 |
|---|---|
| `--collection <name>` | 컬렉션 이름으로 이관(정확 일치 → 대소문자 무시 폴백; 다중 일치는 모호성 오류, 없으면 사용 가능한 이름 안내) |
| `--tag <tag>` | 태그로 이관 |
| `--items <ids>` | 쉼표로 구분한 Zotero item 키 목록 |
| `--target <path>` | 대상 KB(기본: 활성 KB — `factlog where` 참조) |
| `--dry-run` | 파일을 만들지 않고 이관 계획(예상 파일명 포함)만 표시 |
| `--porcelain` | 스크립트용 기계 출력(탭 구분) |
| `--pdf` | 각 항목의 PDF 첨부도 `sources/`로 가져와 텍스트로 변환(아래 참조) |
| `--annotations` | 각 항목의 하이라이트·노트를 `sources/<stem>-notes.md`로 이관(아래 참조) |

컬렉션에 섞여 있는 첨부(PDF)·노트는 제외하고 **top-level 서지 아이템만** 가져옵니다.

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

1. 항목의 노트(`note`)와 PDF 첨부의 하이라이트(highlight/underline/note/text —
   image/ink 제외)를 Local API로 수집합니다.
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
  `written`(신규)/`updated`(재작성)/`skipped`를 구분해 보고합니다.
- **P4**: 사용자가 직접 만든 `<stem>-notes.md`(우리 마커 없음)는 덮어쓰지 않고 건너뜁니다.
- 부분 실패는 항목 단위로 격리됩니다 — 한 항목의 주석 수집이 실패해도 나머지는 계속되며
  `annotation_errors`에 반영됩니다.
- `--pdf`와 함께 쓸 수 있습니다: `--pdf --annotations`로 전문 + 주석을 한 번에.

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
