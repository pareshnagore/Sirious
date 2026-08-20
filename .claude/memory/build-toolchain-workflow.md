---
name: build-toolchain-workflow
description: "Sirious mobile build toolchain versions, keystore handling, and cross-PC workflow constraints"
metadata: 
  node_type: memory
  type: project
  originSessionId: 5b0044f1-f0e8-4b7e-93fa-38f6f8e99696
  modified: 2026-08-19T20:19:17.007Z
---

Sirious Android build requires a specific toolchain and has a cross-PC workflow the user depends on.

**Toolchain (this PC had never built it; other PCs may not either):**
- Flutter ≥ 3.47 (Dart ≥ 3.11). Old Flutter fails `pub get` (path_provider needs Dart ≥ 3.10).
- Gradle 9.3.1 / AGP 9.1.0 / Kotlin 2.4.0 — Flutter 3.47's tested defaults. Old Gradle (8.14) can't run on the Java 25 JBR that Flutter uses via Android Studio; no JDK install is needed.
- Android build-tools 37.0.0 must be installed (`sdkmanager "build-tools;37.0.0"`).

**flutter_pcm_sound pitfall:** v3.3.3 (latest) ships `compileSdk 33`; its AndroidX deps need 34+ → fails `checkDebugAarMetadata`. Patch its pub-cache `build.gradle` to `compileSdkVersion 37`. Lives in `~/.pub-cache`, NOT the repo; a fresh `pub get` reverts it. The full details are in `product_phases.md` → "Android build toolchain + keystore".

**Workflow constraints (not obvious from code):**
- The user develops on this Mac, but has also built on another Windows PC and pulls/pushes between them. They expect changes committed here to be pulled there.
- The GitHub repo `pareshnagore/Sirious` is **PUBLIC** → never commit the signing keystore (`upload-keystore.jks`) or `key.properties` (passwords). Keep them gitignored; transfer PC-to-PC privately or make the repo private if the user opts in.
- The agent (Claude Code) must **not** push to remote nor use the user's git credentials. The user holds a fine-grained PAT for `git push`. Agents commit locally; the user pushes.
- If a release APK is needed, ask the user for/point them to the keystore location rather than reading it out.

**Why:** avoids rebuilding the wrong toolchain on a fresh PC and avoids leaking the signing key into a public repo.
**How to apply:** before any `flutter build`, verify Flutter ≥ 3.47 and build-tools 37; when committing, leave keystore/key.properties untracked and never `git push` — hand back to the user. See [[remote-push]].