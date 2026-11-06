"""Verify persistence, input validation, and credential handling."""

import copy
import io
import json
import wave

import pytest
from breezetts_test.settings import DEFAULTS, BreezeError, api_root, validate_config
from breezetts_test.storage import AudioStore, playable_audio


def test_existing_local_settings_enable_random_seed_by_default():
    config = copy.deepcopy(DEFAULTS)
    config["local"].pop("random_seed")
    config["local"]["seed"] = 77
    validated = validate_config(config)
    assert validated["local"]["random_seed"] is True
    assert validated["local"]["seed"] == 77


@pytest.mark.parametrize("name", ["speech_prompt", "tone_prompt"])
def test_editable_prompt_defaults_and_schema_agree(config, name):
    from pathlib import Path

    config["behavior"].pop(name)
    assert validate_config(config)["behavior"][name] == DEFAULTS["behavior"][name]
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["behavior"]["items"][name]["default"] == DEFAULTS["behavior"][name]


@pytest.mark.parametrize("name", ["speech_prompt", "tone_prompt"])
@pytest.mark.parametrize("value", [[], None, "x" * 6001, "invalid\x00prompt"])
def test_editable_prompts_reject_invalid_values(config, name, value):
    config["behavior"][name] = value
    with pytest.raises(BreezeError):
        validate_config(config)


async def test_prompt_defaults_and_custom_edits_round_trip(service, config):
    snapshot = await service.state()
    assert snapshot["prompt_defaults"] == {
        name: DEFAULTS["behavior"][name] for name in ("speech_prompt", "tone_prompt")
    }
    snapshot["config"]["behavior"].update(
        speech_prompt="像老朋友一样轻松自然。", tone_prompt="保留俏皮的节奏，按需轻笑。"
    )
    saved = await service.save_settings(snapshot["config"], [])
    assert saved["config"]["behavior"]["speech_prompt"] == "像老朋友一样轻松自然。"
    assert saved["config"]["behavior"]["tone_prompt"] == "保留俏皮的节奏，按需轻笑。"
    assert saved["prompt_defaults"] == snapshot["prompt_defaults"]


def test_quote_policy_defaults_and_validation():
    config = copy.deepcopy(DEFAULTS)
    config["behavior"].pop("quote_keep_text")
    assert validate_config(config)["behavior"]["quote_keep_text"] is False
    config["behavior"]["quote_keep_text"] = True
    assert validate_config(config)["behavior"]["quote_keep_text"] is True
    config["behavior"]["quote_keep_text"] = "false"
    with pytest.raises(BreezeError, match="quote_keep_text"):
        validate_config(config)


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_random_seed_requires_a_boolean(value):
    config = copy.deepcopy(DEFAULTS)
    config["local"]["random_seed"] = value
    with pytest.raises(BreezeError, match="random_seed"):
        validate_config(config)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://localhost:7860", "http://localhost:7860/v1"),
        ("https://example.test/proxy/v1/", "https://example.test/proxy/v1"),
        ("http://[::1]:7861/v1/audio/speech", "http://[::1]:7861/v1"),
    ],
)
def test_url_normalization(url, expected):
    assert api_root(url) == expected


def test_generated_cache_count_capacity_and_protected_audio(tmp_path):
    store = AudioStore(tmp_path / "bounded")
    reference = store.add(b"r" * 20, "wav", "reference", "Reference")
    oldest = store.add(b"a" * 4, "wav", "output", "Old")
    middle = store.add(b"b" * 4, "wav", "preview", "Preview")
    newest = store.add(b"c" * 4, "wav", "output", "New")
    store.prune(10, max_bytes=8)
    assert oldest["id"] not in store.items
    assert reference["id"] in store.items and middle["id"] in store.items
    store.prune(1, {middle["id"]}, max_bytes=8)
    assert newest["id"] not in store.items
    assert set(store.items) == {reference["id"], middle["id"]}
    last = store.add(b"d" * 4, "wav", "output", "Last")
    store.prune(1, {middle["id"], last["id"]}, max_bytes=8)
    assert all(store.path(item["id"]).is_file() for item in (middle, last))
    store.prune(1, max_bytes=8)
    assert set(store.items) == {reference["id"], last["id"]}


@pytest.mark.parametrize(
    "url",
    [
        "ftp://host",
        "http://user:key@host",
        "http://host?key=secret",
        "http://host/#token",
        "http://host/../private",
        "http://host:invalid",
        "localhost:7860",
    ],
)
def test_invalid_urls(url):
    with pytest.raises(BreezeError):
        api_root(url)


