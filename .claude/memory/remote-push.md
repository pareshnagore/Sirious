---
name: remote-push
description: "How git pushes to GitHub are handled on this machine (user token, not agent credentials)"
metadata: 
  node_type: memory
  type: project
  originSessionId: 5b0044f1-f0e8-4b7e-93fa-38f6f8e99696
  modified: 2026-08-19T20:19:22.346Z
---

The local agent must never push to the Sirious GitHub remote or use the user's git credentials.

- The user set up a **fine-grained Personal Access Token (PAT)** specifically for `git push` on this machine.
- The repo `pareshnagore/Sirious` is **public**.
- Agent workflow: commit locally (`git commit`), and hand the push back to the user (or they push themselves). Do not attempt `git push` with credentials that aren't the token, and do not invoke the user's token.

**Why:** the agent has no access to the user's credentials and pushing with the wrong mechanism would fail or risk leaking to the wrong remote. The public repo also means secrets must stay untracked (see [[build-toolchain-workflow]]).
**How to apply:** after committing, tell the user "committed, ready for you to push" rather than pushing.