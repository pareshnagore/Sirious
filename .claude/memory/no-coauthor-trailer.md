---
name: no-coauthor-trailer
description: "User does not want the Co-Authored-By: Claude trailer in any commit message"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 5b0044f1-f0e8-4b7e-93fa-38f6f8e99696
  modified: 2026-08-19T20:25:25.391Z
---

Never add a `Co-Authored-By: Claude <noreply@anthropic.com>` trailer (or any co-author line) to commit messages for this user.

They explicitly asked on 2026-08-20 after seeing it in a commit. A commit I made with the trailer was amended to remove it.

**Why:** the user wants clean commit authorship — no Claude co-author credit.

**How to apply:** when writing any `git commit`, leave out `Co-Authored-By:` entirely. This also applies to PR bodies/trailers. See [[remote-push]] and [[build-toolchain-workflow]].