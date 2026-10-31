"""Isolated protocol fixtures without a running model or saved credentials."""

import copy
import importlib.util
import io
import logging
import os
import sys
import tempfile
import wave
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parents[1]
HOST_ROOT = tempfile.TemporaryDirectory(
    prefix="breezetts-host-tests-", ignore_cleanup_errors=True
)
os.environ["ASTRBOT_ROOT"] = HOST_ROOT.name
spec = importlib.util.spec_from_file_location(
    "breezetts_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

from breezetts_test.service import BreezeService  # noqa: E402
from breezetts_test.settings import DEFAULTS  # noqa: E402


def pytest_unconfigure(config):
    HOST_ROOT.cleanup()


class SavedConfig(dict):
    """Provide the injected configuration persistence contract for unit tests."""

    def save_config(self):
        self.saved = copy.deepcopy(dict(self))


@pytest.fixture
def config():
    return SavedConfig(copy.deepcopy(DEFAULTS))


@pytest.fixture
def wav_audio():
    target = io.BytesIO()
    with wave.open(target, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x01\x00" * 24000 * 4)
    return target.getvalue()


@pytest.fixture
async def server():
    servers = []

    async def start(handler):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        instance = TestServer(app)
        await instance.start_server()
        servers.append(instance)
        return str(instance.make_url("/")).rstrip("/")

    yield start
    for instance in servers:
        await instance.close()


@pytest.fixture
async def service(config, tmp_path):
    instance = BreezeService(
        config, tmp_path / "audio", logging.getLogger("breeze-tests")
    )
    await instance.start()
    yield instance
    await instance.close()
