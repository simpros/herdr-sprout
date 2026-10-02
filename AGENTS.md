# AGENTS.md

## Agent skills

### Pull requests

Open and describe every PR with the `visual-pr` skill vendored in this repo at
`.agents/skills/visual-pr/` — Why in one sentence, 1-3 special notes, and a
structural Change outline. Its frontmatter says explicit-invoke-only; that
default is overridden here: use it for every PR body. See
`docs/agents/pull-requests.md` for the publishing steps and the show-me-comment
rule.

### Vendored skills

- `documentation-writer` (`.agents/skills/documentation-writer/`) — every docs or
  prose task: Diátaxis page types (tutorial / how-to / reference / explanation) and
  its clarify → outline → write workflow.
- `emil-design-eng` (`.agents/skills/emil-design-eng/`) — building or reviewing UI:
  animation decisions, easing and duration, component feel, motion performance, a11y.
- `review-animations` (`.agents/skills/review-animations/`) — reviewing animation and
  motion code against the craft bar; it defaults to flagging.
