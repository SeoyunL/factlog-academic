# Zotero 가져오기 (`factlog zotero-import`)

Zotero 개인 라이브러리의 **서지 메타데이터**를 factlog KB의 `sources/`로 이관하는
명령입니다. factlog는 Zotero의 대체재가 아니라 그 위에 얹히는 검증 층으로,
Zotero는 계속 쓰면서 사실 추출·근거 추적·논리 검증만 factlog가 담당합니다.

이 명령은 **로드맵 단계 1**에 해당합니다. 이관 결과는 여전히 후보(candidate)이며,
평소의 `sync → review → accept` 게이트를 거쳐야 사실이 됩니다. Zotero 원본은 절대
수정되지 않습니다(읽기 전용).

## 사전 준비: Zotero Local API

단계 1은 Zotero **Local API**(로컬 HTTP 서버, 포트 23119)를 씁니다.

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

**종료 코드**: `0` 정상 · `1` 요청/설정/아이템 오류(일부 실패 포함) · `2` Local API 연결
실패 **또는** 잘못된 사용법(선택자 누락/상호배타 등 argparse 오류). `1`과 `2`는
stderr 메시지로 구분합니다.

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

## 설정 파일 (선택)

`~/.config/factlog/zotero.toml` 또는 KB의 `policy/zotero-config.toml`:

```toml
[connection]
mode = "local"       # 단계 1은 local만
local_port = 23119   # 단계 1은 23119 고정(다른 값은 거부됨)

[import]
skip_duplicates = true    # 같은 zotero_key는 재이관 시 건너뜀(멱등)
include_abstract = true   # 초록을 본문에 포함
```

> **보안**: web 자격증명(`web_user_id`/`web_api_key`)은 사용자 레벨 파일에서만
> 읽고 KB 정책 파일에서는 무시합니다. KB가 별도로 버전관리될 때 API 키가 커밋되는
> 것을 막기 위함입니다. (단계 1은 Local API만 지원하므로 web 자격증명은 아직 쓰이지
> 않습니다.)

## 멱등성과 원본 불변

- 같은 컬렉션을 다시 이관해도 이미 있는 `zotero_key`는 건너뛰므로 결과가 같습니다(P3).
- factlog는 Zotero 라이브러리와 기존 `sources/` 원본을 절대 수정하지 않습니다(P4).
- 이관된 항목은 후보일 뿐이며, 사람의 `accept` 게이트를 통과해야 사실이 됩니다(P1/P2).

## 아직 지원하지 않는 것 (단계 1 범위 밖)

PDF 텍스트 자동 변환, 하이라이트·주석 이관, 양방향 동기화, 그룹 라이브러리,
Web API. 이후 단계에서 다룹니다.
