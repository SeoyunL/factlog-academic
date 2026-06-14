# Text-to-Fact Extraction Prompt

You are a fact extraction assistant. Given the source text below, extract
atomic, verifiable facts in the form (subject, relation, object).

## Source text

{source_text}

## Output format

Return one fact per line as CSV with columns:
subject,relation,object,source,status,confidence,note
