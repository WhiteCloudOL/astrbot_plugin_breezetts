"""Verify retention boundaries around queued platform sends."""

import copy
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from breezetts_test.settings import BreezeError, validate_config


@pytest.mark.parametrize(
    "field,value",
    [
        ("trigger_probability", -1),
        ("trigger_probability", 101),
        ("trigger_probability", True),
        ("trigger_probability", 0.5),
        ("history_max_mb", 0),
        ("history_max_mb", 4097),
        ("history_max_mb", True),
    ],
)
def test_limits_validate_at_configuration_boundary(config, field, value):
    config["behavior"][field] = value
    with pytest.raises(BreezeError):
        validate_config(config)


async def test_delivery_reservation_survives_count_cleanup(service, config, server):
    async def upstream(_request):
        return web.Response(body=b"\x01\x00" * 100, content_type="audio/pcm")

    config["local"].update(base_url=await server(upstream), mode="plain")
    config["behavior"]["history_limit"] = 1
    first = await service.synthesize("First", for_delivery=True)
    second = await service.synthesize("Second", for_delivery=True)
    assert service.store.path(first["id"]).is_file()
    assert service.store.path(second["id"]).is_file()
    await service.release_audio({first["id"], second["id"]})
    assert set(service.store.items) == {second["id"]}
    assert service.delivery_audio == {}


async def test_expired_reservations_and_saved_limits_cleanup(service, config, server):
    async def upstream(_request):
        return web.Response(body=b"\x01\x00" * 100, content_type="audio/pcm")

    config["local"].update(base_url=await server(upstream), mode="plain")
    first = await service.synthesize("First", for_delivery=True)
    second = await service.synthesize("Second")
    service.delivery_audio[first["id"]] = time.monotonic() - 1
    draft = copy.deepcopy(dict(config))
    draft["behavior"]["history_limit"] = 1
    await service.save_settings(draft, [])
    assert set(service.store.items) == {second["id"]}
    assert service.delivery_audio == {}


async def test_capacity_admission_protects_waiting_delivery(
    service, config, wav_audio, monkeypatch
):
    config["local"]["mode"] = "plain"
    config["behavior"]["history_max_mb"] = 64
    item = service.store.add(wav_audio, "wav", "output", "Reserved")
    service.store.items[item["id"]]["size"] = 64 * 1024 * 1024
    service.delivery_audio[item["id"]] = time.monotonic() + 3600
    monkeypatch.setattr(
        service.client, "synthesize", AsyncMock(return_value=(wav_audio, "wav"))
    )
    with pytest.raises(BreezeError, match="容量"):
        await service.synthesize("New")
    assert set(service.store.items) == {item["id"]}
    assert service.store.path(item["id"]).exists()


async def test_failed_cache_cleanup_stops_new_generation(service, config, monkeypatch):
    config["local"]["mode"] = "plain"
    synthesize = AsyncMock()
    monkeypatch.setattr(service.client, "synthesize", synthesize)
    monkeypatch.setattr(
        service.store,
        "prune",
        MagicMock(side_effect=PermissionError("private-key-test")),
    )
    with pytest.raises(BreezeError, match="成品清理失败"):
        await service.synthesize("New")
    synthesize.assert_not_awaited()
    assert service.active == 0
