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
# (tests/test_ask_router.sh deliberately switches it ON for one case; this file
# needs the opposite, so it unsets rather than assuming an unset environment.)
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
# PIN 1 — keyword generation: whole 어절, no particle stripping, no stop words
# =============================================================================
if py "
import os, sys
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
got = [p.pattern for p in a._keyword_patterns('''$Q_KO''')]
# 이 값은 현재 결함을 고정한 것이다. 이슈 #581 이 이를 바꾼다 (조사 분리: '근거를'
# 은 '근거' 로 줄어야 한다), 이슈 #571 이 이를 바꾼다 (기능어 제거: '논문은',
# '어떻게', '제시하는가' 는 콘텐츠 키워드가 아니다).
# 현재는 어절이 통째로 패턴이 되고, 탈락하는 것은 1글자 '이' 뿐이다.
want = ['논문은', '신경기호', '추론의', '근거를', '어떻게', '제시하는가']
assert got == want, f'got={got}'
" 2>/dev/null; then ok "PIN1 한국어 질문은 조사 붙은 어절 그대로 키워드가 된다 (#581/#571 이 바꾼다)"; else bad "PIN1 keyword pattern set moved: $(py "
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

# 이 값은 현재 결함을 고정한 것이다. 이슈 #581 (조사 분리) 이 이를 바꾼다: 6개 키워드
# 중 코퍼스에 닿는 것은 '논문은' '신경기호' '추론의' '근거를' 뿐이고, 그중 '추론의'
# '근거를' 는 조사가 붙은 형태를 그대로 담은 문서에만 걸린다.
same "PIN2 한국어 질문의 발췌 수 (#581/#576 이 바꾼다)" "4" "$(printf '%s\n' "$ko_refs" | grep -c .)"

# 이 값은 현재 결함을 고정한 것이다. 이슈 #581 이 이를 바꾼다. '근거를' 은 어간 '근거'
# 를 담은 이 파일을 매치하지 못한다 — 조사가 분리되면 이 파일이 결과에 들어와야 한다.
if printf '%s\n' "$ko_refs" | grep -q '2019-evidence-logging'; then
  bad "PIN2 bare-stem 문서가 이미 매치된다 — #581 이 머지됐다면 이 pin 을 갱신하라"
else
  ok "PIN2 조사 붙은 '근거를' 은 어간만 담은 문서에 닿지 않는다 (#581 이 바꾼다)"
fi

# 이 값은 현재 결함을 고정한 것이다. 이슈 #576 이 이를 바꾼다. 같은 주제를 다루는 영어
# 소스는 한국어 질문에서 어휘적으로 도달 불가능하다 (accepted 한국어 어휘를 경유하면
# 닿아야 한다).
if printf '%s\n' "$ko_refs" | grep -q 'faronius-2025-attention-budget'; then
  bad "PIN2 한국어 질문이 영어 소스에 닿는다 — #576 이 머지됐다면 이 pin 을 갱신하라"
else
  ok "PIN2 한국어 질문은 동일 주제 영어 소스에 닿지 않는다 (#576 이 바꾼다)"
fi

ko_answer="$(router wiki "$Q_KO" --reason 'unknown entity' || true)"
[ -n "$ko_answer" ] || bad "PIN2 wiki 렌더가 아무것도 출력하지 않았다"

# 이 값은 현재 결함을 고정한 것이다. 이슈 #575 가 이를 바꾼다. 렌더된 답변에는 어떤
# 키워드가 몇 건 매치됐는지, 리콜이 낮은지에 대한 진단이 전혀 없다 — 6개 중 2개만
# 실질적으로 기여했다는 사실이 사용자에게 보이지 않는다.
#
# 어휘 화이트리스트('keyword|키워드|recall|...')로 부재를 확인하지 않는다. 그건 진단
# 문구가 어떤 단어를 쓰느냐에 걸린 운이고, 예상 못 한 표현이면 조용히 통과한다. 대신
# 첫 인용 줄([ 로 시작) 이전의 머리 블록을 통째로 고정한다 — 어떤 문구로 한 줄이
# 추가되든 형태가 달라지므로 반드시 트립한다. 이 pin 은 블록 순서 계약(마커 ->
# question -> reason -> WARNING -> corpus 라벨 -> 발췌 수)과 발췌 수도 함께 고정한다.
ko_head="$(printf '%s\n' "$ko_answer" | awk '/^\[/{exit} {print}')"
same "PIN2 답변 머리 블록 전체 — 매치 실적 진단이 없다 (#575 가 바꾼다)" \
  "UNVERIFIED — wiki exploration
question: $Q_KO
reason: unknown entity
WARNING: unverified candidates — do not treat as confirmed facts.
sources searched: sources, runs/sources, decisions (supplementary)
source excerpts: 4" \
  "$ko_head"

# 진단이 인용 뒤에 덧붙는 형태여도 트립하도록 꼬리도 고정한다: 답변은 마지막 발췌의
# 마지막 줄로 끝나고, 그 뒤에는 아무것도 없다.
same "PIN2 답변은 마지막 발췌 줄로 끝난다 (#575 가 바꾼다)" \
  "    이 논문은 더 이상 인용해서는 안 된다." \
  "$(printf '%s\n' "$ko_answer" | tail -1)"

# =============================================================================
# PIN 3 — ranking order: 등급(primary vs supplementary)이 정렬 키에 없다
# =============================================================================
# 이 값은 현재 결함을 고정한 것이다. 이슈 #572 가 이를 바꾼다. 점수는 (키워드 커버리지,
# 총 빈도) 뿐이고 디렉터리 등급은 정렬에 전혀 들어가지 않는다. 그래서 사람이 적은
# 리뷰 노트(decisions/, supplementary)가 원자료(sources/)를 앞선다.
same "PIN3 한국어 질문의 랭킹 순서 (#572 가 바꾼다)" \
  "decisions/open-questions.md:3
sources/kim-2024-neurosymbolic-grounding.md:4
sources/kim-2024-neurosymbolic-grounding.md:14
sources/0000_RETRACTION_16354850.md:3" \
  "$ko_refs"

same "PIN3 supplementary 발췌가 1위다 (#572 가 바꾼다)" \
  "decisions (supplementary)" \
  "$(router search "$Q_KO" --all | py "
import json, sys
print(json.load(sys.stdin)['results'][0]['dir'])
")"

# 이 값은 현재 결함을 고정한 것이다. 이슈 #571 이 이를 바꾼다. 철회 공지는 질문과
# 주제가 전혀 겹치지 않는데도, 기능어 '논문은' 하나만으로 결과에 들어온다.
retraction="$(excerpt_of "$Q_KO" 'sources/0000_RETRACTION_16354850.md:3' || true)"
if [ -n "$retraction" ] && ! printf '%s' "$retraction" | grep -qE '신경기호|추론|근거'; then
  ok "PIN3 무관 문서가 기능어 '논문은' 단독으로 결과에 오른다 (#571 이 바꾼다)"
else
  bad "PIN3 기능어 케이스가 이동했다 — 발췌: [$retraction]"
fi

# =============================================================================
# PIN 4 — path citations inside an excerpt feed the frequency score
# =============================================================================
path_refs="$(refs "$Q_PATH" || true)"
[ -n "$path_refs" ] || bad "PIN4 경로 인용 질의가 발췌를 0건 반환했다 (이후 PIN4 는 이 사실의 파생)"

# 이 값은 현재 결함을 고정한 것이다. 이슈 #573 이 이를 바꾼다. reading-notes.md 의
# 본문에는 주제에 대한 산문이 한 줄도 없다 — 'neurosymbolic' 은 오직 파일 경로 안에서만
# 3회 나타나는데, 그 3회가 빈도 점수가 되어 실제 논문을 앞지른다 (primary vs primary,
# 즉 등급 정렬(#572)로는 걸러지지 않는 경로다).
same "PIN4 경로 인용만 있는 노트가 1위다 (#573 이 바꾼다)" \
  "sources/reading-notes.md:5" \
  "$(printf '%s\n' "$path_refs" | head -1)"

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

# 이 값은 현재 결함을 고정한 것이다. 이슈 #573 이 이를 바꾼다.
# 순위 숫자가 아니라 상대 순서를 고정한다: 4~7위는 전부 (1,1) 동점이라 숫자를 박으면
# #573 계약이 아니라 픽스처 파일명의 알파벳 순서를 encode 하게 된다.
notes_rank="$(printf '%s\n' "$path_refs" | grep -nxF 'sources/reading-notes.md:5' | cut -d: -f1 || true)"
kim_rank="$(printf '%s\n' "$path_refs" | grep -nxF 'sources/kim-2024-neurosymbolic-grounding.md:4' | cut -d: -f1 || true)"
if [ -z "$notes_rank" ]; then
  bad "PIN4 경로 인용 노트가 결과에서 사라졌다 — #573 이 머지됐다면 이 pin 을 갱신하라"
elif [ -z "$kim_rank" ]; then
  bad "PIN4 비교 기준인 sources/kim-…:4 발췌가 사라졌다 — 발췌 앵커가 이동했다면(#574) 기준 발췌를 갱신하라"
elif [ "$notes_rank" -lt "$kim_rank" ]; then
  ok "PIN4 경로만 나열한 노트가 실제 논문을 앞선다 (${notes_rank}위 < ${kim_rank}위, #573 이 바꾼다)"
else
  bad "PIN4 경로 인용 노트가 더는 실제 논문을 앞서지 않는다 — #573 이 머지됐다면 이 pin 을 갱신하라"
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

# 이 값은 현재 결함을 고정한 것이다. 이슈 #574 가 이를 바꾼다. 초록 문장은 코퍼스에서
# 키워드 커버리지가 가장 높은 단 하나의 행(논문은/신경기호/근거를 = 3개 매치)인데도,
# 바로 앞 발췌의 last_end 안에 들어가 억제되어 어떤 발췌에도 나타나지 않는다.
if py "
import os, sys
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
line = '이 논문은 신경기호 추론이 산출한 결론의 근거를 역추적하는 절차를 제안한다.'
assert a._excerpt_score(line, a._keyword_patterns('''$Q_KO''')) == (3, 3), a._excerpt_score(
    line, a._keyword_patterns('''$Q_KO'''))
" 2>/dev/null; then ok "PIN5 초록 문장은 코퍼스 최고 커버리지 행이다 (3개 키워드 매치)"; else bad "PIN5 초록 문장의 점수가 이동했다 — 픽스처를 확인하라"; fi

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

echo ""
echo "========================================"
echo "test_ask_wiki_search: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
