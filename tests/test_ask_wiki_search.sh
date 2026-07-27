#!/usr/bin/env bash
# tests/test_ask_wiki_search.sh — characterization baseline for /factlog ask
# wiki search QUALITY (keyword generation, corpus recall, ranking, excerpt window,
# rendered block structure).
#
# tests/test_ask_router.sh proves WHERE a question is routed. It says nothing about
# WHAT the wiki path returns once routed there. This file fills that gap, and it is
# deliberately a CHARACTERIZATION test, not a red test:
#
#   Every expectation below is the CURRENT measured behaviour of origin/main,
#   INCLUDING behaviour that is a known defect. The point is not "is it fixed" but
#   "did my change move something I did not intend to move". #571 #572 #573 #574
#   #575 #576 #581 all touch ranking or excerpting; without a pinned baseline there
#   is no way to tell whether two of them silently revert each other.
#
#   Consequently main stays GREEN. A pin marked with the "이 값은 현재 결함을 고정한
#   것이다" comment is NOT a specification — the named issue is expected to change it,
#   and that PR must update the pin explicitly (and say so). Quietly "fixing" a pin
#   without touching the named issue destroys the only signal this file provides.
#
# The corpus is a synthetic KB under mktemp -d. It is synthetic on purpose: the real
# KB is an English corpus, so a Korean-question/Korean-source match cannot be built
# from it, and it contains zero primary-vs-primary path-citation cases (#573). The
# user's own KB is never read or written by this test.
#
# Runs from the working tree via PYTHONPATH. The wiki path never invokes the engine,
# so no pyrewire install is required.
#
# Usage: bash tests/test_ask_wiki_search.sh
#   Returns 0 if all checks pass, 1 if any fail.

set -euo pipefail

# Temp roots are tracked so a harness that runs inside CI's discovery loop leaves
# nothing behind.
_TMP_CFG="$(mktemp -d)"
_TMP_KB="$(mktemp -d)"
trap 'rm -rf "$_TMP_CFG" "$_TMP_KB"' EXIT

export XDG_CONFIG_HOME="$_TMP_CFG/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine
# This is the repo's only harness that pins RANKED ORDER, so the optional neural
# re-rank must be OFF. FACTLOG_EMBED_MODULE inherited from the developer's shell
# reorders search() results and would fail PIN3/PIN4 as a false alarm — a pinned
# baseline that reports defects the code does not have is worse than none.
# (tests/test_ask_router.sh deliberately switches it ON for two cases — the #31
# rerank seam and the #572 grade-override decision; this file needs the opposite,
# so it unsets rather than assuming an unset environment.)
unset FACTLOG_EMBED_MODULE

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
ROUTER="$PLUGIN_ROOT/tools/ask_router.py"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

same() {  # same <desc> <expected> <got>
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 — expected [$2], got [$3]"; fi
}

