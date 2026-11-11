"""The trunk the setup script creates must match the one that is live (no username header unless asked)."""

from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "setup_telnyx_trunk.py"


@pytest.fixture(scope="module")
def setup() -> ModuleType:
    spec = importlib.util.spec_from_file_location("setup_telnyx_trunk", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def args(**kw: object) -> Namespace:
    base = {
        "name": "Telnyx-NoHeader",
        "phone": "+14706000407",
        "sip_host": "sip.telnyx.com",
        "sip_user": "user",
        "sip_pass": "pass",
        "destination_country": "us",
        "username_header": False,
    }
    return Namespace(**{**base, **kw})


def test_the_default_trunk_matches_the_live_one(setup: ModuleType) -> None:
    trunk = setup.build_trunk(args())
    assert (trunk.name, trunk.address, list(trunk.numbers), trunk.destination_country) == (
        "Telnyx-NoHeader", "sip.telnyx.com", ["+14706000407"], "us"
    )
    assert (trunk.auth_username, trunk.auth_password) == ("user", "pass")
    assert dict(trunk.headers) == {} and dict(trunk.headers_to_attributes) == {}


def test_the_username_header_is_opt_in(setup: ModuleType) -> None:
    trunk = setup.build_trunk(args(username_header=True))
    assert dict(trunk.headers) == {"X-Telnyx-Username": "user"}
    assert dict(trunk.headers_to_attributes) == {"X-Telnyx-Username": "user"}
