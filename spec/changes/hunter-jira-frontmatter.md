# Hunter: Jira Frontmatter on GitHub Issues

## Problem

When hunter creates GitHub issues from Jira stories, there's no structured metadata linking back to Jira. Reviewers can't quickly see the Jira ID, type, epic, sprint, or capability without clicking through.

## Proposed Behaviour

When hunter creates a GitHub issue, it adds a frontmatter block at the top of the issue body:

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

- Hunter already fetches the GitHub issue via `gh issue view`. Jira data requires a separate `GET /rest/api/2/issue/{key}` call using the Jira REST API.
- Jira auth: personal access token stored as `JIRA_TOKEN` env var. Basic auth header: `Authorization: Bearer {token}`.
- Sprint field: Jira returns sprint as an array in `customfield_10020` (or similar). Hunter takes the last entry's `name`.
- Epic field: may be in `customfield_10014` (epic link key) or `parent` depending on Jira config. Hunter tries both.
- The Jira issue key is expected to already be in the GitHub issue title (e.g. `[DAP09A-1184]`). Hunter parses it with regex `\[([A-Z]+-\d+)\]`.
- Add `jira_base_url` and `require_jira_conformance` to `Config`.

## Open Questions

- What's the Jira custom field ID for sprint in this instance? (Can discover at runtime via `/rest/api/2/issue/{key}?expand=names`)
- What's the Jira custom field ID for epic link?