KB="$_TMP_KB/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
# Start from an empty corpus. `factlog init` already seeds decisions/open-questions.md
# (this fixture overwrites it), and a future seed file joining the corpus would move
# PIN2/PIN3/PIN6 for a reason that lives outside this file — the hardest kind of pin
# failure to diagnose.
rm -f "$KB"/sources/* "$KB"/decisions/*
# The same determinism applied to policy/, which shapes the answer's TAIL: cmd_wiki
# appends POLICY_UNCOMPILED_WARNING after the rendered block when logic-policy.md
# defines rules and logic-policy.dl is absent. Today init's seeded logic-policy.md
# carries no compilable rule so the warning stays silent — but that is init's seed
# deciding this file's pinned geometry. An empty .dl makes _policy_uncompiled() False
# unconditionally, so the tail pin below can only be tripped by the answer itself.
: > "$KB/policy/logic-policy.dl"

# --- fixture -----------------------------------------------------------------
# Bibliographic sources are written in the exact shape `factlog zotero-import`
# emits (YAML front matter, then '# <title>', '## Abstract', '## Original source'),
# so the front-matter geometry the excerpt window trips over is the real one: the
# closing fence lands on line 12 (measured median of real imports; range 10..22).

# (1) Korean paper. The question's keywords appear in the front matter (title, tags)
#     AND in the body title AND in the abstract prose — the abstract sentence is the
#     single most relevant line in the whole corpus.
cat > "$KB/sources/kim-2024-neurosymbolic-grounding.md" <<'EOF'
---
zotero_key: "K2M4N6P8"
item_type: "journalArticle"
title: "신경기호 추론의 근거 추적"
authors: ["Kim, Jisoo", "Park, Minho"]
year: "2024"
journal: "인지과학회지"
doi: "10.5555/ns.2024.001"
tags: ["신경기호", "근거"]
imported_from: zotero
imported_at: "2024-03-01T00:00:00Z"
---

# 신경기호 추론의 근거 추적

## Abstract

이 논문은 신경기호 추론이 산출한 결론의 근거를 역추적하는 절차를 제안한다.

## Original source

- Zotero item: `zotero://select/library/items/K2M4N6P8`
- DOI: 10.5555/ns.2024.001
EOF

# (2) English paper on the SAME topic — the answer a bilingual reader wants, and
#     unreachable from a Korean question by lexical matching alone.
cat > "$KB/sources/faronius-2025-attention-budget.md" <<'EOF'
---
zotero_key: "F5A7B9C1"
item_type: "journalArticle"
title: "Attention Budgets in Neurosymbolic Retrieval"
authors: ["Faronius, Lea"]
year: "2025"
journal: "Journal of Applied Reasoning"
doi: "10.5555/ab.2025.007"
tags: ["neurosymbolic", "retrieval"]
imported_from: zotero
imported_at: "2025-02-01T00:00:00Z"
---

# Attention Budgets in Neurosymbolic Retrieval

## Abstract

This paper measures how a neurosymbolic retriever spends its attention budget and
reports the evidence trail behind each retrieved claim.

## Original source

- Zotero item: `zotero://select/library/items/F5A7B9C1`
- DOI: 10.5555/ab.2025.007
EOF

# (3) Hand-written primary note whose body is a list of KB paths and nothing else.
#     It carries no prose about the topic; every keyword occurrence is a filename.
cat > "$KB/sources/reading-notes.md" <<'EOF'
# 읽기 메모

## 관련 자료

- sources/kim-2024-neurosymbolic-grounding.md
- sources/kim-2024-neurosymbolic-grounding.md 의 3절
- runs/sources/kim-2024-neurosymbolic-grounding.txt

정리는 나중에.
EOF

# (4) Korean prose carrying the BARE stem '근거' with no attached particle.
cat > "$KB/sources/2019-evidence-logging.md" <<'EOF'
# 근거 기록 지침

근거 로그는 최소 5년 보존한다.

기록 담당자는 매 분기 점검한다.
EOF

# (5) A topically unrelated notice. Its only overlap with the question is the
#     grammatical function word '논문은'.
cat > "$KB/sources/0000_RETRACTION_16354850.md" <<'EOF'
# 철회 공지

이 논문은 저자 요청으로 철회되었다.

이 논문은 더 이상 인용해서는 안 된다.

철회 사유는 데이터 오기입이다.
EOF

# (6) decisions/ = SUPPLEMENTARY context: human review notes, not source evidence.
cat > "$KB/decisions/open-questions.md" <<'EOF'
# 열린 질문

## 신경기호

신경기호 추론의 근거를 어디까지 기록할 것인가?
신경기호 근거 로그의 보존 기간은?
신경기호 관련 정책은 아직 없다.
EOF

# (7) pages/ is ENGINE-DERIVED (rebuilt from candidates.csv, needs_review rows and
#     all), so it is excluded from the wiki corpus by design — grepping it would quote
#     a candidate the engine never accepted as if it were source text. The file is
#     seeded with the question's OWN keywords so the exclusion guard is not vacuous:
#     an empty pages/ passes that guard even if pages/ were added to the corpus.
cat > "$KB/pages/신경기호-추론.md" <<'EOF'
<!-- generated-by-factlog -->
# 신경기호 추론

- 근거를 제시한다 -> [[역추적 절차]] (sources/x.md, confidence=0.40)
EOF

# (8) The engine's own vocabulary. The KB's facts are Korean; the sources they were
#     extracted from need not be (#576). accepted.dl carries ONLY the triple — the
#     backing source path exists solely in candidates.csv, so the bridge's join is
#     what this pair exercises.
#
#     `factlog init` writes a header-only candidates.csv and no accepted.dl at all,
#     which is why every pin in this file up to #576 was measured with the bridge
#     inert. That state is the graceful-degrade case (수용 기준 5); PIN7 asserts it
#     directly rather than leaving it implied by a fixture that no longer has it.
cat > "$KB/facts/accepted.dl" <<'EOF'
// hand-written fixture; the wiki path never invokes the compiler
relation("arXiv_2505.0001", "핵심_기법", "신경기호_추론_근거_추적").
relation("arXiv_2505.0002", "이점", "신경기호_기반_철회_판정").
relation("arXiv_2505.0003", "이점", "설명가능성_향상").
relation("arXiv_2505.0004", "이점", "신경기호_로그_보존").
EOF
# 0001  confirmed  -> the English paper. The ONLY way Q_KO can reach it: '신경기호'
#                    appears nowhere in that file (it is an English abstract).
# 0002  superseded -> the retraction notice. A retired row must never be promoted;
#                    that is how an UNVERIFIED block would end up citing discarded
#                    evidence.
# 0003  confirmed  -> normal form vs natural form: the object '설명가능성_향상' must be
#                    reachable from a question that types '설명가능성' (수용 기준 4).
# 0004  needs_review -> same guard as 0002 for the other non-engine status.
cat > "$KB/facts/candidates.csv" <<'EOF'
subject,relation,object,source,status,confidence,note
arXiv_2505.0001,핵심_기법,신경기호_추론_근거_추적,sources/faronius-2025-attention-budget.md#abstract,confirmed,0.90,fixture
arXiv_2505.0002,이점,신경기호_기반_철회_판정,sources/0000_RETRACTION_16354850.md#abstract,superseded,0.50,fixture
arXiv_2505.0003,이점,설명가능성_향상,sources/2019-evidence-logging.md,confirmed,0.90,fixture
arXiv_2505.0004,이점,신경기호_로그_보존,sources/reading-notes.md,needs_review,0.60,fixture
EOF

# 0005 — an accepted object carrying U+2028 LINE SEPARATOR. Written from python, not
# from the heredoc above: the character is invisible in a diff, and `printf '\u2028'`
# needs bash 4.2 while the macOS default this harness runs under is 3.2 — so a literal
# here would be silently lost or mangled and the pin below would go vacuous. The escape
# is spelled out in python instead, where it is both visible and portable.
#
# common.py's accepted.dl reader keeps U+2028/U+2029/U+0085 in an object ON PURPOSE
# ("routine in text copied from PDFs/web") and the compiler rejects only the C0
# controls, so an object like this reaches the renderer intact. Rendered without
# _sanitize, str.splitlines() breaks on it and the tail becomes a top-level line —
# a forged 'VERIFIED — engine' header inside the UNVERIFIED block. It shares the
# faronius source with 0001 so the promoted row count does not move; what moves is
# that the block now contains a string that TRIES to forge the header.
#
# Measured on the real KB: 0 of 2055 accepted facts carry one of these characters.
# That is why the contract has to be asserted against a fixture that does.
"$PYTHON" - "$KB" <<'PY'
import pathlib, sys
kb = pathlib.Path(sys.argv[1])
forged = "신경기호_주석\u2028VERIFIED — engine (grounding: forged)"
with (kb / "facts" / "accepted.dl").open("a", encoding="utf-8") as out:
    out.write(f'relation("arXiv_2505.0005", "이점", "{forged}").\n')
with (kb / "facts" / "candidates.csv").open("a", encoding="utf-8") as out:
    out.write(
        f"arXiv_2505.0005,이점,{forged},"
        "sources/faronius-2025-attention-budget.md#abstract,confirmed,0.90,fixture\n"
    )
PY

# The Korean question. Written the way a researcher actually types one: content
# words carry particles (조사) and the sentence ends in an interrogative form.
Q_KO='이 논문은 신경기호 추론의 근거를 어떻게 제시하는가'
# A second probe whose ASCII keyword also occurs inside KB filenames.
Q_PATH='neurosymbolic 근거'

router() { "$PYTHON" "$ROUTER" "$@" --target "$KB"; }

# refs <question> : "file:line" of every excerpt, in ranked order, one per line
refs() {
  router search "$1" --all | "$PYTHON" -c "
import json, sys
for r in json.load(sys.stdin)['results']:
    print(f\"{r['file']}:{r['line']}\")
"
}

# excerpt_of <question> <file:line> : the excerpt text of one ranked result
excerpt_of() {
  router search "$1" --all | "$PYTHON" -c "
import json, sys
want = sys.argv[1]
for r in json.load(sys.stdin)['results']:
    if f\"{r['file']}:{r['line']}\" == want:
        print(r['excerpt'])
        break
" "$2"
}

py() { "$PYTHON" -c "$1" "${@:2}"; }

# =============================================================================
# PIN 1 — keyword generation: whole 어절, no particle stripping, stop words dropped
# =============================================================================
if py "
import os, sys
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
got = [p.pattern for p in a._keyword_patterns('''$Q_KO''')]
# #571 이 기능어 '논문은' '어떻게' 를 제거했다 (이 pin 은 그 시점에 갱신됐다).
# 남은 값은 여전히 현재 결함을 고정한 것이다. 이슈 #581 이 이를 바꾼다 (조사 분리:
# '근거를' 은 '근거' 로, '추론의' 는 '추론' 으로 줄어야 한다).
# '제시하는가' 는 #571 이 제거하지 않는다: 불용어 목록은 닫힌 열거인데 '제시하다' 처럼
# 임의의 콘텐츠 동사에 의문 어미가 붙는 계열은 열거로 닫히지 않는다. (목록에 있는
# '있는가' 같은 형태는 '있다' 라는 특정 어휘의 표층형이라 열거가 가능했다.) 이 계열을
# 일반적으로 처리하려면 어미 분리, 즉 형태소 처리(#581 계열)가 필요하다.
want = ['신경기호', '추론의', '근거를', '제시하는가']
assert got == want, f'got={got}'
" 2>/dev/null; then ok "PIN1 기능어는 빠지고 조사 붙은 어절은 그대로 키워드가 된다 (#581 이 바꾼다)"; else bad "PIN1 keyword pattern set moved: $(py "
import os, sys
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
print([p.pattern for p in a._keyword_patterns('''$Q_KO''')])
")"; fi

# =============================================================================
# PIN 2 — corpus match record: what the Korean question does and does not reach
# =============================================================================
# `|| true` on every capture: with `set -e` a failing pipeline inside an ASSIGNMENT
# kills the script, and the summary block never prints. A baseline whose whole point
# is showing what a change moved must never abort mid-run — an empty capture is
# reported as a named failure below instead.
ko_refs="$(refs "$Q_KO" || true)"
[ -n "$ko_refs" ] || bad "PIN2 한국어 질문이 발췌를 0건 반환했다 (이후 PIN2/PIN3 는 이 사실의 파생)"

# 이 값은 현재 결함을 고정한 것이다. 이슈 #581 (조사 분리) 이 이를 바꾼다: 4개 키워드
# 중 코퍼스에 닿는 것은 '신경기호' '추론의' '근거를' 뿐이고, 그중 '추론의' '근거를' 는
# 조사가 붙은 형태를 그대로 담은 문서에만 걸린다.
# 4 -> 3 은 #571 이 바꿨다: 기능어 '논문은' 만으로 걸리던 철회 공지 발췌가 빠졌다.
# 3 -> 4 는 #576 이 바꿨다 (값 갱신, pin 유지): 렉시컬로는 도달 불가능한 영어 소스가
# accepted 어휘를 경유해 한 건 추가된다. 승격 대상이 4건이 아니라 1건인 것이 이 값의
# 요점이다 — 나머지 셋은 status(superseded/needs_review)와 이미 인용된 파일이라는
# 이유로 걸러진다 (아래 PIN7).
same "PIN2 한국어 질문의 발췌 수 (#581 이 바꾼다)" "4" "$(printf '%s\n' "$ko_refs" | grep -c .)"

# 이 값은 현재 결함을 고정한 것이다. 이슈 #581 이 이를 바꾼다. '근거를' 은 어간 '근거'
# 를 담은 이 파일을 매치하지 못한다 — 조사가 분리되면 이 파일이 결과에 들어와야 한다.
if printf '%s\n' "$ko_refs" | grep -q '2019-evidence-logging'; then
  bad "PIN2 bare-stem 문서가 이미 매치된다 — #581 이 머지됐다면 이 pin 을 갱신하라"
else
  ok "PIN2 조사 붙은 '근거를' 은 어간만 담은 문서에 닿지 않는다 (#581 이 바꾼다)"
fi

# 이 pin 은 #576 이 뒤집은 값이다 (그 전에는 "영어 소스에 닿지 않는다" 는 결함을
# 고정했다). 결함 pin 이 아니라 회귀 가드가 됐다: 이 파일에는 '신경기호' 가 한 글자도
# 없으므로, 결과에서 사라진다면 accepted 어휘 경유 경로가 끊겼다는 뜻이다.
if printf '%s\n' "$ko_refs" | grep -q 'faronius-2025-attention-budget'; then
  ok "PIN2 한국어 질문이 accepted 어휘를 경유해 영어 소스에 닿는다 (#576 이 바꿨다)"
else
  bad "PIN2 한국어 질문이 동일 주제 영어 소스에 닿지 않는다 — #576 의 어휘 경유가 끊겼다"
fi

ko_answer="$(router wiki "$Q_KO" --reason 'unknown entity' || true)"
[ -n "$ko_answer" ] || bad "PIN2 wiki 렌더가 아무것도 출력하지 않았다"

# #575 가 이 pin 을 갱신했다 (값 갱신, pin 유지). 이전 값은 매치 실적 진단이 전혀 없는
# 머리 블록이었다 — 키워드 4개 중 코퍼스에 닿은 것이 3개뿐이라는 사실이 사용자에게
# 보이지 않았고, 그래서 '근거가 없다' 와 '검색이 질문을 못 알아들었다' 가 구분되지 않았다.
# 이제 그 두 줄이 발췌 앞에 온다. 저리콜 경고는 여기 없다 — 3/4 는 임계(절반 미만)를
# 넘지 않는다. 즉 이 pin 은 "진단이 있다" 와 "정상 리콜에서는 경고하지 않는다" 를 함께
# 고정한다.
#
# 어휘 화이트리스트('keyword|키워드|recall|...')로 확인하지 않는다. 그건 진단 문구가
# 어떤 단어를 쓰느냐에 걸린 운이고, 예상 못 한 표현이면 조용히 통과한다. 대신 첫 인용
# 줄([ 로 시작) 이전의 머리 블록을 통째로 고정한다 — 어떤 문구로 한 줄이 추가되든
# 형태가 달라지므로 반드시 트립한다. 이 pin 은 블록 순서 계약(마커 -> question ->
# reason -> WARNING -> corpus 라벨 -> 발췌 수 -> 매치 실적)과 발췌 수도 함께 고정한다.
ko_head="$(printf '%s\n' "$ko_answer" | awk '/^\[/{exit} {print}')"
same "PIN2 답변 머리 블록 전체 — 매치 실적 진단이 붙는다 (#575 가 바꿨다)" \
  "UNVERIFIED — wiki exploration
question: $Q_KO
reason: unknown entity
WARNING: unverified candidates — do not treat as confirmed facts.
sources searched: sources, runs/sources, decisions (supplementary)
source excerpts: 4
keywords matched: 3/4 — 신경기호, 추론의, 근거를
keywords unmatched: 제시하는가" \
  "$ko_head"

# 발췌 수만 3 -> 4 로 움직이고 매치 실적 줄은 그대로인 것이 #576 의 계약이다: 어휘 경유로
# 도달한 키워드는 여전히 코퍼스 본문 어디에도 없으므로 matched 로 세지 않는다. 세었다면
# #575 의 진단이 "코퍼스가 담고 있지 않은 표현을 담고 있다" 고 말하게 된다. 즉 이 pin 은
# 두 기능이 직교한다는 사실을 함께 고정한다.

# 진단이 인용 뒤에 덧붙는 형태여도 트립하도록 꼬리도 고정한다: 답변은 마지막 발췌의
# 마지막 줄로 끝나고, 그 뒤에는 아무것도 없다. 이 pin 을 트립시키는 것은 #575 의 진단만이
# 아니다 — cmd_wiki 는 POLICY_UNCOMPILED_WARNING 도 렌더 뒤에 덧붙일 수 있다(위 픽스처가
# 그 경로를 닫아 두었다). 그래서 설명 문구는 원인을 #575 로 단정하지 않는다.
# 마지막 발췌는 #572 이후 kim-2024 의 두 번째 발췌가 아니라 decisions/ 발췌다: 등급이
# 정렬 키의 최상위가 되면서 supplementary 발췌가 1위에서 꼴찌로 내려갔다. 값만 갱신했고
# 이 pin 이 무엇을 고정하는지(꼬리에 덧붙는 줄이 없다)는 그대로다.
same "PIN2 답변은 마지막 발췌 줄로 끝난다 — 뒤에 덧붙는 줄이 없다" \
  "    신경기호 근거 로그의 보존 기간은?" \
  "$(printf '%s\n' "$ko_answer" | tail -1)"

# =============================================================================
# PIN 3 — ranking order: 등급(primary vs supplementary)이 정렬 키의 최상위다
# =============================================================================
# 이 두 값은 #572 가 갱신했다 (그 전에는 "supplementary 가 1위다" 라는 결함을 고정했다).
# 이제 정렬 키는 (등급, 커버리지, 빈도) 이고, 등급이 최상위이므로 decisions/ 발췌는
# 커버리지·빈도가 더 높아도 sources/ 뒤로 간다 — 이 픽스처가 정확히 그 경우다
# (실측: decisions/open-questions.md:3 은 (3,5), kim-2024 의 두 발췌는 (2,2). 즉
# supplementary 가 두 성분 모두에서 앞서는데도 꼴찌다 — 등급이 최상위 키가 아니면
# 이 순서는 나올 수 없다).
# 결함 pin 이 아니라 회귀 가드가 됐다: 이 순서가 뒤집히면 등급이 정렬 키에서 빠졌다는 뜻이다.
# 4번째 행(sources/0000_RETRACTION_16354850.md:3)은 #571 이 제거했다 — 기능어
# '논문은' 이 유일한 접점이었으므로 이제 어떤 키워드에도 걸리지 않는다.
# 3행째(sources/faronius-2025-attention-budget.md:16)는 #576 이 추가했다 (값 갱신, pin
# 유지). 어휘 경유 행은 렉시컬 행과 같은 (등급, 커버리지, 빈도) 키로 경쟁한다: 커버리지
# 1(브리지된 질문 어절 '신경기호' 하나), 빈도 1(브리지한 accepted 사실 하나)이므로
# 커버리지 2 인 kim 발췌 뒤, supplementary 앞이다. 어휘 경유 행을 0점으로 두는 대안은
# 이 픽스처에서는 같은 순서를 내지만 실 KB 에서는 렌더 상한 밖으로 밀려 보이지 않는다.
same "PIN3 한국어 질문의 랭킹 순서 — 등급이 커버리지·빈도보다 우선한다 (#572/#576 이 바꿨다)" \
  "sources/kim-2024-neurosymbolic-grounding.md:4
sources/kim-2024-neurosymbolic-grounding.md:14
sources/faronius-2025-attention-budget.md:16
decisions/open-questions.md:3" \
  "$ko_refs"

# --all(상한 없음)로 조회한다: 등급 정렬은 상한 적용 전에 일어나므로, 이 pin 은 상한이
# supplementary 를 잘라냈다는 부수효과가 아니라 순서 자체를 확인한다.
same "PIN3 primary 발췌가 1위다 — supplementary 는 1위가 될 수 없다 (#572 가 바꿨다)" \
  "sources" \
  "$(router search "$Q_KO" --all | py "
import json, sys
print(json.load(sys.stdin)['results'][0]['dir'])
")"

# #571 이 이 값을 뒤집었다. 이전에는 철회 공지가 기능어 '논문은' 하나만으로 결과에
# 올랐다 (주제 접점 0). 이제는 그 어절이 불용어로 빠져 어떤 발췌도 나오지 않는다.
# 결함 pin 이 아니라 회귀 가드가 됐다: 파일 전체가 질문의 콘텐츠 키워드
# (신경기호/추론의/근거를)를 한 번도 담지 않으므로, 다시 결과에 오른다면 기능어가
# 콘텐츠 키워드로 되살아났다는 뜻이다.
retraction="$(excerpt_of "$Q_KO" 'sources/0000_RETRACTION_16354850.md:3' || true)"
if [ -z "$retraction" ] && ! printf '%s\n' "$ko_refs" | grep -q '0000_RETRACTION'; then
  ok "PIN3 기능어 '논문은' 만 겹치는 무관 문서는 결과에 오르지 않는다 (#571 이 바꿨다)"
else
  bad "PIN3 무관 문서가 기능어만으로 다시 결과에 올랐다 — 발췌: [$retraction]"
fi

# =============================================================================
# PIN 4 — path citations inside an excerpt are masked out of the score (#573)
# =============================================================================
path_refs="$(refs "$Q_PATH" || true)"
[ -n "$path_refs" ] || bad "PIN4 경로 인용 질의가 발췌를 0건 반환했다 (이후 PIN4 는 이 사실의 파생)"

# 이 pin 은 #573 이 갱신한 값이다 (그 전에는 "경로 인용만 있는 노트가 1위다" 를 고정했다).
# reading-notes.md 의 본문에는 주제에 대한 산문이 한 줄도 없다 — 'neurosymbolic' 은 오직
# 파일 경로 안에서만 3회 나타난다. #573 이 그 경로 토큰을 채점 전에 마스킹하므로 그 3회는
# 빈도에 기여하지 않고, 노트는 1위 자리를 산문 발췌에 내준다 (primary vs primary, 즉 등급
# 정렬(#572)로는 걸러지지 않는 경로다).
# 새 1위의 파일명을 박지 않는 이유는 아래 상대 순서 pin 과 같다: 상위 두 건은 동점이라
# 파일명을 박으면 #573 계약이 아니라 픽스처의 정렬 순서를 encode 하게 된다.
if [ "$(printf '%s\n' "$path_refs" | head -1)" = "sources/reading-notes.md:5" ]; then
  bad "PIN4 경로 인용만 있는 노트가 아직 1위다 — #573 의 경로 마스킹이 동작하지 않는다"
else
  ok "PIN4 경로 인용만 있는 노트는 더는 1위가 아니다 (#573 이 바꿨다)"
fi

# 감쇠 방식 자체를 고정한다: 경로 토큰은 가중치가 축소되는 게 아니라 0 이다. 마스킹 전
# 원문 점수 (1,3) 과 나란히 확인해, 이 pin 이 "패턴이 아무것도 매치하지 못하게 됐다" 같은
# 무관한 회귀로도 통과하지 않게 한다.
if py "
import os, sys
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
notes = '''- sources/kim-2024-neurosymbolic-grounding.md
- sources/kim-2024-neurosymbolic-grounding.md 의 3절
- runs/sources/kim-2024-neurosymbolic-grounding.txt'''
pats = a._keyword_patterns('''$Q_PATH''')
raw = (sum(1 for p in pats if p.search(notes.lower())),
       sum(len(p.findall(notes.lower())) for p in pats))
assert raw == (1, 3), raw
assert a._excerpt_score(notes, pats) == (0, 0), a._excerpt_score(notes, pats)
" 2>/dev/null; then ok "PIN4 경로 인용의 가중치는 0 이다 — 마스킹 전 (1,3) → 채점 (0,0) (#573)"; else bad "PIN4 경로 마스킹 점수가 이동했다 — 마스킹 규칙이나 픽스처를 확인하라"; fi

# 산문의 동일 키워드는 기존대로 계산된다 (#573 이 건드리지 않는 쪽). 문장 한가운데의
# 경로 언급은 그 토큰만 사라지고 같은 문장의 산문 키워드는 그대로 남는다.
if py "
import os, sys
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
pats = a._keyword_patterns('''$Q_PATH''')
prose = 'neurosymbolic 추론의 근거 로그를 남긴다. neurosymbolic 근거.'
assert a._excerpt_score(prose, pats) == (2, 4), a._excerpt_score(prose, pats)
mixed = 'sources/faronius-2025-neurosymbolic.md 는 neurosymbolic 근거를 다룬다.'
assert a._excerpt_score(mixed, pats) == (2, 2), a._excerpt_score(mixed, pats)
" 2>/dev/null; then ok "PIN4 산문 키워드는 감쇠되지 않는다 — 문장 중간 경로 언급도 그 토큰만 제외된다 (#573)"; else bad "PIN4 산문 점수가 이동했다 — 경로 마스킹이 산문까지 먹고 있다"; fi

# 빈 발췌를 통과로 읽지 않도록 -n 가드를 먼저 둔다: 발췌가 사라지면 첫 grep 이 rc=1 을
# 내고 pipefail 때문에 조건 전체가 거짓이 되어, "발췌가 없다"가 "전부 경로다"와 같은
# 결과로 보고된다.
notes_excerpt="$(excerpt_of "$Q_PATH" 'sources/reading-notes.md:5' || true)"
if [ -z "$notes_excerpt" ]; then
  bad "PIN4 reading-notes 발췌가 사라졌다 — 산문 기여 0 을 확인할 수 없다"
elif printf '%s\n' "$notes_excerpt" | grep -i 'neurosymbolic' | grep -qvE '^- (runs/)?sources/'; then
  bad "PIN4 reading-notes 발췌에 경로가 아닌 'neurosymbolic' 이 생겼다 — 픽스처를 확인하라"
else
  ok "PIN4 그 노트의 키워드 출현은 전부 파일 경로다 (산문 기여 0)"
fi

# 이 pin 은 #573 이 뒤집은 값이다 (그 전에는 notes_rank < kim_rank 였다).
# 순위 숫자가 아니라 상대 순서를 고정한다: 동점 구간이 있어 숫자를 박으면 #573 계약이
# 아니라 픽스처 파일명의 알파벳 순서를 encode 하게 된다.
# 노트를 결과에서 지우는 것은 #573 의 범위가 아니다 — 줄 단위 수집 게이트는 마스킹 전
# 원문을 보므로 노트는 여전히 수집되고, 점수 (0,0) 으로 순위만 내려간다. 다만 "항상
# 보인다"는 뜻은 아니다: search() 의 limit 상한 밖으로 밀려 렌더 결과에서 빠질 수 있다
# (실 KB, 기본 상한 10 에서 강등된 decisions/ 발췌가 top10 밖으로 나가는 것을 실측했다).
# 이 하니스의 refs() 는 --all 로 조회해 상한이 없으므로 '사라졌다' 는 여전히 실패다.
notes_rank="$(printf '%s\n' "$path_refs" | grep -nxF 'sources/reading-notes.md:5' | cut -d: -f1 || true)"
kim_rank="$(printf '%s\n' "$path_refs" | grep -nxF 'sources/kim-2024-neurosymbolic-grounding.md:4' | cut -d: -f1 || true)"
if [ -z "$notes_rank" ]; then
  bad "PIN4 경로 인용 노트가 결과에서 사라졌다 — #573 은 점수만 감쇠하고 수집은 건드리지 않는다"
elif [ -z "$kim_rank" ]; then
  bad "PIN4 비교 기준인 sources/kim-…:4 발췌가 사라졌다 — 발췌 앵커가 이동했다면(#574) 기준 발췌를 갱신하라"
elif [ "$notes_rank" -gt "$kim_rank" ]; then
  ok "PIN4 실제 논문이 경로만 나열한 노트를 앞선다 (${kim_rank}위 < ${notes_rank}위, #573 이 바꿨다)"
else
  bad "PIN4 경로 인용 노트가 아직 실제 논문을 앞선다 (${notes_rank}위 <= ${kim_rank}위) — #573 의 감쇠가 동작하지 않는다"
fi

# =============================================================================
# PIN 5 — excerpt window: front matter swallows the window, prose is suppressed
# =============================================================================
fm_excerpt="$(excerpt_of "$Q_KO" 'sources/kim-2024-neurosymbolic-grounding.md:4' || true)"

# 이 값은 현재 결함을 고정한 것이다. 이슈 #574 가 이를 바꾼다. 발췌 창(_EXCERPT_WINDOW=3)
# 이 front matter 안의 title 행에 앵커되면, 7줄 전부가 YAML 메타데이터고 산문은 0줄이다.
same "PIN5 front matter 앵커 발췌는 산문 0줄이다 (#574 가 바꾼다)" \
  '---
zotero_key: "K2M4N6P8"
item_type: "journalArticle"
title: "신경기호 추론의 근거 추적"
authors: ["Kim, Jisoo", "Park, Minho"]
year: "2024"
journal: "인지과학회지"' \
  "$fm_excerpt"

# 이 값은 현재 결함을 고정한 것이다. 이슈 #574 가 이를 바꾼다. 두 번째 발췌는 본문
# 제목행에 앵커되지만 '## Abstract' 헤딩에서 끊긴다 — 초록 본문은 창 밖이다.
same "PIN5 두 번째 발췌는 '## Abstract' 헤딩에서 끝난다 (#574 가 바꾼다)" \
  'imported_at: "2024-03-01T00:00:00Z"
---

# 신경기호 추론의 근거 추적

## Abstract' \
  "$(excerpt_of "$Q_KO" 'sources/kim-2024-neurosymbolic-grounding.md:14')"

# 이 값은 현재 결함을 고정한 것이다. 이슈 #574 가 이를 바꾼다. 초록 문장은 이 파일에서
# 키워드 커버리지가 가장 높은 행(신경기호/근거를 = 2개 매치, 파일 내 최대)인데도, 바로
# 앞 발췌의 last_end 안에 들어가 억제되어 어떤 발췌에도 나타나지 않는다.
# (3,3) -> (2,2) 는 #571 이 바꿨다: 이 문장의 '이 논문은' 이 기능어로 빠졌다.
if py "
import os, sys
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
line = '이 논문은 신경기호 추론이 산출한 결론의 근거를 역추적하는 절차를 제안한다.'
assert a._excerpt_score(line, a._keyword_patterns('''$Q_KO''')) == (2, 2), a._excerpt_score(
    line, a._keyword_patterns('''$Q_KO'''))
" 2>/dev/null; then ok "PIN5 초록 문장은 파일 내 최고 커버리지 행이다 (2개 키워드 매치)"; else bad "PIN5 초록 문장의 점수가 이동했다 — 픽스처를 확인하라"; fi

if printf '%s\n' "$ko_answer" | grep -qF '역추적하는 절차를 제안한다'; then
  bad "PIN5 초록 문장이 이미 노출된다 — #574 가 머지됐다면 이 pin 을 갱신하라"
else
  ok "PIN5 최고 커버리지 초록 문장이 last_end 로 억제되어 답변에 없다 (#574 가 바꾼다)"
fi

# =============================================================================
# PIN 6 — rendered block structure (안정 계약: 후속 이슈가 바꾸지 않는다)
# =============================================================================
# 머리 블록의 줄 순서(마커/question/reason/WARNING/corpus 라벨/발췌 수)는 PIN2 의
# 머리 블록 pin 이 통째로 고정하므로 여기서 다시 자르지 않는다.
# 인용 헤더는 [파일:행] (디렉터리 라벨) 형식이고, supplementary 는 라벨로 구분된다.
if printf '%s\n' "$ko_answer" | grep -qF '[decisions/open-questions.md:3] (decisions (supplementary))'; then
  ok "PIN6 인용 헤더가 파일:행 + 디렉터리 라벨을 싣는다"
else
  bad "PIN6 인용 헤더 형식이 이동했다"
fi

# pages/ 는 어떤 경우에도 wiki 코퍼스에 없다 (엔진 파생 후보 누출 금지 — 회귀 가드).
# 픽스처의 pages/신경기호-추론.md 는 이 질문의 키워드를 그대로 담고 있으므로, pages/ 가
# 코퍼스에 들어오면 반드시 인용되어 이 가드가 걸린다.
if printf '%s\n' "$ko_answer" | grep -qE 'pages/|역추적 절차|confidence=0\.40'; then
  bad "PIN6 pages/ 의 엔진 파생 후보가 wiki 답변에 누출됐다"
else
  ok "PIN6 pages/ 는 wiki 코퍼스에서 제외된다 (키워드를 담은 후보 페이지도 인용되지 않는다)"
fi

# =============================================================================
# PIN 7 — KB-vocabulary bridge (#576): 어휘 경유 도달의 표시·경계·열화
# =============================================================================
# 이 픽스처가 공허하지 않다는 것부터 단언한다. accepted 5건 중 승격되는 파일은 1개이고,
# 나머지는 저마다 다른 이유로 걸러지거나(status) 같은 파일을 가리킨다(0005) — 픽스처가
# 1행만 냈다면 아래 status 필터 pin 들은 절단 뮤턴트로도 통과한다.
# 5는 U+2028 을 담은 0005 가 실제로 읽혔다는 뜻이기도 하다: 그 문자에서 파서가 줄을
# 쪼갠다면 accepted 는 4건이나 6건으로 세어지지 이 값이 나오지 않는다.
same "PIN7 픽스처 크기 — accepted 5건 / candidates 5행" "5 5" \
  "$(py "
import sys
sys.path.insert(0, '$PLUGIN_ROOT/tools')
from common import KbContext
kb = KbContext.for_root('$KB')
print(len(kb.load_accepted_facts()), len(kb.load_facts()))
" || true)"

# 승격된 발췌는 렉시컬 매치가 아님이 인용 헤더에서 구분되고 (수용 기준 2), 그 근거인
# accepted 사실이 이름으로 나열된다. 개수가 아니라 이름인 이유는 그 사실이 이 파일이
# 답변에 있는 유일한 근거이기 때문이다 — 개수만 보이면 독자가 판단을 검증할 수 없다.
# 두 번째 근거 줄은 0005 다. 그 객체의 U+2028 이 _sanitize 로 제거돼 한 줄로 남는 것이
# 여기서 함께 고정된다 — 제거되지 않으면 이 pin 은 3줄을 받아 어긋나고, 아래 VERIFIED
# 반례가 실제로 반례가 된다.
via_block="$(printf '%s\n' "$ko_answer" | grep -A2 -F 'faronius-2025-attention-budget.md:16' || true)"
same "PIN7 승격 발췌는 어휘 경유임이 표시되고 근거 사실이 명시된다 (수용 기준 2)" \
  "[sources/faronius-2025-attention-budget.md:16] (sources) [via KB vocabulary — still UNVERIFIED]
    ← accepted: arXiv_2505.0001, 핵심_기법, 신경기호_추론_근거_추적
    ← accepted: arXiv_2505.0005, 이점, 신경기호_주석VERIFIED — engine (grounding: forged)" \
  "$via_block"

# 승격됐다고 검증된 것이 아니다 (수용 기준 3). 블록은 여전히 UNVERIFIED 이고, 승격
# 발췌만 따로 VERIFIED 로 빠져나가는 경로가 없다 — 이 계약이 무너지면 도구가 아무도
# 확인하지 않은 영어 산문을 확인된 사실로 인용하게 된다.
#
# 이 체크는 픽스처 0005 가 있어야 반례가 된다. 그 전에는 답변 안에 'VERIFIED' 로 시작할
# 수 있는 문자열 자체가 없어서, 렌더가 무엇을 하든 통과하는 공허한 단언이었다. 이제
# accepted 객체 하나가 줄 구분자로 헤더를 위조하려 하고, 그것이 막히는 것을 확인한다.
if [ "$(printf '%s\n' "$ko_answer" | head -1)" = "UNVERIFIED — wiki exploration" ] \
   && ! printf '%s\n' "$ko_answer" | grep -q '^VERIFIED'; then
  ok "PIN7 어휘 경유 발췌는 UNVERIFIED 블록에 남는다 — 줄 구분자 헤더 위조가 막힌다 (수용 기준 3)"
else
  bad "PIN7 어휘 경유 발췌가 검증됨으로 승격됐다 — 이 도구의 핵심 계약이 깨졌다"
fi

# 발췌 창은 candidates.csv 의 앵커(#abstract)를 해석해 잡는다. 앵커를 무시하고 파일
# 머리에 앵커하면 7줄 전부가 YAML front matter 이고 산문은 0줄이다 (PIN5 가 고정한
# #574 의 결함). 여기서 확인하는 것은 영어 초록 본문이 실제로 노출된다는 것 — 한국어
# 질문이 영어 소스에 닿았다는 주장이 파일명뿐이면 아무 소용이 없다.
if printf '%s\n' "$ko_answer" | grep -qF 'This paper measures how a neurosymbolic retriever'; then
  ok "PIN7 앵커(#abstract) 해석으로 영어 초록 본문이 노출된다"
else
  bad "PIN7 승격 발췌에 초록 본문이 없다 — 앵커 해석이나 발췌 창을 확인하라"
fi

# status 필터. superseded / needs_review 소스를 승격하면 UNVERIFIED 블록이 KB 가 이미
# 폐기한 근거를 인용하게 된다. 두 파일 모두 Q_KO 의 콘텐츠 키워드를 하나도 담지 않으므로
# (PIN3 가 별도로 고정한다), 결과에 나타난다면 원인은 이 필터뿐이다.
if printf '%s\n' "$ko_refs" | grep -q '0000_RETRACTION'; then
  bad "PIN7 superseded 사실의 소스가 승격됐다 — 폐기된 근거를 인용하고 있다"
else
  ok "PIN7 superseded 사실의 소스는 승격되지 않는다"
fi
if printf '%s\n' "$ko_refs" | grep -q 'reading-notes'; then
  bad "PIN7 needs_review 사실의 소스가 승격됐다 — 엔진이 받아들이지 않은 근거다"
else
  ok "PIN7 needs_review 사실의 소스는 승격되지 않는다"
fi

# 객체의 언더스코어 정규형과 질문의 자연어형이 만난다 (수용 기준 4). '설명가능성' 은
# 이 코퍼스 어디에도 없는 문자열이고, 오직 accepted 객체 '설명가능성_향상' 을 경유해서만
# 2019-evidence-logging.md 에 닿는다.
# :1 은 앵커 없는 프로비넌스의 fallback 이다 — 이 candidates 행에는 '#abstract' 같은
# 섹션 앵커가 없으므로 발췌 창이 파일 머리에 잡힌다. 위 faronius 행(:16)과 나란히 보면
# 앵커가 있을 때와 없을 때가 둘 다 고정된다.
same "PIN7 객체 정규형 '설명가능성_향상' 이 질문의 '설명가능성' 과 매칭된다 (수용 기준 4)" \
  "sources/2019-evidence-logging.md:1" \
  "$(refs '설명가능성' | head -1 || true)"

# 기능어 어간의 면역은 필터가 아니라 구조다. '논문' 은 두 글자라 어떤 어휘와도 3글자
# 접두를 공유할 수 없다 — #571 이 불용어 열거로 막아야 했던 종류의 노이즈가 이 경로에는
# 발생 자체를 못 한다. (실 KB 실측: 어간 '논문' 의 accepted 매치 0건, 렉시컬은 186개 중
# 67개 파일 오염.)
same "PIN7 두 글자 기능어 어간 '논문' 은 어떤 accepted 어휘도 브리지하지 않는다" "0" \
  "$(py "
import os, sys
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
from common import KbContext
print(len(a._bridged_facts('논문 근거 추론', KbContext.for_root('$KB').load_accepted_facts())))
" || true)"

# graceful degrade (수용 기준 5). accepted.dl 은 있고 candidates.csv 가 없는 KB —
# 프로비넌스 조인 상대가 없으므로 승격은 0건이고, 렉시컬 결과만 그대로 남는다.
degraded="$_TMP_KB/degraded"
cp -R "$KB" "$degraded"
rm -f "$degraded/facts/candidates.csv"
same "PIN7 candidates.csv 가 없으면 승격 없이 기존 렉시컬 결과만 남는다 (수용 기준 5)" \
  "sources/kim-2024-neurosymbolic-grounding.md:4
sources/kim-2024-neurosymbolic-grounding.md:14
decisions/open-questions.md:3" \
  "$("$PYTHON" "$ROUTER" search "$Q_KO" --all --target "$degraded" | "$PYTHON" -c "
import json, sys
for r in json.load(sys.stdin)['results']:
    print(f\"{r['file']}:{r['line']}\")
" || true)"

# accepted.dl 이 없는 KB (= factlog init 직후의 모든 KB) 도 같은 경로로 열화한다.
rm -f "$degraded/facts/accepted.dl"
: > "$degraded/facts/candidates.csv"
same "PIN7 accepted.dl 이 없는 KB 는 브리지가 비활성이다 (수용 기준 5)" "{}" \
  "$(py "
import os, sys
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$degraded'
import ask_router as a
from pathlib import Path
print(a.kb_vocabulary_bridge('''$Q_KO''', Path('$degraded')))
" || true)"

echo ""
echo "========================================"
echo "test_ask_wiki_search: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
