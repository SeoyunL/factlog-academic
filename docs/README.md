# factlog 문서

> 🌐 [English](README.en.md) | **한국어**

factlog의 상세 문서입니다. 프로젝트 소개는 [저장소 README](../README.md)를 보세요.

## 읽는 순서

처음이라면 아래 순서대로 읽으시면 됩니다.

1. [개념](guide/concepts.md) — factlog가 무엇이고, KB 폴더가 어떻게 생겼고, candidate와 accepted가 어떻게 다른지
2. [설치](guide/install.md) — 요구 사항과 플러그인 설치, `/factlog setup`
3. [사용 사례](guide/use-cases.md) — 보고서·슬라이드·논문·wiki를 실제 명령 흐름으로
4. [Slash command 사용법](reference/slash-commands.md) — `/factlog sync` · `query` · `check` · `repair` · `ask`
5. [사실 검토](reference/review.md) — 후보를 사람이 승인하는 게이트
6. [결정론과 한계](guide/determinism.md) — 무엇이 보장되고 무엇이 보장되지 않는지

자기 데이터 없이 흐름을 한 번 완주해 보려면
[빠른 시작 튜토리얼](../examples/sample-kb/README.md)부터 시작하세요.

## 가이드

| 페이지 | 내용 |
|--------|------|
| [개념](guide/concepts.md) | 개요, KB 폴더 구조, candidate vs accepted 신뢰 경계, 명령 한눈에 보기, 동작 방식 다이어그램 |
| [설치](guide/install.md) | 요구 사항, 마켓플레이스 설치, 로컬 설치(개발용), `/factlog setup` 이 하는 일 |
| [사용 사례](guide/use-cases.md) | 보고서·슬라이드·논문·wiki·출처 추적·후보 정리의 일반 작업 흐름 |
| [결정론과 한계](guide/determinism.md) | 스킬 규율의 한계, AC4 오래된 편집 방지, 규모와 성능 |

## 레퍼런스

| 페이지 | 내용 |
|--------|------|
| [Slash command 사용법](reference/slash-commands.md) | `/factlog sync` · `query` · `check` · `repair` · `ask` |
| [소스 파일 형식](reference/sources.md) | 지원 형식 표, `factlog ingest`, 변환본 명명 규칙, #213 업그레이드 안내 |
| [활성 KB](reference/active-kb.md) | `factlog use`/`where`, 어디서든 KB를 대상으로, KB 해석 우선순위 |
| [사실 검토](reference/review.md) | `factlog review` · `accept` · `reject` · `amend`, 사람의 결정이 갖는 내구성 |
| [어휘 · 검색 · 출처 추적](reference/search-provenance.md) | `factlog vocab` · `search` · `provenance` |
| [타입 지정 관계](reference/typed-relations.md) | `policy/typed-relations.md`, date · ordinal · amount · number |
| [관계 별칭](reference/relation-aliases.md) | `policy/relation-aliases.md`, 표면형 이름을 정규형으로 접기 |
| [단일값 관계](reference/single-valued.md) | `policy/single-valued.md`, subject 당 object 하나, `CONFLICT` 와 해소 방법 |
| [소스 제외와 제거](reference/ignore-eject.md) | `factlog ignore`(sync 제외), `factlog eject`(적재 되돌리기), `--fact` |
| [Windows](reference/windows.md) | Windows Python 실행 파일, Git Bash, PEP 668 venv 안내 |

## 학술 서지 연동

각 연동은 별도 extra 설치가 필요하고(`pip install 'factlog-academic[<이름>] @ git+...'`),
가져온 레코드는 여전히 **후보**입니다 — `sync → review → accept` 게이트를 거칩니다.

| 페이지 | 내용 |
|--------|------|
| [Zotero 가져오기](zotero-import.md) | `factlog zotero-import`, Zotero 7 Local API, `--pdf` · `--annotations` |
| [OpenAlex 가져오기](openalex.md) | `factlog openalex-*`, 검색 · 인용 그래프 · 갱신 · 백필, 크레딧 예산 |
| [arXiv 가져오기](arxiv.md) | `factlog arxiv-*`, 단건·검색 임포트, 버전 추적, 철회(withdrawal) |
| [PubMed 가져오기](pubmed.md) | `factlog pubmed-*`, E-utilities 임포트 · 검색 · 갱신 · MeSH 제안 |

영문 요약본도 있습니다 ([Zotero](zotero-import.en.md) · [OpenAlex](openalex.en.md) ·
[arXiv](arxiv.en.md) · [PubMed](pubmed.en.md)).
