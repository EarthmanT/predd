You are working on issue #{issue_number}: {issue_title}

Spec-kit artifacts are in `{spec_refs_dir}/`:
- Constitution: `{constitution_path}`
- Capability spec: `{capability_spec_path}`
- Story spec: `{story_spec_path}`
- Clarifications: `{clarifications_path}`

Capability: {capability}
Story ID: {story_id}
Capability specs SHA: {capability_specs_sha}

Issue description:
{issue_body}

---

Your task is to produce a `plan.md` file in the repository root with the following structure:

```markdown
---
story_id: {story_id}
capability: {capability}
capability_specs_sha: {capability_specs_sha}
---

## Plan

### Overview
[2-3 sentences summarising what will be built and why]

### Files to change
[Bulleted list of files that will be created, modified, or deleted]

### Approach
[Step-by-step implementation plan. Each step should be concrete and actionable.]

### Acceptance criteria
[Bulleted list derived from the story spec. Each criterion must be verifiable.]

### Out of scope
[Anything explicitly excluded from this story]
```

Instructions:
1. Read the spec-kit artifacts listed above (constitution, capability spec, story spec, and clarifications if present).
2. Read the existing codebase to understand the architecture, conventions, and relevant code.
3. Write `plan.md` to the repository root with content matching the structure above.
4. Do not implement any code changes — only produce `plan.md`.
5. The plan must be grounded in the story spec and consistent with the capability spec and constitution.
6. Clarifications (if present) take precedence over the capability spec for anything they address.
