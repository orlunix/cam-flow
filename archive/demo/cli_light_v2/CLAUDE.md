# Workflow Execution Rules

You are executing a multi-step workflow.

## Goals
- Complete all steps
- Do not stop early
- Maintain progress between steps

## Workflow
1. Analyze error
2. Propose fix
3. Validate fix
4. Summarize result

## State handling
- Maintain a JSON state in `.claude/state/workflow.json`
- Always update state after each step

## Rules
- Do NOT terminate unless workflow is complete
- If blocked, attempt alternative approach
- If repeated failure occurs, escalate in reasoning

Always continue execution until done.
