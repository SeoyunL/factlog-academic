# 설치

> 🌐 [English](install.en.md) | **한국어**

## 요구 사항

- Python **3.11+** (엔진 의존성 `pyrewire` 가 요구)
- **pyrewire 1.0.3+** (`pip install -r requirements.txt`)
- Claude Code CLI
- **git** — 마켓플레이스 설치가 내부적으로 `git clone`을 사용합니다. Windows에서는 **Git for Windows**를 설치하세요.

## 설치

factlog-academic은 **Claude Code 플러그인**입니다. Claude Code 세션에서 이 저장소의
마켓플레이스로부터 설치합니다.

*Claude Code에서 실행:*

```
/plugin marketplace add https://github.com/SeoyunL/factlog-academic
/plugin install factlog@seoyunl
/reload-plugins
/factlog setup                     # one-shot: deps + doctor + init, in-session
```

> 업스트림 `semantic-reasoning/factlog` 가 아니라 **이 저장소**에서 설치하십시오.
> 업스트림 플러그인에는 서지 수집 명령이 하나도 없습니다 — `factlog zotero-import`,
> `factlog openalex-*`, `factlog arxiv-*`, `factlog pubmed-*` 는 여기에만 있습니다.
> 설치 핸들 `factlog@seoyunl` 은 `.claude-plugin/marketplace.json` 의
> 마켓플레이스 이름(`seoyunl`)과 플러그인 이름(`factlog`)에서 나옵니다.

위 명령은 **한 줄씩 실행**하십시오. 여러 줄을 한 번에 붙여 넣으면 Claude Code가
마켓플레이스 등록과 설치를 순서대로 처리하지 못할 수 있습니다.

설치가 성공해도 현재 세션에는 새 `/factlog ...` 명령이 즉시 로드되지 않을 수
있습니다. `/plugin install` 다음에 `/reload-plugins` 를 실행한 뒤 `/factlog setup`
을 실행하십시오.

