# factlog

> 🌐 [English](README.en.md) | **한국어**

> facts + logic — 마크다운 소스를 **검증 가능하고 출처로 뒷받침되는 사실(fact)**로
> 바꿔 주는 도구입니다. LLM이 추출하고, 결정론적 Datalog/wirelog 엔진이 검증합니다.
>
> factlog는 [Claude Code](https://code.claude.com) **플러그인**입니다. 세션 안에서는
> `/factlog ...` slash command로 쓰고, 검토·승인 같은 사람의 게이트는 터미널에서
> Python CLI(`python3 -m factlog ...`)로 직접 실행합니다. 두 입구 모두 같은 결정론
> 엔진을 호출합니다 — slash command · Python CLI · 검증 엔진, 이 셋이 한 도구입니다.

## 처음 읽는 사람을 위한 안내

### 개요

factlog는 **프로젝트별 지식베이스(KB) 폴더**를 만들어, 그 안의 문서에서 뽑아낸
주장들을 검증 가능하고 출처로 뒷받침되는 사실로 유지해 주는 도구입니다. 위키 한 종류만을
위한 것이 아니라, 사람이 검토할 가치가 있는 문서라면 어디서든 KB 폴더를 만들 수 있습니다.

- 보고서 / 제안서
- 슬라이드(PPT)
- 논문 / 리서치 노트
- 코드 문서 / 설계 문서
- 데이터셋 설명 / 데이터 사전
- 개인·팀 프로젝트 노트
- 이미 운영 중인 wiki

핵심 흐름은 한 줄로 요약됩니다.

```text
문서 -> 후보 사실 -> 사람의 검토 -> accepted 사실 -> 엔진이 확인한 답변
```

> 처음이라면 자기 데이터 없이 흐름을 한 번 완주해 보는
> [빠른 시작 튜토리얼](examples/sample-kb/README.md)부터 시작하세요.

이 도구는 단 하나의 원칙을 따릅니다.

> 에이전트는 결론을 내리지 않는다. 에이전트는 파일을 만들고 CLI를 호출한다.
> CLI가 검증 가능한 리포트를 돌려준다.

- **LLM(세션 내의 Claude)이 추출합니다** — KB 폴더의 `sources/` 에서 후보 사실을
  뽑아내고, 자연어 질문으로부터 Datalog 쿼리 초안을 작성하며, 제한된 범위의 자기
  교정을 시도합니다.
- **결정론적 엔진([pyrewire](https://github.com/semantic-reasoning/PyreWire) 기반
  wirelog)이 검증합니다** — 확정된 사실을 컴파일하고, 로직 체크를 실행하며,
  정책 위반·모순·`review_required` 항목을 드러냅니다.

#### KB 폴더 구조 — 내 파일은 어디에 넣나

`/factlog setup`(또는 `factlog init`)은 **KB 폴더 하나**를 만듭니다. 기본 위치는
홈 디렉터리 아래 `~/wiki` 이고(`~` 는 홈 디렉터리 — macOS `/Users/<이름>`,
Windows `C:\Users\<이름>`), `--target <경로>` 로 원하는 위치를 고를 수 있습니다
(예: `/factlog setup --target ~/my-report`). setup 은 만든 KB의 **절대경로를 요약에
출력**하므로, 그 경로를 파일 탐색기(Windows)·Finder(macOS)로 열면 됩니다.

```text
<KB>/                     ← setup이 만든 폴더 (기본 ~/wiki)
├── sources/              ← 여기에 내 문서를 넣습니다 (보고서·논문·노트·wiki 등)
├── facts/                ← (엔진 산출물) candidates.csv · accepted.dl · logic_report.txt
├── policy/               ← (선택) 검토 질문·정책 규칙
├── runs/                 ← (엔진 산출물) 추출 실행 기록, 바이너리 변환본(runs/sources/)
├── pages/ · decisions/   ← (엔진 산출물) 사람이 손대지 않음
└── templates/
```

> **핵심:** 내가 직접 넣는 것은 `<KB>/sources/` 안의 문서뿐입니다. 나머지 폴더는
> factlog가 채우고 관리합니다. `.docx`·`.pdf` 같은 바이너리도 `sources/` 에 두면
> `/factlog sync` 가 자동으로 텍스트로 변환합니다(→ `runs/sources/`).

#### candidate vs accepted — 신뢰 경계

factlog에는 두 종류의 사실이 있습니다.

- **candidate(후보)** — 문서에서 추출된 *주장*일 뿐입니다. `sync` 는 후보를 만들어
  낼 뿐, 그 자체로 신뢰할 수 있는 사실을 만들지는 **않습니다**.
- **accepted(승인됨)** — 사람이 후보를 검토해 받아들인 사실입니다. **오직 accepted
  사실만 엔진의 입력**이 되고, 질문에 대한 답변의 근거가 됩니다.

이 사람의 검토 단계가 factlog의 **신뢰 경계**입니다. 모델이 만들어 낸 것은 무엇이든
사람이 accepted로 확정하기 전까지는 후보일 뿐입니다.

> **처음이라면** `/factlog sync` 다음에 뭘 할지 막히기 쉽습니다. 명령만으로 끝까지 가는
> 최소 경로(파일 편집·Datalog 없이)는 [docs/getting-started-verify.md](docs/getting-started-verify.md)
> 를 보세요.

#### 명령 한눈에 보기 — slash command · CLI command · KB 파일

위 인트로에서 정의한 대로, factlog의 명령은 **어디서 실행하느냐**에 따라 두 계층으로
나뉩니다. 세 번째 행은 명령이 아니라 그 명령들이 읽고 쓰는 **KB 파일(산출물/위치)**
입니다.

| 구분 | 실행 위치 | 예시 | 역할 |
|------|-----------|------|------|
| **Claude Code slash command** | Claude Code 세션 안 | `/factlog setup`, `/factlog sync`, `/factlog query`, `/factlog check`, `/factlog ask` | 에이전트가 소스를 읽어 후보 사실을 추출하고, 자연어 질문에서 쿼리 초안을 잡고, 엔진 검증 흐름(컴파일·로직 체크·답변)을 호출합니다(검증 자체는 결정론 엔진이 수행). |
| **Python CLI command** | 터미널(shell) | `factlog status`, `factlog review`, `factlog accept`, `factlog reject`, `factlog amend` | KB 상태를 확인하고, 후보 사실을 **사람이** 검토·승인·폐기·수정합니다. `accept`/`reject` 는 후보를 accepted로 확정하거나 폐기하는 **사람의 게이트**입니다(자동 단계가 아님). |
| **KB 파일**(명령 아님 — 산출물/위치) | 프로젝트 KB 폴더 | `sources/`, `facts/candidates.csv`, `facts/accepted.dl`, `facts/logic_report.txt` | 원본(`sources/`), 후보(`candidates.csv`), 엔진 입력(`accepted.dl`)이 놓이는 자리입니다. `facts/logic_report.txt` 는 **엔진이 생성하는 검증 결과가 남는 위치**로, 사람이 편집하지 않습니다. |

이 표가 위 [신뢰 경계](#candidate-vs-accepted--신뢰-경계)와 맞물립니다. slash command
(`/factlog sync`)는 **후보**를 만들 뿐이고, accepted로의 확정은 터미널에서 사람이
실행하는 CLI 게이트(`factlog accept` / `factlog reject` / `factlog amend --accept`)를
거칩니다. 오직 그렇게 확정된 `accepted.dl` 만 엔진 입력이 됩니다.

### 사용 사례 — 일반 작업 흐름

[개요](#개요)에서 나열한 사용처(보고서·슬라이드·논문·코드 문서·데이터셋·노트·wiki)를
실제 명령 흐름으로 풀면 다음과 같습니다. 어느 경우든 골격은 같습니다 —
`sync`/슬래시는 **후보(candidate)** 만 만들고, 터미널에서 사람이 `factlog accept` 로
확정해야 **accepted** 가 되며, 그래야 `check`/`ask` 답변의 근거가 됩니다(위
[신뢰 경계](#candidate-vs-accepted--신뢰-경계) 참고). 처음이라면 자기 데이터 없이 흐름을
완주해 보는 [빠른 시작 튜토리얼](examples/sample-kb/README.md)부터 시작하세요.

**보고서 검증하기**

- 보고서 파일을 KB의 `sources/` 에 둔다
- (Claude Code에서 실행) `/factlog sync` 로 후보 사실을 추출한다
- (터미널에서 실행) `factlog review` 로 후보를 확인하고 `factlog accept` 로 승인한다
- (Claude Code에서 실행) `/factlog check` 또는 `/factlog ask` 로 accepted 사실 기준 답변을 확인한다

**슬라이드(PPT) 발표자료의 주장·근거 점검하기**

- 발표 자료를 텍스트로 정리해 KB의 `sources/` 에 둔다
- (Claude Code에서 실행) `/factlog sync` 로 슬라이드의 주장을 후보 사실로 뽑는다
- (터미널에서 실행) `factlog review` 로 검토하고, 근거가 분명한 주장만 `factlog accept` 로 승인한다
- (Claude Code에서 실행) `/factlog ask` 로 승인된 주장끼리 모순이 없는지 확인한다

**논문·기술 문서의 핵심 주장 정리하기**

- 논문이나 설계 문서를 KB의 `sources/` 에 둔다
- (Claude Code에서 실행) `/factlog sync` 로 핵심 주장을 후보 사실로 추출한다
- (터미널에서 실행) `factlog review` 로 검토하고 `factlog accept` 로 확정한다
- (Claude Code에서 실행) `/factlog check` 로 accepted 사실에 대한 질문을 검증한다

**이미 운영 중인 wiki 유지하기**

- wiki 문서를 KB의 `sources/` 로 가져온다
- (Claude Code에서 실행) `/factlog sync` 로 문서 변경에서 후보 사실을 갱신한다
- (터미널에서 실행) `factlog review` 로 새 후보를 확인하고 `factlog accept` 로 승인한다
- (Claude Code에서 실행) `/factlog check` 로 누적된 accepted 사실의 일관성을 점검한다

**특정 사실의 출처 추적하기**

- (터미널에서 실행) `factlog search` 로 확인하려는 사실을 찾는다
- (터미널에서 실행) `factlog provenance` 로 그 사실이 어느 소스에서 왔는지 추적한다
- 자세한 사용법은 아래 [사실의 출처 추적](#사실의-출처-추적-factlog-provenance) 참고

**잘못 추출된 후보 정리하기**

- (Claude Code에서 실행) `/factlog sync` 로 후보 사실을 만든다
- (터미널에서 실행) `factlog review` 로 후보를 검토한다
- (터미널에서 실행) 잘못 추출된 후보는 `factlog reject` 로 폐기하고, 표현만 다듬을 후보는 `factlog amend` 로 값을 고친 뒤 `factlog accept`(또는 `factlog amend --accept`)로 승인한다
- 자세한 사용법은 아래 [사실 검토](#사실-검토-factlog-review--accept--reject) 참고

### 동작 방식

![factlog 동작 방식: Claude가 제안하고, 엔진이 검증하며, 사람이 확인합니다](docs/how-it-works.svg)

<details>
<summary>텍스트 버전</summary>

```
sources/        →  Claude extracts        →  facts/candidates.csv, pages/, decisions/
candidates       →  human review           →  confirmed facts
confirmed        →  compile (deterministic) →  facts/accepted.dl
questions        →  Claude drafts query     →  facts/query.dl
accepted + query →  wirelog logic check     →  facts/logic_report.txt   ← 검증 가능한 리포트
review_required  →  Claude repairs (gated)  →  decisions/correction_trace.md
```

</details>

### 요구 사항

- Python **3.11+** (엔진 의존성 `pyrewire` 가 요구)
- **pyrewire 1.0.3+** (`pip install -r requirements.txt`)
- Claude Code CLI
- **git** — 마켓플레이스 설치가 내부적으로 `git clone`을 사용합니다. Windows에서는 **Git for Windows**를 설치하세요.

### 설치

factlog는 **Claude Code 플러그인**입니다. Claude Code 세션에서 이 저장소의
마켓플레이스로부터 설치합니다.

*Claude Code에서 실행:*

```
/plugin marketplace add https://github.com/semantic-reasoning/factlog
/plugin install factlog@semantic-reasoning
/reload-plugins
/factlog setup                     # one-shot: deps + doctor + init, in-session
```

위 명령은 **한 줄씩 실행**하십시오. 여러 줄을 한 번에 붙여 넣으면 Claude Code가
마켓플레이스 등록과 설치를 순서대로 처리하지 못할 수 있습니다.

설치가 성공해도 현재 세션에는 새 `/factlog ...` 명령이 즉시 로드되지 않을 수
있습니다. `/plugin install` 다음에 `/reload-plugins` 를 실행한 뒤 `/factlog setup`
을 실행하십시오.

`setup` 은 `doctor` 실행, 엔진 의존성(`pyrewire`) 설치, KB 스캐폴딩, 환경 재점검을
한 명령으로 수행합니다. KB는 기본적으로 홈 디렉터리 아래 `~/wiki` 에 만들어지며
(다른 위치는 `/factlog setup --target <경로>`), setup 요약에 그 **절대경로**가
출력됩니다. 이후 검증할 문서는 그 폴더의 **`sources/`** 에 넣습니다(위
[KB 폴더 구조](#kb-폴더-구조--내-파일은-어디에-넣나) 참고).

Windows에서 `python` / `python3` 실행 파일 문제로 `setup` 이 실패하면 상세 레퍼런스의
[Windows Python 실행 파일](#windows-python-실행-파일) 절을 참고하십시오.

#### 로컬 설치 (개발용)

로컬 클론에 대해 개발하려면, 작업 트리 자체를 마켓플레이스로 등록하십시오.

*Claude Code에서 실행:*

```
/plugin marketplace add ~/git/semantic-reasoning/factlog
/plugin install factlog@semantic-reasoning
/reload-plugins
/factlog setup
```

#### `/factlog setup` 이 하는 일

`setup` 은 이전에 분리돼 있던 설치 후 단계들을 한 명령으로 합칩니다. 수동으로 하면
동등하게 다음과 같습니다.

*터미널에서 실행:*

```bash
pip install -r ~/git/semantic-reasoning/factlog/requirements.txt   # pyrewire>=1.0.3,<2.0
python3 -m factlog doctor          # checks Python 3.11+ and pyrewire
python3 -m factlog init --target ~/wiki   # scaffold the KB layout
```

## 상세 레퍼런스

### 소스 파일 형식

`/factlog sync` 는 `sources/` 아래의 각 파일을 **세션 내에서 텍스트로 읽어**
사실을 추출합니다. 내장 엔진(`merge_candidates.py`)은 모든 파일을 소스 *경로*로
추적하지만 내용은 결코 파싱하지 않습니다 — 따라서 추출 과정에서 텍스트를 읽어낼 수
있는 파일만 적재됩니다. 그러므로 바이너리 원본(예: `.docx`)은 그 자체만으로는
사실을 만들어 내지 못합니다.

| 형식 | 상태 | 비고 |
|--------|--------|-------|
| `.md`, `.markdown`, `.txt` | **직접 지원** | UTF-8 텍스트, 있는 그대로 읽음. 모든 추출 기준이 전제하는 형식입니다. |
| 그 밖의 UTF-8 텍스트 (`.rst`, `.org`, `.csv`, 소스 코드) | 평문으로 지원 | 별도 파싱 없이 원시 텍스트로 취급. |
| `.docx`, 바이너리 `.pdf`, `.odt`, `.epub`, `.html`, `.rtf` | **자동 변환** | `factlog ingest` 가 pandoc / textutil / pdftotext 로 텍스트 변환. |
| `.hwpx` (한컴 OWPML) | **자동 변환** | 내장 추출기(외부 도구 불필요) — zip 내부 `Contents/section*.xml` 텍스트를 읽음. |
| `.hwp` (구형 한컴, HWP 5.x) | **자동 변환** | `hwp5html`(pyhwp) → pandoc → markdown 경로, 표 보존. `pip install pyhwp` + pandoc 필요. 없으면 안내 메시지와 함께 보고. |
| `.pptx` (PowerPoint) | **자동 변환** | 내장 추출기(외부 도구 불필요) — zip 내부 `ppt/slides/slideN.xml` 의 슬라이드 텍스트를 순서대로 읽고, 슬라이드당 한 블록으로 변환. 발표자 노트는 제외, 표 셀은 셀당 한 줄로 펼쳐짐(행/열 그룹 구조는 보존 안 됨). |
| `.xlsx`, 이미지 | **변환 안 됨** | 내장 변환기 없음 — 안내 메시지와 함께 보고. 수동 변환 필요. |

`factlog ingest` 는 변환된 텍스트를 KB의 **`runs/sources/`** 디렉터리(다른 생성
런 아티팩트와 같은 위치)에 기록합니다 — 사용자의 원본이 그대로 남아 있어야 하는
**`sources/` 에는 결코 쓰지 않습니다**. 변환본 파일명은 **원본의 전체 파일명(확장자
포함) + 변환 확장자**로 만들어지므로(`report.hwpx` → `runs/sources/report.hwpx.md`,
`report.pptx` → `runs/sources/report.pptx.md`), 같은 폴더에 이름이 같고 확장자만 다른
두 원본이 하나의 변환본으로 **충돌해 유실되는 일이 없습니다**. 하위 디렉터리에 있는
원본은 그 하위 구조를 그대로 미러링하므로(`sources/sub/report.pdf` →
`runs/sources/sub/report.pdf.md`), 서로 다른 폴더의 동일 이름 파일도 충돌하지 않습니다.
원본은 손대지 않으며, 변환본에는 출처(provenance) 헤더(소스, 변환기, 날짜)가 붙습니다.
`sources/` 와 `runs/sources/` 모두 추출이 읽는 유효한 소스 루트입니다.

> **업그레이드 안내(#213):** 변환본 파일명 규칙이 바뀌었습니다. 예전에는 원본의
> **어간(stem)**만 써서 `report.pdf` → `runs/sources/report.md` 였지만, 이제는
> 원본의 **전체 이름**을 써서 `runs/sources/report.pdf.md` 로 만듭니다. 이 덕분에
> 같은 폴더의 `report.hwpx`·`report.pptx` 가 각각 별도 변환본으로 보존됩니다.
> 기존에 적재된 KB의 구(舊) 어간 변환본(`runs/sources/report.md`)은 `factlog
> sources`/`coverage`/`status` 가 **어간 기반 폴백으로 계속 원본과 짝지어** 인식하므로
> 조용히 유실되지 않습니다. 새 레이아웃으로 옮기려면 `factlog ingest --scan --force`
> 를 다시 실행하십시오(이후 남은 구 변환본은 `factlog eject --orphans` 로 정리).
> 특히 어간이 충돌하던 KB는 재적재해야 유실되었던 원본이 복원됩니다.

*터미널에서 실행:*

```bash
factlog ingest report.docx --target ~/wiki   # → ~/wiki/runs/sources/report.docx.md (pandoc)
factlog ingest --scan --target ~/wiki        # auto-convert every binary under sources/
```

#### 활성 KB (설정해 둔 KB를 어디서든 대상으로)

`factlog init`/`setup`(또는 `factlog use <kb>`) 이후, 선택한 KB가 **활성 KB**로
기록됩니다. 그래서 `ingest`/`ask`/`sync` 및 도구들이 어느 작업 디렉터리에서든
그 KB를 대상으로 동작합니다 — `--target`/`--wiki` 가 필요 없습니다.

*터미널에서 실행:*

```bash
factlog use ~/wiki        # make ~/wiki the active KB (recorded in config)
factlog where             # show the active KB and how it was resolved
factlog sources           # list registered sources (original, conversion, fact count)
factlog status            # KB state: facts by status, vocabulary, conflicts, logic freshness, engine
cd /anywhere && factlog ingest report.pdf   # → ~/wiki/runs/sources/report.txt
factlog eject report.pdf  # inverse of ingest: remove the conversion + retire its facts
factlog ignore drafts/*.md   # exclude sources from sync (re-extraction)
factlog provenance Acme uses FastAPI   # trace a fact to its source(s)
```

> **슬래시 명령(`/factlog …`)도 활성 KB에서 동작합니다.** 다만 factlog **소스
> 저장소 안에서** 실행하면 번들 `examples/sample-kb` 와 혼동될 수 있으니, KB
> 폴더에서 열거나 `factlog use <kb>` 로 활성 KB를 먼저 지정하세요. `factlog where`
> 로 어느 KB가 대상인지 확인할 수 있습니다. 신선도 게이트(PreToolUse 훅)도
> **활성 KB**(`FACTLOG_ROOT > config > cwd` 로 해석된)를 보호합니다 — 활성 KB가
> 아닌 다른 KB의 엔진 입력을 직접 편집하는 경우는 게이트의 대상이 아닙니다.

#### Zotero 서지 가져오기 (`factlog zotero-import`)

이미 Zotero에 문헌을 관리 중이라면, 컬렉션·태그·개별 항목을 factlog `sources/` 로
한 번에 이관할 수 있습니다. factlog는 Zotero의 대체재가 아니라 그 위에 얹히는 검증
층입니다 — Zotero는 계속 쓰고, factlog가 사실 추출·근거 추적·논리 검증을 맡습니다.
이관된 항목은 여전히 **후보**이며 `sync → review → accept` 게이트를 거칩니다. Zotero
원본은 절대 수정되지 않습니다(읽기 전용).

Zotero 7의 **Local API**(Settings → Advanced에서 활성화, 포트 23119)와 `pip install
'factlog[zotero]'` 가 필요합니다.

```bash
factlog zotero-import --collection "Systematic Review"   # 컬렉션 이관
factlog zotero-import --collection "Systematic Review" --dry-run   # 계획만 미리보기
factlog zotero-import --tag "to-review"                  # 태그로
factlog zotero-import --items "KH78JUPE,64DA4TQJ"        # 개별 항목
```

`--target` 없으면 활성 KB로 이관하고, `--porcelain` 은 스크립트용 출력을 냅니다.
`--pdf` 를 주면 각 항목의 PDF 첨부까지 가져와 기존 `ingest` 로 전문을 텍스트 변환합니다
(원본 PDF는 `sources/` 에 저장되니 KB를 버전관리하면 `*.pdf` 를 `.gitignore` 하세요).
`--annotations` 를 주면 하이라이트·노트를 `sources/<stem>-notes.md` 로 이관합니다(여전히
소스일 뿐 — candidate는 `sync` + 사람 게이트를 거칩니다). 이관 후 `/factlog sync` 로 후보
사실을 추출합니다. 자세한 옵션·설정 파일·provenance front matter 는
[docs/zotero-import.md](docs/zotero-import.md) 를 참고하세요.

```bash
factlog zotero-import --collection "Systematic Review" --pdf            # 서지 + PDF 전문
factlog zotero-import --collection "Systematic Review" --annotations    # + 하이라이트·노트
```

#### 어휘 살펴보기 (`factlog vocab`)

`ask` 와 `provenance` 는 정확한 엔티티/관계 이름을 필요로 합니다. `factlog vocab`
은 이를 나열합니다 — 사용 횟수와 함께 엔티티 이름과 관계 이름을 보여 주므로
무엇을 질의할 수 있는지 알 수 있습니다.

```bash
factlog vocab              # entities + relations (engine facts)
factlog vocab --entities   # just entities
factlog vocab --relations  # just relations (tagged [attribute] / [single-valued] / [typed:<type>])
factlog vocab --all        # include non-engine names (candidate/needs_review/superseded)
```

선언된 속성(attribute) 관계의 객체는 엔티티가 아니라 리터럴이므로 엔티티 목록에서
제외됩니다(`status` 와 동일한 타이핑).

#### 타입 지정 관계 (`policy/typed-relations.md`)

어떤 관계의 리터럴 객체는 단순 매칭이 아니라 **비교**되어야 합니다 — 그래야
결정론적 엔진이 그 값을 정렬(ordering)하거나 임계값(threshold)으로 거르거나
범위(range)로 질의할 수 있습니다(예: "2030년 이후 출시", "순위 <= 3"). 그런
관계는 `policy/typed-relations.md` 에 선언합니다. 객체가 리터럴이므로 해당 관계는
`policy/attribute-relations.md` 에도 함께 선언해야 합니다.

한 줄에 하나씩 선언합니다:

```
- `relation name` : <type> as <ascii_alias>
```

`<ascii_alias>` 는 비교 가능한 값을 담는 엔진 사이드 관계의 이름으로, 관계 이름이
비ASCII 여도 합법적인 엔진 식별자로 남도록 작성자가 직접 고르는 ASCII 식별자
(`[A-Za-z_][A-Za-z0-9_]*`)입니다. 공백이 포함된 관계 이름은 백틱으로 감쌉니다.

네 가지 타입:

- `date` — `2030.1` / `2030-01-15` → 정렬 가능한 yyyymmdd. **엔진 투영 지원**
  (정렬/임계값/범위).
- `ordinal` — `rank 3` / `3rd` → 정수 순위. **엔진 투영 지원**.
- `amount` — `100억` / `1,000원` → 정수 기본 단위. **엔진 투영 지원**. 단위 표가
  필요하며, 줄 끝에 인라인으로 줄 수 있습니다: `: amount as <alias> (억=1e8, 만=1e4, 원=1)`
  (값은 양의 정수). 절을 생략하면 기본 단위 표를 씁니다.
- `number` — `1,000` / `3.5` → 수치 크기. **엔진 투영 지원**: 정렬 가능한
  int64 로 ×1000 스케일됩니다(소수점 3자리). ⚠️ 비교 술어의 임계값은 반드시
  **스케일된 단위**로 적어야 합니다: `version >= 2.0` → `version_num(S, V), V >= 2000`.
  소수 3자리를 넘는 정밀도는 반올림됩니다(ROUND_HALF_UP).

추출기는 타입 지정 리터럴 객체를 구조가 보존되는 compact compound term으로 써도
됩니다: `date(2030,1)`, `date(2030,1,15)`, `number(2.5)`, `ordinal(3)`,
`amount(100,"억")`. `relation/3` 의 객체는 이 term 문자열을 그대로 보관하고,
타입 지정 사이드 관계가 비교 가능한 스칼라로 투영합니다.

`factlog vocab` 은 선언된 타입 지정 관계에 `[typed:<type>]` 태그를 붙여 보여
줍니다(예: `[attribute, typed:date]`).

#### 사실 찾기 (`factlog search`)

정확한 이름을 모를 때는 `factlog search <term>` 이 주어/관계/객체 전체에 대해
대소문자 구분 없는 부분 문자열 매칭을 수행하고, 일치하는 사실을(상태와 출처 수와
함께) 나열합니다. `vocab` 은 이름을 나열하고, `search` 는 조각으로 사실을 찾으며,
`provenance` 는 정확한 트리플을 추적합니다.

```bash
factlog search fastapi   # case-insensitive; matches 'FastAPI'
factlog search acme      # partial — every fact mentioning the fragment
```

#### 사실의 출처 추적 (`factlog provenance`)

모든 사실은 자신이 추출된 소스를 기록합니다. `factlog provenance`(별칭 `trace`)
는 일치하는 사실에 대해 그 근거가 되는 모든 행을 나열합니다 — **소스 경로, 상태,
신뢰도, 노트(추출된 발췌문), 그리고 소스 파일이 디스크에 없을 때의 `[stale]`
표시**까지. (`superseded`/`needs_review` 를 포함한) 모든 상태가 표시되므로,
폐기된 근거도 계속 보입니다.

```bash
factlog provenance Acme uses FastAPI   # exact triple
factlog provenance Acme uses           # all objects for (subject, relation)
factlog provenance Acme                # all facts about a subject
factlog provenance - uses              # relation only ('-' wildcards a position)
factlog provenance - - FastAPI         # object only
```

위치 인자는 `(subject, relation, object)` 접두사입니다. 리터럴 `-` 는 해당 위치를
와일드카드 처리하며, 생략된 뒤쪽 위치도 와일드카드로 취급됩니다(와일드카드가 아닌
항목이 최소 하나는 있어야 합니다). 공백이 포함된 항목은 따옴표로 감싸십시오.

`/factlog ask` 역시 검증된 엔진 답변 아래에 각 근거 소스 경로(`← <source>`)를
나열하므로, 쿼리로 찾은 사실을 그 자리에서 추적할 수 있습니다.

#### 사실 검토 (`factlog review` / `accept` / `reject`)

추출은 사실을 `candidate` 또는 `needs_review` 로 표시하며, `confirmed`/`accepted`
사실만 엔진 입력이 됩니다. `facts/candidates.csv` 를 직접 손대지 않고 승격하거나
폐기할 수 있습니다.

*터미널에서 실행 (후보를 사람이 검토·승인하는 게이트):*

```bash
factlog review                       # list the pending queue (candidate + needs_review)
factlog review --status needs_review # narrow to one pending status
factlog accept Acme uses FastAPI     # pending → accepted (compiled into accepted.dl)
factlog accept Acme                  # accept every pending fact about a subject ('-' wildcards a position)
factlog reject Acme uses Datadog     # pending → superseded (retired, kept for audit)
factlog accept Acme uses FastAPI --dry-run
```

`accept`/`reject` 는 **대기(pending) 행만** 변경합니다. `confirmed`/`accepted`/
`superseded` 와 일치하는 항목은 보고만 되고 그대로 유지됩니다(대기 상태가 아닌
사실을 폐기하려면 `factlog eject` 를 사용). 둘 다 `accepted.dl` 을 재컴파일합니다.

상태가 아니라 사실의 **값 자체를 교정**하려면 `factlog amend` 를 사용하십시오.

*터미널에서 실행:*

```bash
factlog amend Widget codename Draft --set-object Falcon --set-note "name finalized" --accept
factlog amend Acme uses FastApi --set-object FastAPI    # fix a typo
```

위치 트리플이 사실을 식별하고(정확히 일치), `--set-subject` / `--set-relation` /
`--set-object` / `--set-note` 가 새 값을 줍니다(최소 하나, 또는 `--accept`). amend
는 `candidates.csv` **와** 그 근거가 되는 `runs/*.json` 을 **둘 다** 갱신하므로
편집이 `/factlog sync` 후에도 살아남습니다(사실의 값은 `runs/*.json` 에 있으며,
merge 가 그로부터 `candidates.csv` 를 재구성합니다). `--accept` 는 `accepted` 로
승격까지 합니다. 신뢰도는 편집할 수 없습니다. `--dry-run` 으로 미리 볼 수 있습니다.

> **내구성(durability):** 사람이 한 `accept`(및 `amend --accept`)는 `reject`/
> `superseded` 와 같은 방식으로 재머지 후에도 보존됩니다 — `/factlog sync` 가
> 여러분의 결정을 되돌리지 않습니다.

#### sync에서 소스 제외하기 (`factlog ignore`)

`/factlog sync` 는 매 실행마다 **모든** 소스를 다시 추출합니다. 특정 소스를
그 대상에서 빼려면 — 초안, 작업 중인 문서, 외부 문서 등 — KB별 **sync-ignore
목록**(`policy/sync-ignore.md`)에 추가하십시오. 무시된 소스는 **수정되더라도**
`/factlog sync`, `factlog ingest --scan`, 커버리지 누락 보고에서 건너뜁니다. 이미
머지된 사실은 그대로 유지됩니다(사실을 실제로 제거하려면 `factlog eject` 사용).

```bash
factlog ignore drafts/*.md sources/wip-notes.md   # add pattern(s)
factlog ignore                                     # list patterns + what they match
factlog ignore --remove drafts/*.md               # remove a pattern
```

`policy/sync-ignore.md` 는 한 줄에 글롭(glob) 하나씩 적습니다(다른 정책 파일과
같은 너그러운 형식 — `#` 주석, `-` 불릿, 백틱 인용 항목 지원. `#` 로 시작하는
패턴은 백틱으로 감싸십시오). 패턴은 소스의 전체 ref(`sources/...` /
`runs/sources/...`) 또는 소스 루트 내 경로로 매칭됩니다. 글롭 의미:
`*` 와 `?` 는 한 경로 세그먼트 안에 머물고(`/` 를 **넘지 않음**), `**` 는
세그먼트를 넘으며, 끝의 `/` 는 그 하위 트리 전체를 뜻합니다.

| 패턴 | 매칭 대상 |
|---------|---------|
| `drafts/*.md` | `sources/drafts/x.md` — 단, `sources/drafts/sub/x.md` 는 아님 |
| `drafts/**` (또는 `drafts/`) | `sources/drafts/` 아래 전부 |
| `**/*.md` | 임의 깊이의 모든 `.md` |

`factlog sources` 는 무시된 소스를 `[ignored]` 로 표시하고, 커버리지는 이를 누락이
아니라 `excluded` 로 보고합니다.

#### 소스 제거 (`factlog eject`) — `ingest` 의 역연산

`factlog eject <source>` 는 적재(ingest)를 되돌립니다. `runs/sources/` 변환본을
삭제하고, 해당 소스에서 추출된 행을 `runs/*.json` 에서 제거하며, 그 소스를 인용하는
사실을 폐기합니다. 소스는 파일명, 어간(stem), 또는 KB 기준 상대 경로로 지정할 수
있습니다 — 바이너리 원본(예: `report.pdf`)을 지정하면 그 `runs/sources/<원본이름>.md`
변환본도 함께 매칭되고(변환본의 provenance 헤더로 짝을 확인), 어간만 주면 같은 어간을
가진 모든 소스가 매칭됩니다.

```bash
factlog eject report.pdf                 # delete conversion; mark citing facts superseded (kept for audit)
factlog eject report.pdf --purge         # delete the citing candidate rows instead of superseding them
factlog eject report.pdf --delete-original  # also delete the user's original under sources/
factlog eject report.pdf --dry-run       # show the planned changes, modify nothing
```

##### 사실 하나만 제거 (`--fact`)

소스 자체는 멀쩡한데 추출된 사실 하나가 잘못된 경우, 그 사실만 폐기할 수
있습니다 — 소스의 변환본과 원본은 그대로 남습니다.

```bash
factlog eject --fact "을서비스" "정식_운영" "2030.1"      # retire one fact (mark superseded)
factlog eject --fact "갑봇" "통합" "을서비스" --fact "값가" "대체" "값나"   # several at once
factlog eject --fact "을서비스" "정식_운영" "2030.1" --purge   # delete the candidate row instead
```

사실은 **모든** 소스에 걸쳐 그 `(subject, relation, object)` 트리플로 매칭됩니다.
기본값인 `superseded` 는 `runs/*.json` 을 건드리지 않으므로 폐기가 내구성을
가집니다 — 이후 `/factlog sync` 가 소스로부터 사실을 다시 주장하더라도
`merge_candidates` 가 그것을 계속 superseded 로 유지합니다. 반면 `--purge` 는 행을
삭제하고 `runs/*.json` 에서도 제거합니다. 소스가 여전히 그 사실을 주장한다면 재싱크
시 다시 추출되므로, 사실을 영구히 폐기하려면 기본값을 사용하십시오. fact 모드와
source 모드는 상호 배타적이며, `--delete-original` 은 `--fact` 와 함께 쓸 수
없습니다.

기본적으로 폐기된 사실은 `superseded` 로 표시되어(감사 목적으로
`facts/candidates.csv` 에 남음) `sources/` 아래 원본은 **유지**됩니다 — 따라서 다음
`/factlog sync` 때 다시 변환됩니다. 원본까지 제거하려면 `--delete-original` 을
넘기십시오. `accepted.dl` 은 재컴파일되어 엔진 입력에서 폐기된 사실이 즉시
빠집니다.

`runs/sources/` 변환본은 적재 출처 헤더를 통해 그것을 만들어 낸 원본과 묶여
있으므로, 두 원본이 어간을 공유하더라도 `eject report.docx` 가 `report.pptx` 의
변환본을 건드리지 않습니다. `pages/` 는 `eject` 로 재생성되지 않습니다 —
`/factlog sync` 를 실행해 맞추십시오. 기본값 `superseded` 는 현재 상태 기준의
폐기입니다. **텍스트** 원본을 `sources/` 아래 그대로 두면 다음 `/factlog sync` 가
그 사실을 다시 추출·주장하므로, 소스를 영구히 제거하려면 `--purge` 와/또는
`--delete-original` 을 넘기십시오.

해석 우선순위: `--target`/`--wiki` 플래그 > `$FACTLOG_ROOT` > 활성 KB 설정
(`${XDG_CONFIG_HOME:-~/.config}/factlog/config.json`) > 현재 디렉터리. 설정이 없으면
동작은 종전과 같습니다(현재 디렉터리 사용).

`/factlog sync` 는 첫 단계로 `factlog ingest --scan` 을 실행하므로, `sources/` 에
넣어 둔 바이너리는 자동으로 변환됩니다(멱등적으로 — 바뀌지 않은 파일은 건너뜀).
바이너리에 `runs/sources/` 변환본이 없으면 `merge_candidates.py` 가 경고하여,
조용한 비적재(non-ingestion)가 드러나게 합니다.

### Windows Python 실행 파일

Windows에서 factlog의 `.sh`/Bash 도구(예: `factlog_python.sh`)는 Git Bash로
실행됩니다. **Git for Windows**를 설치하면 Git Bash가 함께 제공되어, factlog의
번들 `.sh` 스크립트가 그 위에서 실행됩니다.

Windows에서는 `python3` 명령이 실제 Python이 아니라 Microsoft Store stub을
가리킬 수 있습니다. 이 경우 `python` 또는 `py`는 정상이어도 플러그인의 번들
스크립트가 실패할 수 있습니다.

먼저 다음을 확인하십시오.

```powershell
python3 --version
python --version
py -0p
```

`python3 --version`이 `Python`만 출력하고 실패하거나 Microsoft Store를 여는
상태라면, factlog가 사용할 Python을 명시하십시오. venv를 쓰는 경우:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e <path-to-factlog-repo>
$env:FACTLOG_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
```

플러그인의 hook과 skill 명령은 `${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh`
를 통해 `$FACTLOG_PYTHON`, `python3`, `python`, `py` 순서로 Python 3.11+
실행 파일을 찾습니다.

여러분의 Python이 외부 관리(PEP 668) 상태라면 pip이 그 안으로의 설치를
거부합니다. 이때 `setup` 은 설치를 강행하는 대신 venv 안내를 출력합니다. venv를
만들어 활성화한 뒤 `setup` 을 다시 실행하십시오.

```bash
python3 -m venv ~/.factlog-venv && source ~/.factlog-venv/bin/activate
python3 -m factlog setup --target ~/wiki
```

### 사용법

지식베이스 안의 Claude Code 세션에서(플러그인은 모든 세션에서 활성):

*Claude Code에서 실행:*

```
/factlog sync      # read sources/, extract candidate facts, update pages & decisions
/factlog query     # translate policy/questions.md into facts/query.dl (Datalog query draft)
/factlog check     # compile accepted facts, run the logic check over accepted + query, show the report
/factlog repair    # attempt gated self-correction of review_required queries
/factlog ask       # answer one question: deterministically routed to the engine (verified) or wiki exploration (unverified)
```

`/factlog check` 전에 `/factlog query` 를 실행하십시오. 로직 체크는
`facts/query.dl` 의 쿼리 초안을 평가하는데, 이 초안은 `/factlog query` 가
`policy/questions.md` 의 자연어 질문으로부터 생성합니다.

### 결정론과 한계

스킬은 곧 프롬프트이므로, 모델은 각 단계를 실행하도록 *유도*될 뿐 *강제*되지는
않습니다. factlog는 신뢰성이 필수인 모든 단계 — 사실 컴파일, wirelog 로직 체크,
정책 컴파일, 검증 — 를 모델의 판단이 아니라 **스킬이 실행하고 신뢰하도록 지시받는
번들 스크립트**로 유지합니다. 로직 체크 리포트는 언제나 엔진이 생성하며, 모델이
서술하지 않습니다.

#### AC4 — 오래된 편집 방지 (두 단계)

factlog는 두 가지 서로 다른 메커니즘으로 신선도(freshness)를 강제합니다.

| 단계 | 메커니즘 | 보장하는 것 |
|-------|-----------|-------------------|
| **훅으로 강제** | `facts/logic_report.txt` 가 없거나 대상 파일보다 오래되었을 때, `PreToolUse` 훅이 `facts/accepted.dl` 또는 `facts/query.dl` 로의 모든 `Write`/`Edit` 를 거부합니다(`/factlog check` → `run_logic_check.py` 로 갱신) | 로직 리포트가 오래된 상태에서는 엔진의 컴파일된 입력을 덮어쓸 수 없습니다 — 훅이 파일에 손대기 전에 도구 호출을 차단합니다 |
| **SKILL 규율 (최선 노력)** | `SKILL.md` 는 어떤 결론을 말하기 전에 Claude가 `run_logic_check.py` 를 실행하고 `facts/logic_report.txt` 를 그대로 보여 주도록 지시합니다 | 모델은 엔진 리포트를 드러내도록 *유도*되지만 *강제*될 수는 없습니다(R10: "완전히 보장할 수 없음") — 원시 리포트에 대한 사람의 검토가 최종 검증 단계입니다 |

이 두 단계는 상호 보완적입니다. 훅은 결정론적 빈틈을 메우고, SKILL 규율은
엔지니어링적 강제가 불가능한 서술(narration) 계층을 담당합니다.

#### 규모와 성능

**성능 때문에 KB를 비울 필요는 없습니다.** 로직 체크 비용은 총 사실 수보다
**엔티티 간 관계**(A→B에서 B가 다시 subject가 되는 엣지) 수에 더 민감합니다 —
엔진이 도달성(path)을 계산하기 때문입니다. object가 대체로 리터럴인 속성 위주
KB는 수만~수십만 사실까지 저렴하게 확장되고, 촘촘한 엔티티 그래프(인용·의존망
등)는 그보다 빨리 무거워질 수 있습니다. 따라서 지켜볼 지표는 총 사실 수가 아니라
**엔티티↔엔티티 엣지 수**입니다.

무거워지더라도 해법은 "비우기"가 아닙니다. 관계 설계를 조정하고,
`factlog ignore`(재추출 제외)와 멱등 ingest로 반복 비용을 관리하면 됩니다.
정확성과 중복 방지는 규모와 무관하게 그대로 유지됩니다.

## 라이선스

Apache-2.0 — [LICENSE](LICENSE) 와 [NOTICE](NOTICE) 참조.
