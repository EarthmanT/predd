You are implementing issue #{issue_number}: {issue_title}

Spec-kit artifacts are in `{spec_refs_dir}/`:
- Constitution: `{constitution_path}`
- Capability spec: `{capability_spec_path}`
- Story spec: `{story_spec_path}`
- Clarifications: `{clarifications_path}`

Implementation plan: `{plan_path}`
Tasks file (if present): `{tasks_path}`

Issue description:
{issue_body}

---

Your task is to implement the changes described in `plan.md` and complete all items in `tasks.md` (if it exists).

Instructions:
1. Read `plan.md` to understand the full implementation plan, files to change, approach, and acceptance criteria.
2. Read `tasks.md` if it exists — it contains a structured task list approved by the reviewer. Complete every task listed.
3. Read the spec-kit artifacts (story spec, capability spec, constitution, clarifications) for deeper context.
4. Read the existing codebase as needed to understand conventions, patterns, and dependencies.
5. Implement all changes. Every acceptance criterion in `plan.md` must be met.
6. Write tests that verify the acceptance criteria where applicable.
7. Do not modify `spec-refs/`, `plan.md`, or `tasks.md` — these are read-only references.
8. Clarifications (if present) take precedence over the capability spec for anything they address.

When done, all acceptance criteria from `plan.md` must be satisfied and tests must pass.
