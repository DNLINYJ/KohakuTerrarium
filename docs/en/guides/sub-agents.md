---
title: Sub-agents
summary: Configure bounded inspection helpers, service presets, custom sub-agents, and budgets.
tags:
  - guides
  - sub-agent
  - budget
---

# Sub-agents

Sub-agents are private vertical delegation inside a creature. `explore` and
`critic` start with their own prompt and the supplied task, not the parent's
conversation. The default-on `fork` helper instead inherits a detached snapshot
of the parent's effective context. Lightweight helpers inspect; graph creatures
own substantial delegated work. The parent retains planning and decisions.

## Builtin task helpers

```yaml
subagents:
  - {name: explore, type: builtin}
  - {name: critic, type: builtin}
```

- **explore:** answer a bounded workspace question. Supply the question, search
  scope, and existing findings. Returns an answer, file evidence, and coverage
  limits. Not implementation, planning, or an unnecessary extra hop for a known file.
- **critic:** independently review a specified change, artifact, or proposal.
  Supply the target, requirements, scope, and a diff for change reviews. Returns
  evidence-backed findings and coverage. No fixing, broad audits, or test suites.

Both use only `read`, `glob`, `grep`, and `tree`. There is no shell access: the
parent supplies command output or diffs when needed. `can_modify: false` alone
is not a sandbox for custom tools; the builtin tool selection excludes writers
and shell execution. Policies for additional capabilities belong in plugins.

### Defaults

| Setting | Both helpers |
| --- | --- |
| Budget plugin model calls, soft/hard | 8 / 12 |
| Budget plugin tool calls, soft/hard | 24 / 40 |
| Absolute `max_turns` | 14 |
| Whole-run `timeout` | 600 seconds |
| Automatic compaction | Not enabled by the preset |
| Interactive | No |

`explore` follows the per-sub-agent model mapping and otherwise uses the parent.
`critic` explicitly inherits the parent's model unless overridden in its entry.
A fast tool-capable profile can be selected for exploration without forcing a
weaker model on independent review.

At a soft limit, stop expanding scope and prepare the report. At a hard budget
limit, the plugin blocks tools but allows text reporting. The absolute loop cap
bounds a model that keeps requesting tools; reaching it reports failure with
collected text retained, rather than claiming completion. A natural text finish
on the last allowed call still completes normally. The emergency timeout is not
a graceful reporting deadline and may end before a final report is produced.

Limits are ceilings, not goals. Partial or blocked reports must name missing
context, unchecked scope, and the next useful check. Runtime success means the
agent returned normally, not that its findings establish complete coverage.

### Override limits or models

```yaml
subagents:
  - name: explore
    type: builtin
    max_turns: 18
    timeout: 900
    model: my-fast-profile
    plugins:
      - name: budget
        options:
          turn_budget: [10, 16]
          tool_call_budget: [30, 50]
```

The `plugins` list replaces the preset's list. Keep a reporting allowance between
the hard model-call budget and the absolute loop cap. `timeout` is the whole-run
deadline. The budget plugin's current `walltime_budget` measures time around LLM
calls, not total elapsed time including tools; do not use it as a whole-run timer.

## Context forks and `/btw`

Every creature registers the builtin `fork` helper by default, even with no
`subagents:` list. It answers a bounded side task using a snapshot of the
parent's current effective branch. This does **not** change `explore` or `critic`:
continue supplying self-contained questions and evidence to those fresh-context
helpers.

`fork` has only `read`, `glob`, `grep`, and `tree`: no writes, shell, graph
coordination, or recursive delegation. It inherits the parent's model but must
use an isolated provider instance. A provider without a working `fork()` that
returns a distinct instance is rejected; KT never silently reuses the parent's
provider. The preset has 8/12 soft/hard model-call and 24/40 tool-call budgets,
an absolute 14-call cap, a 600-second timeout, no automatic compaction, and no
shared parent iteration budget. Inherited tool inventories are background, not
capabilities granted to the child.

