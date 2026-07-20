# 관계 별칭 (`policy/relation-aliases.md`)

> 🌐 [English](relation-aliases.en.md) | **한국어**

**표면형** 관계명을 **정규형(canonical)** 에 매핑해, `게재연도` 와 `발행년도` 로 쓰인 사실을
하나의 관계 `published_year` 로 다룹니다. 이것이 없으면 엔진은 둘을 무관한 관계로 보고, 한쪽을
질의하면 다른 쪽에 저장된 사실을 놓칩니다(#213).

```
# policy/relation-aliases.md
- `게재연도` -> `published_year`
- `publication_year` -> `published_year`
```

한 줄에 매핑 하나, `raw` -> `canonical` 입니다. 여기서는 **두 이름 모두 백틱이 필수**입니다 —
다른 정책 파일에서는 선택이지만 이 파일만 다릅니다. 화살표는 있는데 백틱이 없는 줄은 사용자가
만들려다 잘못 쓴 매핑이므로, 조용히 넘기지 않고 stderr 에 malformed 로 보고하고 건너뜁니다.
정규형은 다른 정책 파일에서 선언하는 이름이며, 별칭은 그 적용 전에 정규형으로 접힙니다.

`factlog init` 이 주석 처리된 예시와 함께 이 파일을 만들어 둡니다.
