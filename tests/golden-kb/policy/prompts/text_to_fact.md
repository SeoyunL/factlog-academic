# Text-to-Fact Extraction Prompt

You are a fact extraction assistant. Given the source text below, extract
atomic, verifiable facts in the form (subject, relation, object).

## Source text

{source_text}

## Output format

Return one fact per line as CSV with columns:
subject,relation,object,source,status,confidence,note

For typed literal objects, you may use compact compound terms when they preserve
structure better than prose strings: date(2030,1), date(2030,1,15),
number(2.5), ordinal(3), amount(100,"억"). Keep entity objects as plain names.
