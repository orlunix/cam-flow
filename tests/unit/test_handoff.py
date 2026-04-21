"""Tests for the node-result.json `handoff` field.

Handoff is a detailed paragraph an agent writes for the NEXT node's
agent, injected at the top of the downstream CONTEXT fence. Only the
most recent handoff is retained (state.last_handoff, overwritten per
node), so it always describes the immediately preceding hop.
"""

from __future__ import annotations

from camflow.backend.cam.prompt_builder import RESULT_CONTRACT, build_prompt
from camflow.engine.state_enricher import enrich_state, init_structured_fields


def _fresh_state():
    return init_structured_fields({"pc": "n", "status": "running"})


# ---- state_enricher ------------------------------------------------------


class TestEnricherCapturesHandoff:
    def test_handoff_saved_to_state(self):
        state = _fresh_state()
        result = {
            "status": "success",
            "summary": "one-liner",
            "handoff": (
                "Patched rtl/foo.v:123 to disable the flag that was "
                "causing the -7.8% regression. Next node: rebuild and "
                "validate IPC."
            ),
            "state_updates": {},
            "error": None,
        }
        enrich_state(state, "n", result)
        assert state["last_handoff"].startswith("Patched rtl/foo.v:123")

    def test_missing_handoff_is_graceful(self):
        """Old agents without a handoff field still work; last_handoff
        is simply not set (or left unchanged from a prior hop)."""
        state = _fresh_state()
        enrich_state(state, "n", {
            "status": "success", "summary": "ok",
            "state_updates": {}, "error": None,
        })
        assert "last_handoff" not in state or not state.get("last_handoff")

    def test_blank_handoff_does_not_clobber_previous(self):
        """A node that writes '' or whitespace for handoff should NOT
        erase a meaningful handoff from an earlier node — the next
        agent's briefing survives."""
        state = _fresh_state()
        state["last_handoff"] = "earlier handoff from node A"
        enrich_state(state, "b", {
            "status": "success", "summary": "ok",
            "handoff": "   ",
            "state_updates": {}, "error": None,
        })
        assert state["last_handoff"] == "earlier handoff from node A"

    def test_new_handoff_overwrites_previous(self):
        """Spec: only keep the most recent handoff."""
        state = _fresh_state()
        state["last_handoff"] = "from A"
        enrich_state(state, "b", {
            "status": "success", "summary": "ok",
            "handoff": "from B",
            "state_updates": {}, "error": None,
        })
        assert state["last_handoff"] == "from B"

    def test_failure_also_emits_handoff(self):
        """A failing agent's handoff ('what I tried, why it failed') is
        the most valuable — must survive enrichment on failure too."""
        state = _fresh_state()
        result = {
            "status": "fail", "summary": "broke",
            "handoff": "Tried approach X, RTL at ifu.v:847 rejects it because...",
            "state_updates": {},
            "error": "boom",
        }
        enrich_state(state, "n", result)
        assert "ifu.v:847" in state["last_handoff"]


# ---- prompt_builder CONTEXT block ---------------------------------------


class TestHandoffRendersInContext:
    def test_handoff_visible_in_next_prompt(self):
        state = _fresh_state()
        state["last_handoff"] = (
            "Reverted config_ifu_btb_en to 1'b0 in rtl/NV_GR_FECS_RV_ifu_va1s32.v:11024. "
            "Next step: add a 2-bit BHT at the same BTFN decision site."
        )
        node = {"do": "agent placeholder", "with": "do the thing"}
        p = build_prompt("n", node, state)
        assert "Handoff from previous node:" in p
        assert "config_ifu_btb_en" in p
        assert "2-bit BHT" in p

    def test_blank_handoff_is_skipped(self):
        state = _fresh_state()
        state["last_handoff"] = ""
        node = {"do": "agent placeholder", "with": "do the thing"}
        p = build_prompt("n", node, state)
        assert "Handoff from previous node" not in p

    def test_missing_handoff_is_skipped(self):
        state = _fresh_state()
        # last_handoff not set at all
        node = {"do": "agent placeholder", "with": "do the thing"}
        p = build_prompt("n", node, state)
        assert "Handoff from previous node" not in p


# ---- output contract documents handoff ----------------------------------


class TestResultContractMentionsHandoff:
    def test_contract_explains_handoff_field(self):
        assert '"handoff"' in RESULT_CONTRACT
        # Agents should understand it's for the *next* agent, with
        # specifics; vague contracts produce vague handoffs.
        low = RESULT_CONTRACT.lower()
        assert "next agent" in low
        assert "file paths" in low or "line numbers" in low
