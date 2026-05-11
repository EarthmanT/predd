# Hunter

## What It Is

Hunter is a companion tool to predd that closes the loop on GitHub issues. Where predd reviews other people's PRs, hunter works your own queue: it picks up assigned issues, writes proposals, implements them, self-reviews, and hands back to the reporter after release.

Both tools share the same config, state, logging, gh helpers, worktree setup, and backend drivers. The implementation lives in `hunter.py` alongside `predd.py`.

---

## Workflow

```
Issue assigned to you
        │
        ▼
Hunter picks up → labels issue "[user]:in-progress"
        │
        ▼
Creates proposal PR (draft) → labels issue "[user]:proposal-open"
        │
        ▼
        ── HUMAN reviews proposal PR ──
        │  (requests changes or approves)
        │
        ▼
Hunter detects proposal PR merged → creates implementation PR (draft)
        │                           labels issue "[user]:implementing"
        ▼
Hunter self-reviews its own implementation PR
        │  (runs predd skill on the PR)
        │
        ▼
Hunter fixes identified issues → pushes to implementation branch
        │
        ▼
Hunter marks implementation PR ready for review
        │
        ▼
        ── HUMAN reviews implementation PR ──
        │
        ▼
Human merges → release pipeline runs
        │
        ▼
Hunter detects merge → re-opens issue, assigns back to reporter
        labels issue "[user]:awaiting-verification"
```

---

## "Picked Up" Signals

**For issues:** GitHub label `[user]:in-progress` added by hunter at pickup time.
This is the distributed lock. Hunter adds the label then re-reads the issue to confirm it won the race (see Race Conditions below).

**For proposals:** GitHub label `[user]:proposal-open` on the issue + a draft PR whose body contains `[user]:issue-<number>`. Hunter scans its own open draft PRs to find proposals it created.

**For implementation:** GitHub label `[user]:implementing` on the issue + a non-draft PR whose body contains `[user]:impl-<number>`.

Labels are preferred over milestones — they're lightweight, queryable via `gh`, and don't require project-level setup.

---

## Race Condition Prevention

GitHub label operations are not atomic. Two hunter instances can both read an unlabeled issue and both try to pick it up.

Mitigation: **check-then-label-then-verify**

1. Read issue — confirm `[user]:in-progress` is absent
2. Add label via `gh issue edit --add-label [user]:in-progress`
3. Re-read issue after 2s — confirm the label is present AND no other [user]:* label exists from a concurrent run
4. If another label appeared, back off and skip

This won't be 100% race-free without a distributed lock service, but in practice (single user, small team) the 2s re-read catches collisions.

---

## Human Touchpoints

| Step | Human action | Hunter waits for |
|------|-------------|-----------------|
| Issue triage | Assign issue to yourself | Assignment (trigger) |
| Proposal review | Review/merge proposal PR | PR merged or closed |
| Implementation review | Review implementation PR | PR merged |
| Verification | Test the release | (out of scope for hunter) |

Hunter does not require human assignment of proposal PRs — assignment of the issue means the assigned user owns proposal + implementation. Anyone else on the team who wants to pick it up should re-assign the issue to themselves.

---

## Post-Release Verification

After the implementation PR merges:

1. Hunter detects merge via polling the PR state
2. Re-opens the original issue
3. Adds label `[user]:awaiting-verification`
4. Re-assigns to the issue's original reporter (`issue.author.login`)
5. Posts a comment: "Implementation merged in #<pr>. Please verify and close when confirmed."

This returns ownership to the reporter without requiring any manual step.

---

## Decisions

- **Proposal format:** Follow the openspec process.
- **Self-review loop:** 1 self-review pass, 1 fix cycle. If issues remain after the fix, flag for human and stop.
- **Branch naming:** User-defined via config. Default pattern: `usr/at/<issue-number>-proposal-<short-name>` and `usr/at/<issue-number>-impl-<short-name>`.
- **Config:** Single `~/.config/predd/config.toml`. Hunter is an optional module within predd.
- **Repo scope:** Three lists in config:
  - `repos` — watched by both predd and hunter
  - `predd_only_repos` — predd reviews only
  - `hunter_only_repos` — hunter issue tracking only
- **Labels:** Namespaced by GitHub username, e.g. `adam:in-progress`, not `hunter:in-progress`. Avoids collisions on shared repos.
