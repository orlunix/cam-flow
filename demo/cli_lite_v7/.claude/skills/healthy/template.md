# Healthy Check Template (STRICT)

## STEP 1: RUN SCRIPT (MANDATORY)
You MUST run:
!python scripts/healthy_check.py

Do NOT continue without script output.

## STEP 2: PARSE OUTPUT
Extract:
- status
- reason
- suggestion

## STEP 3: VALIDATE STATE PROGRESS
Check:
- history changed?
- pc changed?
- last_result updated?

If NOT:
- override status = stuck

## STEP 4: OUTPUT DECISION
Return:
- status
- reason
- suggestion

## STEP 5: WRITE BACK
You SHOULD write suggestion into state.suggestion

FAILURE TO USE SCRIPT OUTPUT IS INVALID