`setup` 은 `doctor` 실행, 엔진 의존성(`pyrewire`) 설치, KB 스캐폴딩, 환경 재점검을
한 명령으로 수행합니다. KB는 기본적으로 홈 디렉터리 아래 `~/wiki` 에 만들어지며
(다른 위치는 `/factlog setup --target <경로>`), setup 요약에 그 **절대경로**가
출력됩니다. 이후 검증할 문서는 그 폴더의 **`sources/`** 에 넣습니다(위
[KB 폴더 구조](concepts.md#kb-폴더-구조--내-파일은-어디에-넣나) 참고).

Windows에서 `python` / `python3` 실행 파일 문제로 `setup` 이 실패하면 상세 레퍼런스의
[Windows Python 실행 파일](../reference/windows.md#windows-python-실행-파일) 절을 참고하십시오.

### 로컬 설치 (개발용)

로컬 클론에 대해 개발하려면, 작업 트리 자체를 마켓플레이스로 등록하십시오.

*Claude Code에서 실행:*

```
/plugin marketplace add ~/git/factlog-academic
/plugin install factlog@seoyunl
/reload-plugins
/factlog setup
```

### `/factlog setup` 이 하는 일

`setup` 은 이전에 분리돼 있던 설치 후 단계들을 한 명령으로 합칩니다. 수동으로 하면
동등하게 다음과 같습니다.

*터미널에서 실행:*

```bash
pip install -r ~/git/factlog-academic/requirements.txt   # pyrewire>=1.0.3,<2.0
python3 -m factlog doctor          # checks Python 3.11+ and pyrewire
python3 -m factlog init --target ~/wiki   # scaffold the KB layout
```

## 학술 서지 연동의 선택 의존성

각 서지 연동은 extra 하나가 필요합니다. **이 저장소에서** 설치하십시오.

> **PyPI의 `factlog` 는 이 프로젝트가 아닙니다.** 그 이름은 2013년의 무관한
> 프로젝트("File ACTivity LOGger", v0.0.1)가 갖고 있습니다. pip에 그 이름을 달라고 하면
> **그 패키지**가 설치됩니다. 게다가 그쪽엔 해당 extra가 없어 pip이 경고만 하고 exit 0
> 으로 끝나므로, 성공 메시지와 원치 않은 패키지만 남고 pyzotero·httpx·feedparser는
> 하나도 들어오지 않습니다. 그 뒤로 연동은 계속 실패합니다. 배포명은
> **`factlog-academic`** 입니다 — extra 를 붙일 때 `-academic` 없는 맨이름을 쓰는 형태는
> 이 저장소 어디에도 쓰지 않습니다. 아래 방법 중 하나로 설치하십시오.

```bash
# 이 저장소를 클론했다면 (대부분 이 경우)
pip install -e '.[zotero]'

# 클론 없이 Claude Code 플러그인으로 설치했다면. 이 변수는 Claude Code 세션 안에서만
# 설정됩니다 — 일반 터미널에서는 /factlog 세션에서 `factlog where` 로 플러그인 경로를
# 확인해 그 경로를 직접 넣으십시오
pip install -e "${CLAUDE_PLUGIN_ROOT}[zotero]"

# 체크아웃 없이 어디서든
pip install 'factlog-academic[zotero] @ git+https://github.com/SeoyunL/factlog-academic'
```

`zotero` 자리에 `openalex` · `arxiv` · `pubmed` 를 넣으면 됩니다.
`pip install -e '.[zotero,arxiv]'` 처럼 한 번에 여러 개도 됩니다.

> **이전 버전에서 올라오셨나요?** 배포명이 `factlog` 였습니다. 두 이름이 같은 `factlog`
> 모듈과 같은 `factlog` 명령을 소유하므로 pip이 둘을 나란히 설치하고, 옛 것만 지우면 그
> 공유 명령이 사라집니다. `pip uninstall factlog` 후 **반드시 재설치**하십시오. 그 상태는
> `factlog doctor` 가 잡아 알려줍니다.

각 연동의 사용법은 [Zotero](../zotero-import.md) · [OpenAlex](../openalex.md) ·
[arXiv](../arxiv.md) · [PubMed](../pubmed.md) 문서에 있습니다.

## 설치 실패 모드 — 증상 → 원인 → 해결

대부분의 설치 문제는 `factlog doctor` 가 이미 진단하고 있습니다. doctor는 각 점검을
`OK` / `INFO` / `WARN` / `FAIL` 중 하나로 출력하고, 문제가 있으면 `→` 로 시작하는
해결 안내를 함께 붙입니다. 아래 표는 그 출력을 증상별로 정리한 것입니다.

| 증상 | 원인 | 해결 |
|------|------|------|
| 설치 후에도 `/factlog …` 명령이 없음 | 현재 세션에 새 명령이 아직 로드되지 않음 | `/reload-plugins` 를 실행한 뒤 `/factlog setup` |
| doctor: `FAIL  Python 3.x < 3.11 필요` | Python이 최소 버전 미만 | Python 3.11 이상을 설치한 뒤 다시 실행 |
| doctor: `FAIL  pyrewire not installed` 또는 `FAIL  pyrewire X < 1.0.3` | 엔진 의존성 미설치/구버전 | `pip install -r requirements.txt` (또는 `/factlog setup` 재실행) |
| `setup`: `factlog setup: this Python is externally managed (PEP 668), so pip refused to install into it.` (*이 Python은 외부 관리 상태라 pip이 설치를 거부했습니다*) 와 함께 pip이 거부 | 배포판이 관리하는 시스템 Python (PEP 668) | venv를 만들어 활성화한 뒤 `setup` 재실행. factlog는 `--break-system-packages` 로 강행하지 않습니다 |
| doctor: `WARN  Python 3.x (Store stub: …\WindowsApps\…)` | Windows의 Microsoft Store Python stub | python.org 정식 배포판 설치를 권장. 또는 `$FACTLOG_PYTHON` 으로 쓸 Python을 지정 |
| doctor: `FAIL  git이 없습니다` | git 미설치 — 마켓플레이스 설치가 `git clone` 을 사용 | macOS는 `xcode-select --install`, 그 외는 패키지 매니저(예: `apt install git`), Windows는 **Git for Windows** |
| doctor: `WARN  FACTLOG_PYTHON = … (경로 없음)` | `$FACTLOG_PYTHON` 이 없는 경로를 가리킴 | 경로를 고치거나 `unset FACTLOG_PYTHON` |
| doctor: `WARN  이 폴더에 factlog/ 폴더가 있어 패키지를 가릴 수 있습니다` | 현재 디렉터리의 `factlog/` 폴더가 설치된 패키지를 가림 | 다른 위치에서 실행하거나 그 폴더 이름을 변경 |
| `[factlog] FACTLOG_PYTHON is set but is not a usable Python 3.11+` (종료 코드 127) | `$FACTLOG_PYTHON` 이 가리키는 실행 파일이 Python 3.11+ 가 아님 | 경로를 고치거나 `unset FACTLOG_PYTHON`. `$FACTLOG_PYTHON` 이 설정돼 있으면 `python3`/`python`/`py` 로 **폴백하지 않고 즉시 실패**합니다 |

> **git FAIL은 `setup` 을 막지 않습니다.** doctor 단독 실행은 git이 없으면 실패
> (종료 코드 1)하지만, `setup` 의 실제 작업(pip 설치 + KB 스캐폴딩)은 git을 쓰지
> 않으므로 git FAIL이 `setup` 의 종료 코드를 뒤집지 않습니다. 다만 마켓플레이스
> 설치 자체는 `git clone` 을 쓰므로, 그 단계에는 여전히 git이 필요합니다.

`WARN` 과 `INFO` 는 종료 코드에 영향을 주지 않습니다 — 오직 `FAIL` 만 셉니다. doctor
요약줄은 `결과: 이상 없음` 또는 `결과: FAIL N개.` 형태로 끝납니다.

Windows의 Python 실행 파일 문제는 상세 레퍼런스의
[Windows Python 실행 파일](../reference/windows.md#windows-python-실행-파일) 절에
전체 절차가 있습니다.
