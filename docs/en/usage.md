# BreezeTTS workbench

Author: 清蒸云鸭  
License: AGPL-3.0

Requires AstrBot 4.29.3+ and Python 3.10+. Install the repository, reload the plugin, then open **Plugins → BreezeTTS details → BreezeTTS workbench**. The interface is currently in Simplified Chinese.

| Workbench entry | Purpose |
| --- | --- |
| 合成试听 | Generate speech, play audio, download outputs, manage history |
| 音色与录音 | Upload reference recordings, edit transcripts, select voices, create official previews |
| 连接与设置 | Configure connections, credentials, generation parameters, and chat behavior |

The existing AstrBot plugin configuration entry remains available and edits the same settings. No existing AstrBot navigation has been renamed or moved.

![Workbench](../images/studio-desktop.png)

## Local API

Use the service root, such as `http://127.0.0.1:7860` without authentication or `http://127.0.0.1:7861` with authentication. Enter only the key itself. A configured key is sent as `Authorization: Bearer <key>`, including on health checks; an empty key sends no Authorization header.

For voice cloning, upload a recording, save its exact transcript, and select it as the default local voice. Supported uploads are WAV, MP3, FLAC, and OGG, up to 20 MiB and 120 seconds. Files are decoded and normalized to mono PCM16 WAV. Voice design uses an instruction instead of a reference; plain mode permits neither.

Local multipart synthesis uses `text`, `instruction`, `cfg_scale`, `seed`, and the paired `ref_audio` / `ref_text` fields. The raw PCM response is validated and wrapped in a playable WAV container.

## BreezeBlue official API

Use `https://api.breeze.blue/v1`, enter your developer key, and save the connection. Query the voice catalog and choose a voice, or enter an existing voice ID. Public catalog filtering uses `voice_type=default`.

Supported delivery modes are synchronous, streamed reception, and background jobs. Streamed audio is played after the complete response is saved. Background jobs are polled and downloaded only from controlled paths on the configured service; arbitrary upstream download URLs and redirects are not followed.

Clone previews require a normalized reference of at least 3 seconds and no more than 5 MB. Voice design requires a description and preview text. Audition the generated preview, then explicitly save it to the official account. Verification-required clones must be completed in the official console.

## Chat integration

Use `/breezetts text to speak`, `/btts`, or `/语音合成`. AstrBot command permissions and wake prefixes apply. The `breeze_tts` LLM tool is enabled by default and can be disabled in settings.

Automatic speech is off by default. Enable it for completed non-streaming LLM replies; disable AstrBot's built-in TTS and streaming chat output to avoid duplicate or unsupported output. Failed or oversized synthesis retains the original text. A successful tool-generated voice prevents a second automatic voice for the same event.

Audio uses AstrBot's standard `Record` component. Delivery and codec conversion depend on the platform adapter; live delivery has not been tested on every platform.

## Storage and credentials

Persistent files live in `data/plugin_data/astrbot_plugin_breezetts/`. Up to 100 references are retained until manually removed. Generated outputs and previews share a configurable retention limit, defaulting to 50. Reload cancels plugin-owned tasks while preserving saved files.

The workbench returns only credential-presence flags. Blank credential inputs preserve existing keys; explicit checkboxes clear them. Keys are stored by AstrBot's configuration system. HTTP errors do not expose upstream response bodies or credentials. Generation and save requests are never retried automatically.

See the [README](../../README.md) for development tests, optional real-model checks, and upstream references.
