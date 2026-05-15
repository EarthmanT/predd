You are producing a story spec.md from a slice definition and Jira story data.

Slice information:
- Goal: {slice_goal}
- Scope: {slice_scope}
- Out of scope: {slice_out_of_scope}
- Done when: {slice_done_when}
- Trace links: {slice_trace_links}
- Impacted repos: {impacted_repos}

Jira story:
- Summary: {jira_summary}
- Description: {jira_description}
- Acceptance criteria: {jira_acceptance_criteria}

---

Produce a story spec.md with exactly these sections:

## What
Combine the slice Goal with the Jira summary into a single coherent statement of what
this story delivers. Do not pad or repeat yourself.

## Scope
The scope of this story, from the slice Scope section.

## Out of Scope
What is explicitly excluded, from the slice Out of Scope section.

## Acceptance Criteria
Use Jira acceptance criteria if present. Otherwise use the slice "Done When" as the sole
acceptance criteria source. Do NOT hallucinate requirements that are absent from both sources.

## Trace Links
The BRs and ERs from the slice Trace Links section, verbatim.

## Impacted Repos
The repositories that will require changes, from slice.yaml.

---

Output ONLY the markdown content. No preamble, no code fences, no commentary.
