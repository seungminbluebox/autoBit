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