@pytest.mark.parametrize(
    "group,key,value",
    [
        ("local", "api_key", "secret\nheader"),
        ("local", "seed", True),
        ("local", "cfg_scale", float("nan")),
        ("cloud", "speed", 0),
        ("cloud", "language_code", "chinese"),
        ("cloud", "delivery", "wrong"),
        ("behavior", "max_characters", 2001),
        ("behavior", "timeout", -1),
    ],
)
def test_invalid_settings(group, key, value):
    config = copy.deepcopy(DEFAULTS)
    config[group][key] = value
    with pytest.raises(BreezeError):
        validate_config(config)


async def test_credentials_preserve_clear_and_rollback(service, config):
    config["cloud"]["api_key"] = "test-private-key"
    state = await service.state()
    assert state["config"]["cloud"]["api_key"] == ""
    assert state["credentials"]["cloud"]
    assert "test-private-key" not in json.dumps(state)
    draft = state["config"]
    draft["cloud"]["speed"] = 1.5
    await service.save_settings(draft, [])
    assert config["cloud"]["api_key"] == "test-private-key"
    invalid = copy.deepcopy(draft)
    invalid["cloud"]["speed"] = 3
    with pytest.raises(BreezeError):
        await service.save_settings(invalid, [])
    assert config["cloud"]["speed"] == 1.5
    await service.save_settings(draft, ["cloud"])
    assert not config["cloud"]["api_key"]


def test_audio_upload_roundtrip_and_pruning(tmp_path, wav_audio):
    store = AudioStore(tmp_path)
    item = store.upload(wav_audio, "../../reference.wav")
    assert item["name"] == "reference.wav"
    assert item["duration"] == 4
    item["ref_text"] = "准确文字稿"
    store.items[item["id"]] = item
    store.persist()
    loaded = AudioStore(tmp_path)
    assert loaded.items[item["id"]]["ref_text"] == "准确文字稿"
    for index in range(4):
        loaded.add(wav_audio, "wav", "output", str(index))
    loaded.prune(2)
    assert len(loaded.items) == 3
    assert loaded.path(item["id"]).exists()
    assert len(list(tmp_path.glob("*.wav"))) == 3
    loaded.remove(item["id"])
    assert len(loaded.items) == 2


@pytest.mark.parametrize(
    "audio_id", ["../private", "C:\\private.wav", "a" * 32, "", None]
)
def test_store_rejects_arbitrary_paths(tmp_path, audio_id):
    with pytest.raises(BreezeError):
        AudioStore(tmp_path).path(audio_id)


def test_corrupt_store_preserves_original(tmp_path):
    index = tmp_path / "audio-index.json"
    index.write_text("invalid", encoding="utf-8")
    with pytest.raises(BreezeError):
        AudioStore(tmp_path)
    assert index.read_text(encoding="utf-8") == "invalid"


@pytest.mark.parametrize("data", [b"", b"not an audio file", b"RIFFinvalidWAVE"])
def test_invalid_recordings(tmp_path, data):
    with pytest.raises(BreezeError):
        AudioStore(tmp_path).upload(data, "recording.wav")
    assert not list(tmp_path.glob("*.wav"))


def test_pcm_is_a_real_wave_container():
    data, extension = playable_audio(b"\0\0" * 100, "pcm", 16000)
    with wave.open(io.BytesIO(data)) as audio:
        assert audio.getnframes() == 100
        assert audio.getframerate() == 16000
    assert extension == "wav"


@pytest.mark.parametrize(
    ("name", "template"),
    [
        ("speech_prompt", "{not_a_variable}"),
        ("speech_prompt", "{passage}"),
        ("speech_prompt", "{output_protocol}"),
        ("tone_prompt", "{missing}"),
    ],
)
def test_prompt_templates_reject_unknown_or_out_of_scope_variables(
    config, name, template
):
    config["behavior"][name] = template
    with pytest.raises(BreezeError, match="变量"):
        validate_config(config)


def test_prompt_variables_keep_literal_json_and_round_trip(config):
    config["behavior"]["tone_prompt"] = (
        '{platform_rules}\n{audio_tags}\n{output_protocol}\n{"instruction":""}'
    )
    assert (
        validate_config(config)["behavior"]["tone_prompt"]
        == config["behavior"]["tone_prompt"]
    )
