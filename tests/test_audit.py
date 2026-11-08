"""Regression tests for invalid input, storage failures, and safe diagnostics."""

import copy
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from breezetts_test.client import BreezeClient
from breezetts_test.settings import BreezeError, api_root, validate_config
from breezetts_test.storage import AudioStore, playable_audio


@pytest.mark.parametrize(
    "value", [None, [], 42, "http://host\\private", "http://" + "a" * 2050]
)
def test_invalid_service_roots_are_safe(value):
    with pytest.raises(BreezeError):
        api_root(value)


@pytest.mark.parametrize(
    "group,key,value",
    [
        ("cloud", "speed", 10**1000),
        ("local", "cfg_scale", 10**1000),
        ("cloud", "voice_id", ".."),
        ("cloud", "voice_id", "voice\nsecret"),
    ],
)
def test_extreme_config_values_raise_validation(config, group, key, value):
    config[group][key] = value
    with pytest.raises(BreezeError):
        validate_config(config)


@pytest.mark.parametrize("value", [None, False, 0, [], {}])
async def test_invalid_key_types_are_not_treated_as_blank(service, config, value):
    draft = copy.deepcopy(dict(config))
    draft["local"]["api_key"] = value
    with pytest.raises(BreezeError):
        await service.save_settings(draft, [])
    assert config["local"]["api_key"] == ""


async def test_invalid_clear_key_list(service, config):
    with pytest.raises(BreezeError):
        await service.save_settings(copy.deepcopy(dict(config)), [{}])


def test_delete_failure_restores_file_and_index(tmp_path, wav_audio, monkeypatch):
    store = AudioStore(tmp_path)
    item = store.upload(wav_audio, "voice.wav")
    original = Path.unlink

    def fail_staged(path, *args, **kwargs):
        if path.suffix == ".delete":
            raise PermissionError("test-private-message")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_staged)
    with pytest.raises(PermissionError):
        store.remove(item["id"])
    loaded = AudioStore(tmp_path)
    assert loaded.path(item["id"]).read_bytes() == wav_audio
    assert not list(tmp_path.glob("*.delete"))


def test_failed_audio_write_removes_partial_file(tmp_path, wav_audio, monkeypatch):
    store = AudioStore(tmp_path)
    original = Path.write_bytes

    def partial_write(path, data):
        original(path, data[:10])
        raise OSError("test-private-message")

    monkeypatch.setattr(Path, "write_bytes", partial_write)
    with pytest.raises(OSError):
        store.add(wav_audio, "wav", "output", "voice")
    assert not store.items and not list(tmp_path.glob("*.wav"))


@pytest.mark.parametrize(
    "key,value",
    [
        ("kind", "unknown"),
        ("created", float("nan")),
        ("size", -1),
        ("ref_text", []),
        ("name", None),
    ],
)
def test_corrupt_metadata_is_preserved(tmp_path, wav_audio, key, value):
    store = AudioStore(tmp_path)
    item = store.upload(wav_audio, "voice.wav")
    store.items[item["id"]][key] = value
    store.persist()
    raw = store.index.read_bytes()
    with pytest.raises(BreezeError):
        AudioStore(tmp_path)
    assert store.index.read_bytes() == raw
    assert store.path(item["id"]).exists()


def test_compressed_audio_allocation_is_bounded(tmp_path, monkeypatch):
    import breezetts_test.storage as storage

    audio = MagicMock(format="FLAC", frames=9_000_000, samplerate=100_000, channels=2)
    context = MagicMock()
    context.__enter__.return_value = audio
    monkeypatch.setattr(storage.sf, "SoundFile", lambda _source: context)
    with pytest.raises(BreezeError, match="大小上限"):
        AudioStore(tmp_path).upload(b"compressed-content", "voice.flac")
    audio.read.assert_not_called()


@pytest.mark.parametrize(
    "data,fmt",
    [(b"RIFF\0\0\0\0WAVE", "wav"), (b"ID3invalid", "mp3"), (b"fLaCinvalid", "flac")],
)
def test_malformed_audio_container_is_rejected(data, fmt):
    with pytest.raises(BreezeError):
        playable_audio(data, fmt)


async def test_completed_audio_survives_cleanup_failure(
    service, config, server, monkeypatch, caplog
):
    async def handle(_request):
        return web.Response(body=b"\0\0" * 100, content_type="audio/pcm")

    config["local"].update(base_url=await server(handle), mode="plain")
    monkeypatch.setattr(
        service.store,
        "prune",
        MagicMock(side_effect=[None, None, PermissionError("private-key-test")]),
    )
    item = await service.synthesize("测试")
    assert service.store.path(item["id"]).exists()
    assert "cleanup failed" in caplog.text and "private-key-test" not in caplog.text