Context forks inherit only opted-in parent pre-tool authorization guards, checked
against the live enabled/applicable plugins after child argument transformations.
This is not blanket plugin inheritance: parent budgets, context hooks, and
lifecycle hooks are not shared. The builtin `sandbox` and `permgate` opt in;
new permission approvals made in a child remain private to that child. Custom
plugins opt in with `inherit_fork_policy = True` and may implement
`fork_tool_policy()` to create a validation-only guard with child-private state.
If parent policy identities or options change during an async approval, the tool
invocation is blocked for retry. These are known-tool authorization guards, not
an OS sandbox or automatic containment of arbitrary custom tool code.
See [fork authorization policies](../reference/plugin-hooks.md#fork-authorization-policies).

Disable the builtin and user side conversations at the creature level:

```yaml
fork_enabled: false
```

An explicit `subagents:` entry named `fork` overrides the implicit preset when
forks are enabled. For example, to override its timeout:

```yaml
fork_enabled: true                 # default; can be omitted
subagents:
  - name: fork
    type: builtin
    timeout: 300
```

### Ask without interrupting the main task

```text
/btw What does the error we just saw mean?
/btw --continue JOB_ID Which file should I inspect next?
/btw --open JOB_ID
/btw --stop JOB_ID
```

`/btw` is a user-facing side conversation, not a new parent turn. Neither its
question nor its answer becomes parent LLM input, and completion does not wake
the parent controller. It can run while the parent is busy. A follow-up continues
the side transcript, not a fresh snapshot of the parent's latest conversation;
wait for completion or stop the side run before continuing it.

With session persistence enabled, the transcript is saved under the same
sub-agent run and job ID, with source provenance and an inherited-message count.
You can reopen and continue it after restoring the session. The visible side
transcript hides the inherited parent prefix; hiding is a presentation boundary,
not redaction from the saved context. `/btw --stop` stops the side job, not the
parent. With `fork_enabled: false`, `/btw` returns a disabled error.

The web side-conversation panel shows the same “KohakUwUing…” indicator as main
chat while that side run is active, including follow-ups. Display names start
with a short label from the side question instead of the internal job ID. For
persisted Studio sessions, the existing session-summary settings also refine
side titles in the background using only the side exchanges, never the inherited
parent history. Disabling summaries keeps the initial label; reopening and
follow-ups preserve the title. Job IDs remain available for command addressing.

The controller can also call `fork` as a normal sub-agent; that result follows
normal controller result routing. This is distinct from user-audience `/btw`.
The existing **`/fork` command is unchanged**: it copies a session into a separate
`.kohakutr` file, rather than starting a context-aware side task.

For programmatic snapshots, see [`Agent.capture_context()`](../reference/python.md#context-snapshots).

## Service presets

`response`, `summarize`, `memory_read`, and `memory_write` remain available with
their existing service-specific behavior. They are not automatically added to
kt-biome's general creature. Their existing budget defaults are 40/60 model
calls and 75/100 tool calls, with the `auto-compact` plugin pack selected.

## Migration from older configurations

`worker`, `plan`, `research`, and `coordinator` are no longer builtin sub-agents.
Unknown or removed builtin entries produce a warning naming the entry and are
skipped, including in strict startup. Valid entries still register and the
creature remains runnable. Updating KT before kt-biome therefore does not brick
an older creature configuration.

Update kt-biome or remove stale entries. Keep planning with the parent, assign
substantial work to graph creatures, or declare a custom sub-agent if an
application specifically needs one. Python imports from removed builtin preset
modules must be migrated; this compatibility behavior covers config loading.

## Custom sub-agents

Custom configurations remain supported; the small builtin roster does not
restrict the runtime's general capabilities.

```yaml
subagents:
  - name: dependency_mapper
    type: custom
    description: Map dependency edges without editing files
    system_prompt: |
      Map the requested import relationships. Return file evidence and gaps.
    tools: [glob, grep, read, tree]
    can_modify: false
    max_turns: 14
    timeout: 600
    plugins:
      - name: budget
        options:
          turn_budget: [8, 12]
          tool_call_budget: [24, 40]
```

Inline entries support `SubAgentConfig` fields such as prompts, tools, output
routing, models, plugins, compaction, `max_turns`, and `timeout`. Runtime budget
axes live under the budget plugin's `options`, not directly on `SubAgentConfig`.
Use module-backed configs when an application needs shared Python definitions.

## Parent budgets and compaction

Budget plugins are instantiated per run. Their model/tool accounting is separate
from the optional legacy parent iteration pool. At runtime, `budget_allocation`
selects an isolated legacy allocation; otherwise `budget_inherit: true` can
reuse the parent's iteration pool. A shared pool can stop a child earlier than
its own limits. For builtin overrides, use the supported `plugins`, `max_turns`,
and `timeout` options rather than assuming every config field is overlaid.

For custom agents requiring longer contexts, opt into compaction explicitly:

```yaml
subagents:
  - name: document_reader
    type: custom
    system_prompt: "Read the supplied documents and report evidence and uncertainty."
    tools: [read, grep]
    default_plugins: [auto-compact]
    compact:
      max_tokens: 120000
      threshold: 0.75
      keep_recent_turns: 4
    max_turns: 30
    timeout: 900
    plugins:
      - name: budget
        options:
          turn_budget: [20, 28]
          tool_call_budget: [40, 60]
```

The `auto-compact` pack selects `compact.auto`; a `compact` block configures the
manager. The three inspection presets do not select either by default. Prefer a
smaller question or a graph creature over silently extending a lightweight run.
