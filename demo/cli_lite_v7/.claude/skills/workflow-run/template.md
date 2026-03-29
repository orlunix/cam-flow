# Workflow-Run Execution Template (STRICT)

YOU MUST FOLLOW THESE STEPS EXACTLY:

## STEP 1: LOAD CONTEXT
- Read workflow.yaml
- Read .claude/state/workflow.json
- Read .claude/state/memory.json

## STEP 2: LOCATE NODE
- Identify current node from state.pc
- Load node definition from workflow.yaml

## STEP 3: PARSE DSL
Extract:
- do
- with
- next
- transitions

## STEP 4: EXECUTE NODE
- Execute instruction defined in `do`
- Use `with` as input context
- Produce a concrete result (NOT empty)

## STEP 5: COMMIT STATE (MANDATORY)
You MUST update:
- history += current node
- retry += 1
- last_result = execution output
- pc = next node (or resolved transition)

## STEP 6: MEMORY USAGE
- If similar case exists in memory, reuse strategy
- Optionally append new memory entry

## STEP 7: CONTINUE
- Repeat until pc == done

FAILURE TO UPDATE STATE = NODE NOT EXECUTED
