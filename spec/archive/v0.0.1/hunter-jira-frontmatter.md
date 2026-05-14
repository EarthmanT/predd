# Hunter: Jira Frontmatter on GitHub Issues

## Problem

When hunter creates proposal and implementation PRs for assigned GitHub issues, there's no structured metadata linking back to Jira. Reviewers can't quickly see the Jira ID, type, epic, sprint, or capability without clicking through.

## Proposed Behaviour

When hunter creates a proposal or implementation PR, it adds a frontmatter block at the top of the PR body:

```markdown
| Field | Value |
|-------|-------|
| Jira | [DAP09A-1184](https://jira.cec.lab.emc.com/browse/DAP09A-1184) |
| Type | Story |
| Epic | [DAP09A-1000](https://jira.cec.lab.emc.com/browse/DAP09A-1000) Epic Name |
| Sprint | DAP09A Sprint-10 2026-05-12 |
| Capability | 12345 — cool feature |

---

[issue body follows]
```

## Jira Fields

| GH Field | Source | Notes |
|----------|--------|-------|
| Jira | `issue.key` | Hyperlinks to `{jira_base_url}/browse/{key}` |
| Type | `issue.fields.issuetype.name` | e.g. Story, Bug, Task |
| Epic | `issue.fields.epic.key` + `epic.fields.summary` | Hyperlinks to epic. Empty row if no epic. |
| Sprint | `issue.fields.sprint.name` | e.g. `DAP09A Sprint-10 2026-05-12`. Empty if no sprint. |
| Capability | Parsed from description | See below. Empty if not found. |

## Capability Parsing

Hunter looks for a line in the Jira description matching:

```
capability: <id> <name>
```

Examples that match:
- `capability: 12345 cool feature`
- `Capability: 42 auth subsystem`
- `capability:99 payments` (no space after colon)

If found, displayed as `{id} — {name}`. If not found, the row is omitted from the frontmatter.

## Conformance Flagging

Hunter flags issues that don't conform by adding a label `{github_user}:needs-jira-info` and posting a comment:

```
⚠️ This issue is missing required Jira fields:
- No sprint assigned
- No capability found in description (add `capability: <id> <name>`)

Hunter will not process this issue until it conforms.
```

Hunter skips (does not pick up) non-conformant issues. On the next poll, if the issue is now conformant, hunter picks it up normally.

## Required fields for conformance

- Epic must be set
- Sprint must be set
- Capability line must be present in the Jira description

Type and Jira ID are always available so never block conformance.

## Config

```toml
jira_base_url = "https://jira.cec.lab.emc.com"

# If false, hunter picks up issues regardless of conformance (just omits missing fields)
require_jira_conformance = true
```

## Implementation Notes

- Hunter parses the Jira key from the GH issue title with regex `\[([A-Z]+-\d+)\]`.
- Jira data fetched via `GET /rest/api/2/issue/{key}` using the Jira REST API.
- Jira auth: personal access token stored as `JIRA_TOKEN` env var. Header: `Authorization: Bearer {token}`.
- Sprint field: returned in `customfield_10020` (or similar) as an array. Hunter takes the last entry's `name`. Field ID discovered at runtime via `/rest/api/2/issue/{key}?expand=names` and cached.
- Epic field: may be `customfield_10014` (epic link key) or `parent.key` depending on Jira config. Hunter tries both.
- Frontmatter is prepended to the PR body in `gh_create_branch_and_pr` — hunter builds the body string before calling it.
- Add `jira_base_url` and `require_jira_conformance` to `Config`.
