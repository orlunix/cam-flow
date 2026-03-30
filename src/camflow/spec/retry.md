# Retry Policy

## Rules

- Max retry per node: **2**
- Only retryable errors trigger retry (see error-classifier)
- Retry counter resets when moving to a different node
- When retry budget is exhausted → recovery policy takes over

## Retry flow

1. Node returns fail
2. Error classified as retryable
3. If `retry < MAX_RETRY`: increment counter, re-execute same node
4. If `retry >= MAX_RETRY`: delegate to recovery policy
