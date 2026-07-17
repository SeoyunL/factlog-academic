# Slash command 사용법

> 🌐 [English](slash-commands.en.md) | **한국어**

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
