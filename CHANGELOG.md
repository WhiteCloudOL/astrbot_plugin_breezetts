# Changelog

## v1.3.1 — 2026-11-01

- Deliver Breeze audio independently at the platform boundary while preserving surrounding text, native segmentation, and optional splitters.
- Release audio reservations after successful, failed, or cancelled delivery and prevent whole-message retries after a partial send.
- Keep voice messages outside long-text forward cards.
- Enable a fresh random seed for each local synthesis by default, with a workbench switch and optional fixed seed.
- Inject temporary speech guidance after final tool-permission filters, preserving stable prompts and saved history.
- Read voice requests from the current message rather than quoted content and honor explicit text-only replies.
- Require a real model-response event before automatic narration, preserving fixed command replies even when their result type is mislabeled.

## v1.3.0 — 2026-11-01

- Execute random speech directly after the program's probability decision, selecting one bounded prose passage before message splitting.
- Keep autonomous tool calls compatible with random speech through shared cooldown, locking, and per-turn deduplication.
- Preserve surrounding text, media, code, links, and lists while supporting ordinary delivery, native segmentation, and the splitter plugin.
- Keep tool receipts internal and remove standalone delivery acknowledgments from model output and current-turn history.
- Update probability controls, usage documentation, and debug diagnostics without adding random-speech instructions to LLM requests.

## v1.2.3 — 2026-10-31

- Set the minimum supported AstrBot version to 4.26.0 and align installation instructions.
- Use the official AstrBot logger when the host does not provide a plugin-specific logger.
- Discover the workbench through the shared pages directory and support both Dashboard bridge names.

## v1.2.2 — 2026-10-31

- Add temporary current-capability guidance for explicit voice requests while keeping ordinary text turns free of speech instructions.
- Clarify that the speech tool uses BreezeTTS workbench settings and reports actual synthesis, delivery, and caption status.
- Keep capability guidance out of saved conversation history and preserve voice opt-out requests.

## v1.2.1 — 2026-10-31

- Detect updated workbench fields against an older running backend and show plugin reload instructions before editing settings.
- Require explicit values for custom probabilities and the conversation interval.
- Verify persisted settings and credentials survive reload and gain the new behavior defaults.

## v1.2.0 — 2026-10-31

- Add private/group probability overrides with inherited defaults and a shared per-conversation cooldown for random and autonomous speech.
- Reserve in-flight generation across concurrent turns, release failed attempts, and bound cooldown records in memory.
- Keep explicit commands and same-turn passages available, with responsive settings and usage documentation.
- Credit the speech-frequency design inspiration from [astrbot_plugin_private_companion](https://github.com/menglimi/astrbot_plugin_private_companion).

## v1.1.0 — 2026-10-31

- Replace whole-reply automatic synthesis with probability-based, model-selected spoken passages; rename Automatic Reply Speech to Random Speech and Allow LLM Speech Tool to Autonomous LLM Speech in Chat & Runtime.
- Support concurrent random and autonomous modes with turn-local deduplication, tag-free replies/history, and temporary prompt hints.
- Preserve ordinary text and voice ordering with no splitting, AstrBot's built-in splitting, and the splitter plugin.
- Send tool-generated voice messages independently and retain text when generation fails.
- Add configurable count and total-capacity limits for downloaded outputs/previews, with reservations for in-flight delivery and reads.
- Remove standalone Python project/development manifests and use host tooling for verification.

## v1.0.1 — 2026-10-31

- Fix audio playback through the AstrBot bridge and restore the latest generated audio on reopening.
- Separate local reference voices from official voice catalogs and preview forms.
- Group voice-card controls, emphasize voice selection, and add capsule switches and inline key-clear buttons.
- Improve responsive layouts, playback recovery, configuration-save consistency, and accessibility.
- Harden audio validation, storage rollback, lifecycle handling, input boundaries, and secret-safe error logging.
- Add an original BreezeTTS icon and simplify the README around features, configuration, and usage.

## v1.0.0 — 2026-10-31

- Add local Breeze multipart synthesis with optional Bearer authentication and PCM-to-WAV conversion.
- Add BreezeBlue official synthesis with synchronous, stream, and background delivery, voice pagination, clone/design previews, and explicit voice saving.
- Add the BreezeTTS workbench with audio uploads, transcript editing, playback, downloads, history, responsive layout, and theme integration.
- Add chat commands, an LLM speech tool, optional automatic speech, and resource cleanup on reload.
- Preserve the existing AstrBot plugin configuration entry; add workbench entries for synthesis, voice management, and connection settings.
- Add protocol, persistence, AstrBot loader/API, browser, and opt-in real-model tests.
