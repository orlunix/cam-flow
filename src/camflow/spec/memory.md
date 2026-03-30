# Memory Policy

Memory is a small, mutable working store for cross-node context.

## Structure

```json
{
  "summaries": ["step 1 result", "step 2 result"],
  "lessons": ["learned X from failure"]
}
```

## Rules

- Memory is NOT: log store, raw output store, full history
- Write sources: node output (`state_updates`), DSL `set`, runtime internal
- Small and essential only — no large payloads
- Overwrites allowed
- Summaries: append after each node. Keep last N (default 3) for prompt context
- Lessons: append on failure recovery

## Memory vs Trace vs Artifact

| | Memory | Trace | Artifact |
|---|--------|-------|----------|
| Purpose | Current working context | Execution history | Raw outputs |
| Size | Small | Medium | Large |
| Mutability | Mutable | Append-only | Immutable |
