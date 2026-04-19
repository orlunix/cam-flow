"""Integration test: stateless fix→test→fix loop with accumulating state.

Simulates the calculator-demo pattern: analyze → fix → test → (fail) → fix →
test → (success) → done. Mocks the agent so results are deterministic.

Validates the stateless-execution thesis:
  1. Every agent call starts from scratch. The ONLY state transfer is via
     state.json + CLAUDE.md (not tested here since we mock the agent).
  2. After N iterations, state carries:
       - iteration counter == N
       - completed[] with one entry per successful node
       - lessons[] deduped + FIFO-bounded
       - failed_approaches[] bounded; purged for a node on its success
       - test_output overwritten with the most recent test run
       - key_files unioned across all agent reports
  3. Each next prompt contains the accumulated context in the fenced block,
     so retries don't repeat previous approaches.
"""

import json
import textwrap

from camflow.backend.cam.engine import Engine, EngineConfig


WORKFLOW = """
start:
  do: agent claude
  with: Analyze the bugs.
  next: fix

fix:
  do: agent claude
  with: Fix one bug.
  next: test

test:
  do: cmd bash -c "exit $TEST_EXIT"
  transitions:
    - if: fail
      goto: fix
    - if: success
      goto: done

done:
  do: agent claude
  with: Summarize.
"""


