# BreezeTTS workbench

Open BreezeTTS from the AstrBot plugin details page. The interface is in Simplified Chinese and follows AstrBot's light or dark theme

After an update, reload BreezeTTS in plugin management, then refresh the workbench

| Entry | Purpose |
| --- | --- |
| 合成试听 | Enter text, select a voice, synthesize, play, and download audio |
| 音色与录音 | Manage voices for the currently selected service |
| 连接与设置 | Set the service URL and key, voice parameters, and chat behavior |

The standard AstrBot configuration entry and the workbench share the same settings. Voice management displays local references or official account voices according to the selected service

## Local API

Set the local service root, such as `http://127.0.0.1:7860` or `http://127.0.0.1:7861`. A populated API Key sends Bearer authentication; an empty key uses direct access

Upload a recording, enter its exact transcript, and select it as the current voice. Each card has a prominent selection button, grouped playback/download/edit controls, and a separate delete action. Voice design uses the description configured in settings; plain mode provides ordinary narration

![Local voice library](../images/voices-local-desktop.png)

## Official API

Select BreezeBlue official API and enter `https://api.breeze.blue/v1` and your developer key. Save and check the connection, then browse personal or public voices and choose one

Clone creation has its own reference-upload control. Enter a voice name and language, generate a preview, audition it, and save it to your account. Voice design accepts a voice description and preview script

## Controls and playback

Clear buttons sit beside key inputs. Click Clear and save to remove a key; enter a new value and save to replace it. Blank key inputs retain existing saved credentials

Boolean settings use accessible capsule switches. The player sits beside the composer on PC and below it on mobile, showing the audio name, duration, loading state, retry action, and download button. Reopening the workbench loads the most recent generated output

In Connection & Settings → Chat & Runtime, enable random speech and choose its probability, or enable autonomous LLM speech to let the model select passages through `breeze_tts`. Both modes share turn-local duplicate suppression. Selected passages become voice messages while ordinary text follows the current delivery mode: no splitting, AstrBot's built-in splitting, or the splitter plugin

Random speech uses one program-side draw per turn and directly synthesizes a prose passage after a hit, bounded to 200 characters and the configured text limit. Other content follows the existing segmentation settings. Tool receipts stay internal, and standalone delivery acknowledgments are removed from model replies and the current turn's history

Enable **启用动态语调** (Dynamic Tone) under Connection & Settings → Chat & Runtime to use inline native events and section directions. For example, `[style:gentle](laugh)Welcome back. [style:confident]We can do this.` uses independent instructions for each section and joins the resulting WAV audio with natural pauses. Native tags remain in speech input, while recognized controls are hidden from captions and history. Code, escaped literals, ordinary brackets, and Markdown links are preserved. Directions must match the dialogue language, with 1–200 characters each and at most eight markers. The `breeze_tts` tool also accepts an optional overall `instruction`, up to 200 characters. Saved voice descriptions remain in effect. Model support for native events varies; see the [official local model](https://github.com/breezeblue-ai/breeze-tts#-highlights) and [cloud audio tags](https://docs.breezeblue.ai/guides/audio-tags).

Dynamic Tone is off by default. When off, recognized inline controls and per-call instructions are ignored; the configured fixed style still applies. With **概率语音 LLM 语调自适应** (Adaptive Tone for Random Speech) also enabled, the plugin makes one additional LLM call only after a probability hit and passage selection. Choose an existing chat provider in **语调自适应 LLM 提供商**, or leave it empty to follow the current conversation. The LLM selects delivery style without rewriting dialogue. Missing providers, invalid output, and a 20-second timeout fall back to the configured style. No adaptive call runs when Dynamic Tone is off, probability misses, cooldown or quote policy blocks speech, or a tool already handled it.

When keeping the spoken text, the plugin sends its caption first and then a standalone voice message. Text and voice are never combined in a single outgoing message. Use punctuation and single native pause tags rather than stacked pauses or invented duration syntax. Multi-section synthesis returns one WAV file; single-section synthesis retains the configured output format.

The former automatic-reply speech option is now Random Speech, and Allow LLM Speech Tool is now Autonomous LLM Speech. Both remain in Chat & Runtime

Private and group conversations can inherit the default probability or use separate values. The conversation interval defaults to 90 seconds and is shared by random speech and LLM tool calls. Different passages in one turn can follow one another; setting the interval to zero disables the cooldown

![Mobile speech settings](../images/settings-mobile.png)

Generated audio and previews have configurable count and capacity limits, defaulting to 50 files and 256 MiB. Reference recordings are retained separately

![Mobile workbench](../images/studio-mobile.png)

See the [README](../../README.md) for installation, configuration fields, and chat commands
