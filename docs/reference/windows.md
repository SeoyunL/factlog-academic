# Windows

> 🌐 [English](windows.en.md) | **한국어**

## Windows Python 실행 파일

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
를 통해 Python 3.11+ 실행 파일을 찾습니다. `$FACTLOG_PYTHON` 이 설정돼 있으면 그
값이 유일한 후보입니다 — 그 실행 파일이 Python 3.11+ 가 아니면 `python3`/`python`/
`py` 로 **폴백하지 않고 즉시 실패**합니다(종료 코드 127). `python3`, `python`, `py`
순서의 탐색은 `$FACTLOG_PYTHON` 이 설정되지 않았을 때만 적용됩니다.

여러분의 Python이 외부 관리(PEP 668) 상태라면 pip이 그 안으로의 설치를
거부합니다. 이때 `setup` 은 설치를 강행하는 대신 venv 안내를 출력합니다. venv를
만들어 활성화한 뒤 `setup` 을 다시 실행하십시오.

```bash
python3 -m venv ~/.factlog-venv && source ~/.factlog-venv/bin/activate
python3 -m factlog setup --target ~/wiki
```
