# Slash command 사용법

> 🌐 [English](slash-commands.en.md) | **한국어**

> **플러그인과 스킬.** 설치하는 대상은 **플러그인**(factlog-academic)이고, 그 플러그인이
> 설치해 실행하는 프롬프트는 `/factlog` **스킬**입니다. 아래 `/factlog ...` 는 스킬을
> 부르는 slash command 이며, 검토·승인 같은 사람의 게이트는 터미널에서 Python
> CLI(`python3 -m factlog ...`)로 직접 실행합니다. 두 입구 모두 같은 결정론 엔진을
> 호출합니다 — slash command · Python CLI · 검증 엔진, 이 셋이 한 도구입니다.

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
