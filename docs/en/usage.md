# BreezeTTS workbench

Open BreezeTTS from the AstrBot plugin details page. The interface is in Simplified Chinese and follows AstrBot's light or dark theme

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

![Mobile workbench](../images/studio-mobile.png)

See the [README](../../README.md) for installation, configuration fields, and chat commands
