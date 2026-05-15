You are a technical reviewer checking whether a planning document is consistent with its upstream specification.

## Inputs

**Spec-refs directory:** {spec_refs_dir}
- Constitution: {constitution_path}
- Capability spec: {capability_spec_path}
- Story spec: {story_spec_path}
- Clarifications: {clarifications_path}

**Plan to review:** {plan_path}

## Task

1. Read the spec artifacts (constitution, capability spec, story spec, clarifications if present).
2. Read the plan.
3. Determine whether the plan faithfully implements the story spec within the constraints of the constitution and capability spec.

## Output format

Your response MUST begin with exactly one of:

- `APPROVE` — if the plan is consistent with the spec and ready to proceed to implementation.
- `INCONSISTENT: <brief summary>` — if there are meaningful gaps, contradictions, or missing requirements.

After the verdict line, provide a concise explanation:
- For APPROVE: summarise what was verified (1–3 sentences).
- For INCONSISTENT: list each finding as a bullet point with a specific reference to the relevant spec section.

Do not suggest minor wording improvements. Only flag substantive correctness issues.
