---
name: healthy
description: Monitor workflow health
---

## Step 1: Run health check script

!python scripts/healthy_check.py

## Step 2: Analyze result

Based on script output:
- detect loop
- detect stuck
- detect retry issues

## Step 3: Output

Return:
- status
- reason
- suggestion