async def test_failed_job_logs_original_text_and_safe_error(
    service, config, server, caplog
):
    caplog.set_level("INFO")

    async def handle(_request):
        return web.Response(status=401, text="private-key-test")

    config["local"].update(
        base_url=await server(handle), mode="plain", api_key="private-key-test"
    )
    job = await service.submit("synthesize", {"text": "private-input-text"})
    task = service.tasks[job["id"]]
    await task
    assert service.jobs[job["id"]]["status"] == "failed"
    assert "upstream_http_401" in caplog.text
    assert "private-key-test" not in caplog.text
    assert "private-input-text" in caplog.text
    assert "private-key-test" not in json.dumps(service.jobs)


async def test_closed_client_cannot_reopen():
    client = BreezeClient()
    await client.start()
    await client.close()
    with pytest.raises(BreezeError):
        await client.start()
    assert client.session.closed


async def test_malformed_async_job_status_is_safe(config, service, server):
    async def handle(request):
        if request.method == "POST":
            return web.json_response({"generation_job_id": "job_test"}, status=202)
        return web.json_response(
            {"generation_job_id": "job_test", "status": {"private": "value"}}
        )

    config["backend"] = "cloud"
    config["cloud"].update(
        base_url=await server(handle),
        api_key="test-key",
        voice_id="voc_test",
        delivery="async",
    )
    with pytest.raises(BreezeError, match="后台任务"):
        await service.synthesize("测试")


async def test_invalid_voice_catalog_id_is_safe(config, service, server):
    async def handle(_request):
        return web.json_response(
            {"voices": [{"voice_id": "..", "name": "Voice"}], "has_more": False}
        )

    config["cloud"]["base_url"] = await server(handle)
    with pytest.raises(BreezeError):
        await service.client.voices(config["cloud"])


async def test_clone_preview_identity_survives_audition_failure(
    config, service, server, wav_audio
):
    async def handle(request):
        if request.path.endswith("/clone"):
            return web.json_response(
                {"generated_voice_id": "preview_paid", "requires_verification": False}
            )
        return web.Response(status=503, text="private-service-details")

    config["backend"] = "cloud"
    config["cloud"].update(base_url=await server(handle), api_key="test-key")
    reference = service.store.upload(wav_audio, "reference.wav")
    result = await service.create_preview(
        "clone",
        {"name": "Voice", "language_code": "zh", "reference_id": reference["id"]},
        service.settings(),
    )
    assert result["preview_id"] == "preview_paid" and result["audio_unavailable"]
    assert "audio" not in result


async def test_persistence_failure_keeps_saved_config(
    service, config, monkeypatch, caplog
):
    original = copy.deepcopy(dict(config))
    draft = copy.deepcopy(original)
    draft["behavior"]["auto_tts"] = True
    monkeypatch.setattr(
        config, "save_config", MagicMock(side_effect=OSError("private-key-test"))
    )
    with pytest.raises(BreezeError) as error:
        await service.save_settings(draft, [])
    assert dict(config) == original
    assert (
        "private-key-test" not in str(error.value)
        and "private-key-test" not in caplog.text
    )


async def test_local_cfg_requires_an_instruction(config):
    config["local"].update(mode="plain", cfg_scale=4)
    client = BreezeClient()
    with pytest.raises(BreezeError, match="CFG"):
        await client.synthesize(config, "测试")
    assert client.session is None


def test_empty_upload_name_cannot_poison_index(tmp_path, wav_audio):
    store = AudioStore(tmp_path)
    with pytest.raises(BreezeError):
        store.upload(wav_audio, "/")
    assert not store.items
    assert not list(tmp_path.glob("*.wav"))


@pytest.mark.parametrize("encoded", ["not-base64", "UklGRmludmFsaWRXQVZF"])
async def test_design_preview_identity_survives_invalid_audio(
    config, service, server, encoded
):
    async def handle(_request):
        return web.json_response(
            {
                "previews": [
                    {
                        "generated_voice_id": "preview_design_paid",
                        "audio_base_64": encoded,
                    }
                ]
            }
        )

    config["backend"] = "cloud"
    config["cloud"].update(base_url=await server(handle), api_key="test-key")
    result = await service.create_preview(
        "design",
        {
            "name": "Voice",
            "language_code": "zh",
            "description": "A warm voice",
            "text": "Preview text",
        },
        service.settings(),
    )
    assert result["preview_id"] == "preview_design_paid" and result["audio_unavailable"]


@pytest.mark.parametrize("voice_id", [" voice", "voice\nsecret", "voice\x7f", ".."])
async def test_saved_preview_rejects_malformed_voice_identity(
    service, config, server, voice_id
):
    async def upstream(request):
        assert request.path == "/v1/voice-previews/preview_paid/save"
        return web.json_response({"voice_id": voice_id})

    config["cloud"].update(base_url=await server(upstream), api_key="test-key")
    with pytest.raises(BreezeError):
        await service.save_preview(
            {"preview_id": "preview_paid", "name": "Voice", "language_code": "zh"}
        )
