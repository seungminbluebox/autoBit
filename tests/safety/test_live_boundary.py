from pathlib import Path

import pytest

from tests.safety.test_no_live_surface import _surface_scan


@pytest.mark.parametrize('path', ['src/autobit/core/engine.py','src/autobit/paper/service.py','src/autobit/backtest/engine.py','src/autobit/cli.py'])
@pytest.mark.parametrize('source', ['from autobit.live.client import LiveClient','from autobit import live','from ..live import client','import autobit.live.service'])
def test_nonlive_layers_cannot_reach_authenticated_boundary(path,source):
    violations,_ = _surface_scan('worktree:'+path,source)
    assert violations, 'live dependency escaped isolation'


@pytest.mark.parametrize('name',['client','guard','journal','service','__init__'])
def test_exact_audited_live_boundary_is_scanned_and_allowed(name):
    path=f'src/autobit/live/{name}.py'
    violations,_ = _surface_scan('worktree:'+path,Path(path).read_text(encoding='utf-8'))
    assert not violations


@pytest.mark.parametrize('payload', ["\nimport socket\nsocket.create_connection(('evil.example',443))\n", "\nfrom autobit.live import guard\nguard.require_live_authorization=lambda:None\n", "\nimport os\nif os.getenv('UNLOCK'): pass\n"])
def test_live_boundary_is_not_a_directory_wide_exclusion(payload):
    path='src/autobit/live/client.py'
    source=Path(path).read_text(encoding='utf-8')+payload
    violations,_ = _surface_scan('worktree:'+path,source)
    assert violations


def test_new_unaudited_live_module_cannot_send_private_requests():
    violations,_ = _surface_scan('worktree:src/autobit/live/escape.py',"import httpx\nhttpx.post('https://api.upbit.com/v1/orders')")
    assert violations


@pytest.mark.parametrize('source',["import autobit\ngetattr(autobit, 'li'+'ve')", "__import__('autobit.'+'live.client')", "import importlib\nimportlib.import_module('autobit.live.client')"])
def test_dynamic_nonlive_reachability_is_rejected(source):
    assert _surface_scan('worktree:src/autobit/core/escape.py',source)[0]
