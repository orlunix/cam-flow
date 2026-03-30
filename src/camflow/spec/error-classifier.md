# Error Classification

## Error codes

| Code | Retryable | Condition |
|------|-----------|-----------|
| `PARSE_ERROR` | yes | Agent output could not be parsed as JSON |
| `NODE_FAIL` | yes | Node returned `status: fail` |

## Classification logic

1. If parse failed → `PARSE_ERROR`
2. If parsed but `status = fail` → `NODE_FAIL`
3. Otherwise → no error (null)
