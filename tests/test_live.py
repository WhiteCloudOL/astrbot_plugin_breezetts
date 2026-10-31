"""Opt-in model integration tests; credentials stay in environment variables."""

import io
import logging
import os
import wave
from pathlib import Path

import pytest
from breezetts_test.service import BreezeService
from breezetts_test.settings import BreezeError


@pytest.mark.skipif(
    not os.environ.get("BREEZE_LIVE_LOCAL_URL"),
    reason="Set BREEZE_LIVE_LOCAL_URL to run a real local model test",
)
async def test_live_local_model(config, tmp_path):
    key = os.environ.get("BREEZE_LIVE_LOCAL_KEY", "")
    key_file = os.environ.get("BREEZE_LIVE_LOCAL_KEY_FILE")
    if key_file:
        key = Path(key_file).read_text(encoding="utf-8").strip()
    config["local"].update(
        base_url=os.environ["BREEZE_LIVE_LOCAL_URL"], api_key=key, mode="plain"
    )
    service = BreezeService(
        config, tmp_path / "live-audio", logging.getLogger("breeze-live")
    )
    try:
        await service.start()
        health = await service.connection()
        assert health["health"]["status"] == "ok"
        reference = os.environ.get("BREEZE_LIVE_REFERENCE")
        if reference:
            record = service.store.upload(
                Path(reference).read_bytes(), "live-reference.wav"
            )
            service.store.items[record["id"]]["ref_text"] = os.environ[
                "BREEZE_LIVE_REFERENCE_TEXT"
            ]
            config["local"].update(reference_id=record["id"], mode="reference")
        item = await service.synthesize("你好，这是 BreezeTTS 插件的一次真实合成测试。")
        with wave.open(
            io.BytesIO(service.store.path(item["id"]).read_bytes())
        ) as result:
            assert result.getframerate() == 24000 and result.getnframes() > 2400
        if key:
            assert key not in str(await service.state())
            config["local"]["api_key"] = ""
            with pytest.raises(BreezeError, match="401"):
                await service.connection()
    finally:
        await service.close()
