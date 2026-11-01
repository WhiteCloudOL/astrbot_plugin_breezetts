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

The former automatic-reply speech option is now Random Speech, and Allow LLM Speech Tool is now Autonomous LLM Speech. Both remain in Chat & Runtime

Private and group conversations can inherit the default probability or use separate values. The conversation interval defaults to 90 seconds and is shared by random speech and LLM tool calls. Different passages in one turn can follow one another; setting the interval to zero disables the cooldown

![Mobile speech settings](../images/settings-mobile.png)

Generated audio and previews have configurable count and capacity limits, defaulting to 50 files and 256 MiB. Reference recordings are retained separately

![Mobile workbench](../images/studio-mobile.png)

See the [README](../../README.md) for installation, configuration fields, and chat commands
