from dataclasses import asdict, replace
from datetime import datetime, timezone
import pytest
from autobit.backtest.engine import BacktestConfig, BacktestResult, EquityPoint, OrderRecord, TradeRecord
from autobit.domain.models import OrderStatus
from tools.research.run_experiment import candidate_config, save_cell, load_cell, prepare_output


def test_candidates_change_only_the_registered_exit_field():
    base = BacktestConfig()
    for name, field, value in [('trail_4atr', 'trailing_atr_mult', 4.0), ('activate_3r', 'profit_activation_r', 3.0)]:
        candidate = candidate_config(base, name)
        assert candidate == replace(base, strategy=replace(base.strategy, **{field: value}))
        assert candidate.risk.hard_drawdown == 0.15
    with pytest.raises(ValueError):
        candidate_config(base, 'unregistered')


def test_cell_roundtrip_preserves_fills_times_and_tiny_quantities(tmp_path):
    t = datetime(2022, 1, 1, tzinfo=timezone.utc)
    result = BacktestResult((EquityPoint(t,100.),),
        (OrderRecord('1',OrderStatus.COMPLETED,'BUY',1e-8,1e-8,0.,t,t,t,100.,0.001,0.002,90.,'ENTRY'),),
        (TradeRecord(t,t,1e-8,100.,110.,1e-7,9e-8,1e-8,'TRAILING_STOP'),),100.1,0.001,0.002)
    p=tmp_path/'cell.json.gz'
    save_cell(p,result)
    assert load_cell(p)==result
    with pytest.raises(FileExistsError):
        save_cell(p,result)
    p.write_bytes(p.read_bytes()+b'tamper')
    with pytest.raises(ValueError, match='checksum'):
        load_cell(p)


def test_resume_requires_matching_manifest_and_never_adopts_unknown_folder(tmp_path):
    p=tmp_path/'run'
    prepare_output(p, {'data':'abc'}, resume=False)
    prepare_output(p, {'data':'abc'}, resume=True)
    with pytest.raises(ValueError, match='manifest'):
        prepare_output(p, {'data':'changed'}, resume=True)
    with pytest.raises(FileExistsError):
        prepare_output(p, {'data':'abc'}, resume=False)
    unknown=tmp_path/'unknown'; unknown.mkdir(); (unknown/'keep').write_text('mine')
    with pytest.raises(ValueError, match='manifest'):
        prepare_output(unknown,{},resume=True)
    assert (unknown/'keep').read_text()=='mine'


def test_cell_cannot_be_reused_for_another_candidate_or_cost(tmp_path):
    result=BacktestResult((),(),(),100.,0.,0.)
    path=tmp_path/'cell.json.gz'
    save_cell(path,result,identity={'candidate':'baseline','cost':'baseline'})
    with pytest.raises(ValueError, match='identity'):
        load_cell(path,identity={'candidate':'trail_4atr','cost':'baseline'})


def test_research_refuses_imports_from_a_different_checkout(tmp_path):
    from tools.research.run_experiment import validate_source_imports
    with pytest.raises(ValueError, match='checkout'):
        validate_source_imports(tmp_path)


def test_source_check_accepts_our_real_namespace_packages():
    from pathlib import Path
    from tools.research.run_experiment import validate_source_imports
    validate_source_imports(Path(__file__).resolve().parents[2])


def test_resume_rejects_incomplete_reports_and_changed_summaries(tmp_path):
    from tools.research.run_experiment import _report_bundle, _write_or_verify
    folder=tmp_path/'report'; folder.mkdir()
    (folder/'summary.json').write_text('{}')
    with pytest.raises(ValueError,match='incomplete report'):
        _report_bundle(folder,{'candidate':'baseline'})
    p=tmp_path/'summary.json'
    _write_or_verify(p,{'equity':100})
    with pytest.raises(ValueError,match='stored summary'):
        _write_or_verify(p,{'equity':110})


def test_research_request_keeps_phase_boundaries_and_ignores_future_prices():
    import pandas as pd
    from tools.research.run_experiment import research_request
    from autobit.validation.splits import build_rolling_folds
    from autobit.validation.models import WalkForwardConfig
    from autobit.validation.trials import registered_cost_scenarios
    index=pd.date_range('2020-01-01',periods=6000,freq='4h',tz='UTC')
    frame=pd.DataFrame({'open':100.,'high':101.,'low':99.,'close':100.,'volume':10.},index=index)
    fold=tuple(build_rolling_folds(index,WalkForwardConfig()))[0]
    cost=registered_cost_scenarios()[1]
    request=research_request(frame,fold,cost,'OOS','trail_4atr')
    changed=frame.copy(); changed.loc[changed.index>=fold.test_end,'close']=99999.
    replay=research_request(changed,fold,cost,'OOS','trail_4atr')
    pd.testing.assert_frame_equal(request.frame,replay.frame)
    assert request.frame.index.min()>=fold.test_start
    assert request.frame.index.max()<fold.test_end
    assert request.config.strategy.trailing_atr_mult==4.0
    assert request.config.force_liquidate_at_end
    assert request.config.risk.hard_drawdown==0.15


def test_provenance_uses_only_fixed_read_only_commands(monkeypatch, tmp_path):
    import importlib.metadata
    import subprocess
    from tools.research.provenance import read_provenance

    calls = []
    responses = iter((b'a.py\0b.py\0', b'', b'abc123\n'))
    def capture(argv, **kwargs):
        calls.append((argv, kwargs))
        return next(responses)
    packages = []
    def version(name):
        packages.append(name)
        return '1.0'
    monkeypatch.setattr(subprocess, 'check_output', capture)
    monkeypatch.setattr(importlib.metadata, 'version', version)
    result = read_provenance(tmp_path)
    assert calls == [
        (['git', '--no-optional-locks', 'ls-files', '-z'], {'cwd': tmp_path}),
        (['git', '--no-optional-locks', 'status', '--porcelain', '--untracked-files=no'], {'cwd': tmp_path}),
        (['git', '--no-optional-locks', 'rev-parse', 'HEAD'], {'cwd': tmp_path}),
    ]
    assert result == {'tracked': ['a.py', 'b.py'], 'commit': 'abc123',
                      'dependencies': {p: '1.0' for p in packages}}
    assert packages == ['backtrader', 'pandas', 'numpy', 'pandas-ta', 'scipy', 'httpx']


def test_provenance_refuses_dirty_or_failed_git_reads(monkeypatch, tmp_path):
    import subprocess
    from tools.research.provenance import read_provenance

    responses = iter((b'a.py\0', b' M a.py\n'))
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: next(responses))
    with pytest.raises(ValueError, match='commit tracked changes'):
        read_provenance(tmp_path)
    def failed(*a, **k):
        raise subprocess.CalledProcessError(1, 'git')
    monkeypatch.setattr(subprocess, 'check_output', failed)
    with pytest.raises(subprocess.CalledProcessError):
        read_provenance(tmp_path)


def test_source_check_rejects_research_metadata_from_another_checkout(monkeypatch, tmp_path):
    from pathlib import Path
    import sys
    from types import SimpleNamespace
    from tools.research.run_experiment import validate_source_imports
    monkeypatch.setitem(sys.modules, 'tools.research.provenance',
                        SimpleNamespace(__file__=str(tmp_path / 'provenance.py')))
    with pytest.raises(ValueError, match='different checkout'):
        validate_source_imports(Path(__file__).resolve().parents[2])
