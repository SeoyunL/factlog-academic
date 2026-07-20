# factlog

> 🌐 [English](README.en.md) | **한국어**

> facts + logic — 마크다운 소스를 **검증 가능하고 출처로 뒷받침되는 사실(fact)**로
> 바꿔 주는 도구입니다. LLM이 추출하고, 결정론적 Datalog/wirelog 엔진이 검증합니다.
>
> factlog는 [Claude Code](https://code.claude.com) **플러그인**입니다. 세션 안에서는
> `/factlog ...` slash command로 쓰고, 검토·승인 같은 사람의 게이트는 터미널에서
> Python CLI(`python3 -m factlog ...`)로 직접 실행합니다. 두 입구 모두 같은 결정론
> 엔진을 호출합니다 — slash command · Python CLI · 검증 엔진, 이 셋이 한 도구입니다.

![factlog 동작 방식: Claude가 제안하고, 엔진이 검증하며, 사람이 확인합니다](docs/how-it-works.svg)

## 요구 사항

- Python **3.11+** (엔진 의존성 `pyrewire` 가 요구)
- **pyrewire 1.0.3+** (`pip install -r requirements.txt`)
- Claude Code CLI
- **git** — 마켓플레이스 설치가 내부적으로 `git clone`을 사용합니다. Windows에서는 **Git for Windows**를 설치하세요.

## 설치

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

로컬 설치(개발용), `/factlog setup` 이 하는 일, PEP 668 venv 안내, Windows Python
실행 파일 문제는 [설치 가이드](docs/guide/install.md)를 보세요.

## 빠른 시작

처음이라면 자기 데이터 없이 흐름을 한 번 완주해 보는
[빠른 시작 튜토리얼](examples/sample-kb/README.md)부터 시작하세요.

## 문서

상세 문서는 [`docs/`](docs/README.md) 에 있습니다.

- [개념](docs/guide/concepts.md) — 개요, KB 폴더 구조, candidate vs accepted 신뢰 경계, 명령 한눈에 보기
- [설치](docs/guide/install.md) — 요구 사항, 마켓플레이스·로컬 설치, `/factlog setup`
- [사용 사례](docs/guide/use-cases.md) — 보고서·슬라이드·논문·wiki의 일반 작업 흐름
- [결정론과 한계](docs/guide/determinism.md) — 무엇이 보장되고 무엇이 보장되지 않는지
- [Slash command 사용법](docs/reference/slash-commands.md) · [소스 파일 형식](docs/reference/sources.md) · [사실 검토](docs/reference/review.md) — 상세 레퍼런스

## 라이선스

Apache-2.0 — [LICENSE](LICENSE) 와 [NOTICE](NOTICE) 참조.
