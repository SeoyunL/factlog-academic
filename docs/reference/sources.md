# 소스 파일 형식

> 🌐 [English](sources.en.md) | **한국어**

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

### 형식별 변환기와 사전 요구사항

위 표의 “자동 변환”이 실제로 무엇을 요구하는지는 형식마다 다릅니다. 각 확장자에는
**변환기 체인**이 있고, factlog는 그 체인을 순서대로 훑어 **처음으로 사용 가능한**
변환기를 고릅니다(내장 변환기는 항상 사용 가능, 외부 도구는 PATH에 있을 때만).

| 형식 | 변환기 체인 (순서대로) | 필요한 설치 | 출력 |
|------|------------------------|-------------|------|
| `.docx`, `.odt` | pandoc → textutil | pandoc, 또는 macOS의 textutil | `.md` (pandoc) / `.txt` (textutil) |
| `.html`, `.htm` | pandoc → textutil | pandoc, 또는 macOS의 textutil | `.md` / `.txt` |
| `.epub` | pandoc | pandoc | `.md` |
| `.rtf` | textutil | **macOS 전용** — textutil은 macOS에 기본 포함 | `.txt` |
| `.pdf` | pdftotext | poppler (`pdftotext`) | `.txt` |
| `.hwpx` | `factlog-hwpx` (내장) | 없음 — 표준 라이브러리만 사용 | `.md` |
| `.pptx` | `factlog-pptx` (내장) | 없음 — 표준 라이브러리만 사용 | `.md` |
| `.hwp` | `factlog-hwp` (내장, 외부 도구 오케스트레이션) | `hwp5html`(`pip install pyhwp`) **와** pandoc 둘 다 | `.md` |
| `.xlsx`, `.png`, `.jpg`, `.jpeg` | 없음 | — | 변환 안 됨 |

설치 안내는 도구별로 이렇게 안내됩니다 — pandoc은 `brew install pandoc`(또는
<https://pandoc.org>), pdftotext는 poppler(`brew install poppler`), textutil은
macOS에 기본 포함됩니다.

`.docx`·`.odt`·`.html`·`.htm` 에 **폴백 체인**이 있다는 점이 중요합니다. pandoc이
없어도 macOS라면 textutil이 대신 처리합니다(대신 마크다운이 아니라 평문 `.txt` 로
떨어져 표 구조가 보존되지 않습니다). 반면 `.epub`·`.rtf`·`.pdf` 는 체인이 하나뿐이라
그 도구가 없으면 변환되지 않습니다.

### 변환기가 없을 때의 동작

변환기를 찾지 못했을 때 `ingest` 가 실패로 처리할지 아닐지는 **파일을 어떻게
지정했는지**에 달려 있습니다.

| 상황 | `--scan` (자동 발견) | 명시적 `ingest <파일>` |
|------|----------------------|------------------------|
| 체인의 도구가 PATH에 없음 (예: pandoc 없이 `.docx`) | `skipped` 로 집계, **실행은 성공**(종료 코드 0) | `failed` 로 집계, **종료 코드 1** |
| 변환기가 아예 없는 형식 (`.xlsx`, 이미지) | `skipped`, 종료 코드 0 | `failed`, 종료 코드 1 |
| 내장 변환기가 외부 도구를 못 찾음 (`.hwp` 에 pyhwp/pandoc 없음) | `skipped`, 종료 코드 0 | `failed`, 종료 코드 1 |

어느 쪽이든 **이유는 항상 stderr에 출력**되므로 조용히 넘어가지 않습니다.

```text
factlog ingest: no converter on PATH for .pdf (tried: pdftotext). install poppler (e.g. `brew install poppler`)
factlog ingest: skip y.xlsx (.xlsx): no built-in converter; export sheets to .csv and place those in sources/
factlog ingest: 0 converted, 2 skipped, 0 failed
```

이 비대칭은 의도된 것입니다. `--scan` 은 `/factlog sync` 의 사전 단계로 돌기 때문에,
변환할 수 없는 파일 하나가 sync 전체를 실패시키면 안 됩니다. 반대로 사용자가 파일을
직접 지목했다면 그것은 처리해 달라는 요청이므로, 처리하지 못했다면 실패입니다.

`--scan` 은 이 밖에도 두 가지 경우를 따로 집계해 드러냅니다 — **확장자는 변환
대상이지만 내용이 바이너리가 아닌 파일**(예: 평문 `.hwpx`)과 **0바이트 파일**입니다.
둘 다 변환하지 않고 `ignored` 로 보고합니다(전자는 sync가 유효한 소스라면 텍스트로
직접 읽습니다).

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

`/factlog sync` 는 첫 단계로 `factlog ingest --scan` 을 실행하므로, `sources/` 에
넣어 둔 바이너리는 자동으로 변환됩니다(멱등적으로 — 바뀌지 않은 파일은 건너뜀).
바이너리에 `runs/sources/` 변환본이 없으면 `merge_candidates.py` 가 경고하여,
조용한 비적재(non-ingestion)가 드러나게 합니다.
