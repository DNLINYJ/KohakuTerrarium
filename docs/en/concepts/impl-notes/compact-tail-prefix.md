# Reuse accepted context for post-compaction tail mutations

## Scope

This change was developed on main `2c0d96f3` and rebased onto main `b76cab27`,
not the frozen compaction migration branch.
It addresses editing or regenerating a retained input created after an already
accepted summary. It adds no storage format, checkpoint replacement rules,
compaction writer, delivery protocol, or runtime request-comparison guard.

The motivating case has `compact_source_path=None`: ordinary feedback can produce
this metadata, and ordinary cold restore can legitimately retain that summary.
The existing targeted reconstruction rejects it and expands raw history. The
regression fixture reproduces this through real subagent-event acceptance and
compaction: completion/checkpoint event 55 precedes target event 63, yet the old
edit and regenerate paths expand five request messages to eleven.

## Selection boundary

Canonical target resolution remains unchanged. Before falling back to snapshot/raw
reconstruction, `agent_raw_history` attempts a detached slice of the current accepted
conversation through that target. It uses normal branch routing, target uniqueness
and clear-cutoff handling. It does not verify ancestor contents or reconstruct old
tool calls to establish summary provenance.

A retained user can have been consumed by compaction and reattached afterward, so
position after a summary alone is insufficient. Existing `compact_complete` records
supply the time ordering: each retained summary must match one completion by its
round/text and existing summary metadata, and completion must precede target creation.
These records are already loaded by target resolution. No new fields are persisted;
unknown provenance remains explicitly unknown. Missing or ambiguous chronology uses
the existing reconstruction path, not unconditional summary acceptance.

A later ordinary checkpoint watermark does not invalidate this chronology. The
summary may predate the target while the latest ordinary save follows it. Conversely,
a watermark is not a claim that all earlier events were consumed by the summary.

The native candidate is detached before truncation. Successful selection preserves
accepted message metadata, compact state and cutoff. A second reload is idempotent.
Implicit regeneration changes the user's branch coordinates but can retain its old
in-memory event ID; that optional ID is not an additional veto after canonical
identity and unique coordinates have resolved the target.

## Limits

This is a current-conversation shortcut. A non-current target whose representation
cannot be safely selected still follows existing snapshot/raw handling. The change
does not generally repair unknown-provenance checkpoints, missing historical inputs,
summary ranking, system-less compaction, or shared checkpoint retention.

Preparation failures do not mutate the current conversation. This change does not
make the entire edit operation transactional: existing post-install persistence and
failure behavior remain unchanged. No checkpoint-format or ownership fix from the
later planned work is included.

## Tests and review corrections

The existing integration history workflow retains its known-source and pre-capture
edit cases, and adds independent unknown-source edit, regenerate and internal-edit
cases. Only the compact-tail subjourney varies: the common history setup and media
journeys run once, with isolated directories and patches for each tail variant.
The positive cases switch branches, normally stop/close, reopen, and issue a
new request. Both shared prefix and branch-specific user suffix are checked. Editing
consumed input still excludes the old summary. Settled tool history is retained.

ScriptedLLM records the actual normalized message dictionaries delivered to the LLM
interface without network calls. Equality is asserted there, not at the HTTP wire or
provider-specific serialization layer. No cache hit is asserted. Repeated implicit
regeneration is also tested with replay-generated metadata explicitly applied to the
live user; that setup is not an end-to-end test of the hydration path itself.

The previous switch-back replay assertion aggregated speculative work for all
checkpoint candidates. Two losing candidates replayed earlier IDs even though the
correct checkpoint was selected and the provider prefix was unchanged. The test now
checks the selected representation and request. The single-checkpoint cold-restore
watermark and no-counter-read checks remain.

Historical validation before rebasing and localizing the test variants, on
Windows/Python 3.14.2 (not a claim about the final rebased tree):

- Unit + integration: **18727 passed, 40 skipped** with two workers.
- End-to-end: **84 passed** with the normal automatic worker setting.
- Frontend: **277 files, 2557 tests passed**, normal parallelism, temporary local
  dependency-path allowance and 15-second test timeout. Default 5-second full-suite
  execution timed out on panel registration; that file passed alone at the default.
- Black checks over `src/` and `tests/`, Ruff over those trees, frontend Prettier, and
  Git whitespace checks passed.

The first auto-worker unit/integration collection exhausted memory in the existing
real-config pollution guard. An earlier two-worker full run also hit a Windows MCP
response-file PermissionError; the final complete run passed without source changes
to that module. Neither earlier failure is counted as a pass. Investigating parallel
collection memory is a separate user-requested follow-up, not part of this patch.

After rebasing onto `b76cab27` and localizing the variants:

- Unit + integration: **18736 passed, 40 skipped, 1 failed** in 1005.38 seconds.
  The failure was the same Windows response-file PermissionError in
  `tests/unit/mcp_server/test_management.py::test_restart_ignores_old_request_and_response`;
  this full run is not green.
- A second combined run again reported **18736 passed, 40 skipped, 1 failed**,
  this time because the MCP lifecycle integration observed `local_ready=False`
  while its subprocess was still starting. All unit tests passed in that run.
  The subsequent separate complete integration run passed: **179 passed** in
  182.37 seconds. These reruns do not retroactively make either combined run green.
- The complete E2E tier, that MCP module, raw-history units and the consolidated
  history journey subsequently passed together: **130 passed** in 64.92 seconds.
- Black, Ruff, frontend Prettier and the frontend production build passed.
- The independent review's additional public-entry and settled-tool probes were
  reported as passing against the pre-rebase tree; those results are not counted as
  reruns on this revision. The PR links its own fork CI result separately.

Local commands and logs are retained in main-checkout
`temp/compact-tail-prefix-pr1/`. No external model requests or dependency installation
were used. The frontend reused installed dependencies through a local junction; its
project configuration was not changed.