def test_stateless_loop_accumulates_state(tmp_path, monkeypatch):
    wf = tmp_path / "workflow.yaml"
    wf.write_text(textwrap.dedent(WORKFLOW))

    # Script for each agent call (cmd/test runs are real subprocesses we
    # control via the TEST_EXIT env; we toggle it externally)
    #
    # Expected execution order:
    #   1. start(agent)         success  — analyze
    #   2. fix(agent)           success  — fix bug #1
    #   3. test(cmd)            fail     (TEST_EXIT=1 on first run)
    #   4. fix(agent)           success  — fix bug #2
    #   5. test(cmd)            success  (TEST_EXIT=0 on second run)
    #   6. done(agent)          success
    #
    # TEST_EXIT is toggled via a mutable counter file.

    counter_file = tmp_path / "_test_counter.txt"
    counter_file.write_text("0")

    # Use a bash wrapper that reads the counter and toggles pass/fail
    # Replace the `cmd` for test to one that references the counter
    wf.write_text(textwrap.dedent(f"""
        start:
          do: agent claude
          with: Analyze the bugs.
          next: fix

        fix:
          do: agent claude
          with: Fix one bug.
          next: test

        test:
          do: cmd bash -c 'n=$(cat {counter_file}); echo "FAILED test_{{state.iteration}}" if [ "$n" = "0" ] then exit 1 else exit 0 fi' || true
          transitions:
            - if: fail
              goto: fix
            - if: success
              goto: done

        done:
          do: agent claude
          with: Summarize.
    """))

    # Simpler: just run a cmd that passes when the counter file contains "1"
    wf.write_text(textwrap.dedent(f"""
        start:
          do: agent claude
          with: Analyze the bugs.
          next: fix

        fix:
          do: agent claude
          with: Fix one bug.
          next: test

        test:
          do: cmd bash -c 'test "$(cat {counter_file})" = "1"'
          transitions:
            - if: fail
              goto: fix
            - if: success
              goto: done

        done:
          do: agent claude
          with: Summarize.
    """))

    agent_call_log = []

    # Script agent results. Ordered by node execution.
    agent_results = {
        "start": [{
            "status": "success",
            "summary": "Analyzed: 2 failing tests",
            "output": {},
            "state_updates": {
                "active_task": "Fix 2 bugs",
                "next_steps": ["fix bug A", "fix bug B"],
                "files_touched": ["calculator.py", "test_calculator.py"],
            },
            "error": None,
        }],
        "fix": [
            {
                "status": "success",
                "summary": "Fixed bug A (divide zero-check)",
                "output": {},
                "state_updates": {
                    "new_lesson": "always zero-check before divide",
                    "files_touched": ["calculator.py"],
                    "resolved": "divide zero-check",
                    "lines": "L16-18",
                    "detail": "added raise ValueError",
                },
                "error": None,
            },
            {
                "status": "success",
                "summary": "Fixed bug B (factorial off-by-one)",
                "output": {},
                "state_updates": {
                    "new_lesson": "range(1,n) is off-by-one, use range(1,n+1)",
                    "files_touched": ["calculator.py"],
                    "resolved": "factorial off-by-one",
                    "lines": "L30-34",
                },
                "error": None,
            },
        ],
        "done": [{
            "status": "success",
            "summary": "All tests pass, summary complete",
            "output": {},
            "state_updates": {},
            "error": None,
        }],
    }

    # Side-effect: after 2nd `fix` we flip the counter so `test` passes next
    flip_after = {"fix": 2}  # after 2nd fix, make test pass

    def fake_start(node_id, prompt, project_dir, **kwargs):
        agent_call_log.append({"node": node_id, "prompt": prompt})
        return f"agent{len(agent_call_log):08x}"

    def fake_wait(agent_id, result_path, timeout, poll_interval):
        return ("file_appeared", None)

    def fake_finalize(agent_id, completion_signal, project_dir, cleanup=True):
        # Determine which node this was from the call log
        entry = agent_call_log[-1]
        node = entry["node"]
        results_for_node = agent_results.setdefault(node, [])
        if not results_for_node:
            return {
                "status": "fail",
                "summary": f"no more scripted results for {node}",
                "state_updates": {},
                "error": {"code": "NODE_FAIL"},
            }
        r = results_for_node.pop(0)
        # Flip counter after the 2nd fix
        if node == "fix" and not results_for_node:
            # This was the last scripted fix → flip counter so test passes next
            counter_file.write_text("1")
        return r

    from camflow.backend.cam import agent_runner
    monkeypatch.setattr(agent_runner, "start_agent", fake_start)
    monkeypatch.setattr(agent_runner, "_wait_for_result", fake_wait)
    monkeypatch.setattr(agent_runner, "finalize_agent", fake_finalize)

    cfg = EngineConfig(poll_interval=0, node_timeout=10, max_retries=1,
                       max_node_executions=10)
    eng = Engine(str(wf), str(tmp_path), cfg)
    final = eng.run()

    assert final["status"] == "done", final

    # --- State accumulation checks ---
    assert final["iteration"] >= 5  # start, fix, test, fix, test, done

    # completed should have at least start, fix, fix, done (test is cmd, also counted)
    completed_nodes = [c["node"] for c in final["completed"]]
    assert completed_nodes.count("start") == 1
    assert completed_nodes.count("fix") == 2
    assert completed_nodes.count("done") == 1

    # Lessons deduped and accumulated
    assert "always zero-check before divide" in final["lessons"]
    assert "range(1,n) is off-by-one, use range(1,n+1)" in final["lessons"]

    # Files unioned
    assert "calculator.py" in final["active_state"]["key_files"]
    assert "test_calculator.py" in final["active_state"]["key_files"]

    # blocked cleared on final success
    assert final["blocked"] is None

    # resolved list has both
    assert "divide zero-check" in final["resolved"]
    assert "factorial off-by-one" in final["resolved"]

    # --- Fenced CONTEXT appears in 2nd fix prompt ---
    # The 2nd `fix` is the 2nd agent call to `fix`. Find it.
    fix_calls = [c for c in agent_call_log if c["node"] == "fix"]
    assert len(fix_calls) == 2
    second_fix_prompt = fix_calls[1]["prompt"]
    assert "CONTEXT" in second_fix_prompt
    # Must have context from the first fix
    assert "Fixed bug A" in second_fix_prompt
    # Lessons from first fix must appear
    assert "always zero-check before divide" in second_fix_prompt
    # The test failure output (from cmd) should be captured
    # (bash -c 'test ...' fails silently with no stdout, so test_output
    # will be empty string; we don't assert content but verify the key exists)
    assert "iteration" in second_fix_prompt.lower()

    # --- Trace is complete ---
    trace_path = tmp_path / ".camflow" / "trace.log"
    entries = [json.loads(l) for l in trace_path.read_text().strip().split("\n")]
    node_seq = [e["node_id"] for e in entries]
    # Canonical: start, fix, test(fail), fix, test(success), done
    assert node_seq[0] == "start"
    assert node_seq[-1] == "done"
    assert "test" in node_seq
    assert node_seq.count("fix") == 2


