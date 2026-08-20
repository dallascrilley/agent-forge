# U4/U5 skill smoke checklist (manual)

In a fresh agent session with only `skills/agent-forge/` installed:

1. Ask: "make me an agent that checks the HN front page daily and
   summarizes AI posts."
2. The skill interviews one question at a time and writes a spec to disk
   before generating.
3. `validate` passes; bundles generate for the chosen runtime(s).
4. pimono leg: `run.sh --dry-run` prints a pi argv. langgraph leg:
   `compileall` passes and venv setup imports the graph.
5. Total elapsed clone → smoke-checked bundle is recorded for R7 (≤30 min).
6. The spec on disk would survive the session dying mid-way (artifact-first
   ordering was followed).
