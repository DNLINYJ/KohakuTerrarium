# Builtin sub-agents

The public task helpers are `explore` (bounded workspace investigation) and
`critic` (bounded independent review). They use only `glob`, `grep`, `read`, and
`tree`; callers supply diffs and relevant conversation constraints explicitly.
Sub-agents inspect; graph creatures own substantial delegated work. Planning
and final decisions remain with the parent.

Both inspection presets use the budget plugin with soft/hard model-call limits
8/12 and tool-call limits 24/40, an absolute `max_turns=14`, and a whole-run
`timeout=600`. The hard plugin gate blocks tools while allowing a final text
report; the loop cap reports incomplete work if no final response arrives.
Neither preset opts into automatic compaction. `critic` explicitly inherits the
parent model; `explore` uses the sub-agent model mapping with a parent fallback.

Service presets remain unchanged: `response`, `summarize`, `memory_read`, and
`memory_write`. They retain their own prompt template and budget defaults.

`worker`, `plan`, `research`, and `coordinator` are no longer builtins. Config
entries referring to unavailable builtins are warned about and skipped even
under strict startup. Update kt-biome/configs; use graph creatures for substantial
work or define an application-specific custom sub-agent. Direct Python imports
of removed preset modules are not supported.

## Files

| File | Purpose |
| --- | --- |
| `explore.py` | Workspace-question contract and defaults |
| `critic.py` | Independent-review contract and defaults |
| `_inspection_prompt.md` | Shared bounded-inspection instructions |
| `_base_prompt.md` | Existing service-agent prompt template |
| `_prompt_loader.py` | Prompt rendering |
| `response.py`, `summarize.py` | Output and continuation services |
| `memory_read.py`, `memory_write.py` | Memory services |
| `__init__.py` | Lazy public exports |

The catalog lives in `builtins/subagent_catalog.py` and returns defensive copies.