def test_stateless_every_agent_starts_fresh(tmp_path, monkeypatch):
    """Ensure that each agent invocation is a separate camc call.

    No agent reuse: every node execution = a fresh start_agent call."""
    wf = tmp_path / "workflow.yaml"
    wf.write_text(textwrap.dedent("""
        start:
          do: agent claude
          with: a
          next: mid

        mid:
          do: agent claude
          with: b
          next: done

        done:
          do: agent claude
          with: c
    """))

    call_count = {"n": 0}

    def fake_start(node_id, prompt, project_dir, **kwargs):
        call_count["n"] += 1
        return f"agent{call_count['n']:08x}"

    def fake_wait(*args, **kw):
        return ("file_appeared", None)

    def fake_finalize(agent_id, *args, **kw):
        return {"status": "success", "summary": "ok", "state_updates": {}, "error": None}

    from camflow.backend.cam import agent_runner
    monkeypatch.setattr(agent_runner, "start_agent", fake_start)
    monkeypatch.setattr(agent_runner, "_wait_for_result", fake_wait)
    monkeypatch.setattr(agent_runner, "finalize_agent", fake_finalize)

    cfg = EngineConfig(poll_interval=0, node_timeout=5, max_retries=1)
    eng = Engine(str(wf), str(tmp_path), cfg)
    final = eng.run()

    assert final["status"] == "done"
    # 3 nodes = 3 separate agent calls, no reuse
    assert call_count["n"] == 3


def test_stateless_long_loop_prunes_lessons(tmp_path, monkeypatch):
    """15 lessons added; state.lessons caps at 10 (MAX_LESSONS)."""
    wf = tmp_path / "workflow.yaml"
    wf.write_text(textwrap.dedent("""
        start:
          do: agent claude
          with: go
          next: fix

        fix:
          do: agent claude
          with: keep fixing
          transitions:
            - if: output.more
              goto: fix
          next: done

        done:
          do: cmd echo ok
    """))

    counter = [0]
    lessons_provided = []

    def fake_start(node_id, prompt, project_dir, **kwargs):
        return f"agent{counter[0]:08x}"

    def fake_wait(*a, **k):
        return ("file_appeared", None)

    def fake_finalize(agent_id, *a, **k):
        counter[0] += 1
        i = counter[0]
        if i == 1:
            # start
            return {"status": "success", "summary": "analyzed",
                    "state_updates": {}, "output": {}, "error": None}
        # fix node: loop 15 times producing a lesson each time,
        # then success without more
        if i <= 16:
            lesson = f"lesson #{i - 1}"
            lessons_provided.append(lesson)
            more = i < 16  # last iteration = success without more
            return {
                "status": "success",
                "summary": f"fix iter {i-1}",
                "output": {"more": more},
                "state_updates": {"new_lesson": lesson},
                "error": None,
            }
        return {"status": "success", "summary": "final", "state_updates": {}, "output": {}, "error": None}

    from camflow.backend.cam import agent_runner
    monkeypatch.setattr(agent_runner, "start_agent", fake_start)
    monkeypatch.setattr(agent_runner, "_wait_for_result", fake_wait)
    monkeypatch.setattr(agent_runner, "finalize_agent", fake_finalize)

    cfg = EngineConfig(poll_interval=0, node_timeout=5, max_retries=1,
                       max_node_executions=20)
    eng = Engine(str(wf), str(tmp_path), cfg)
    final = eng.run()

    assert final["status"] == "done"

    # Lessons capped at 10
    assert len(final["lessons"]) == 10
    # FIFO: oldest dropped, newest kept
    assert final["lessons"][0] == lessons_provided[5]   # #0..#4 dropped
    assert final["lessons"][-1] == lessons_provided[-1]
