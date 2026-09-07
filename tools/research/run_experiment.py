"""Fixed exploratory exit-policy comparisons using the existing execution engine.

This is an offline research command, not the official nine-trial PASS evaluator.
Private validation helpers are deliberately reused and pinned by the source snapshot.
"""
import argparse
from dataclasses import replace
from datetime import datetime
import gzip
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import zipfile
from uuid import uuid4

from autobit.backtest.engine import BacktestConfig, BacktestResult, EquityPoint, OrderRecord, TradeRecord, run_backtest
from autobit.backtest.analyzers import calculate_metrics
from autobit.backtest.benchmark import run_buy_and_hold
from autobit.cli import _read_walk_forward_csv, _load_quality_provenance
from autobit.domain.models import OrderStatus
from autobit.indicators.trend import compute_trend_indicators
from autobit.reporting.reports import _json_value, write_report_bundle
from autobit.validation.models import WalkForwardConfig, WalkForwardRun
from autobit.validation.splits import build_rolling_folds
from autobit.validation.trials import registered_trials, registered_cost_scenarios
from autobit.validation.runner import _request_for_phase, _validate_backtest_result, _stitch_oos

CANDIDATES = ('baseline', 'trail_4atr', 'activate_3r')


def candidate_config(base, name):
    changes = {'baseline': {}, 'trail_4atr': {'trailing_atr_mult': 4.0},
               'activate_3r': {'profit_activation_r': 3.0}}
    if name not in changes:
        raise ValueError('unregistered candidate')
    return replace(base, strategy=replace(base.strategy, **changes[name]))


def research_request(frame, fold, cost, phase, name):
    request=_request_for_phase(frame,fold=fold,trial=registered_trials()[0],cost=cost,phase=phase)
    return replace(request,config=candidate_config(request.config,name))


def _bytes(value):
    return (json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True,
                       allow_nan=False, indent=2) + '\n').encode('utf-8')


def _hash(payload):
    return hashlib.sha256(payload).hexdigest()


def _write(path, value):
    with path.open('xb') as stream:
        stream.write(_bytes(value))


def prepare_output(output, manifest, resume=False):
    expected = _bytes(manifest)
    if output.exists():
        if not resume:
            raise FileExistsError(output)
        record = output / 'experiment.json'
        if not record.is_file() or record.read_bytes() != expected:
            raise ValueError('resume manifest mismatch')
    else:
        output.mkdir(parents=True)
        (output / 'experiment.json').write_bytes(expected)


def validate_source_imports(root):
    expected = (root / 'src').resolve()
    for name, module in tuple(sys.modules.items()):
        if name == 'autobit' or name.startswith('autobit.'):
            origin = getattr(module, '__file__', None)
            origins = [origin] if origin else list(getattr(module, '__path__', ()))
            if not origins or any(not Path(p).resolve().is_relative_to(expected) for p in origins):
                raise ValueError('autobit import came from a different checkout; set PYTHONPATH to this checkout/src')


def save_cell(path, result, identity=None):
    payload = gzip.compress(_bytes({'identity':identity,'result':result}), mtime=0)
    with path.open('xb') as stream:
        stream.write(payload)
    with path.with_suffix(path.suffix + '.sha256').open('x', encoding='ascii') as stream:
        stream.write(_hash(payload))


def load_cell(path, identity=None):
    payload = path.read_bytes()
    checksum = path.with_suffix(path.suffix + '.sha256')
    if not checksum.is_file() or _hash(payload) != checksum.read_text(encoding='ascii'):
        raise ValueError('cell checksum mismatch; preserve and investigate')
    envelope = json.loads(gzip.decompress(payload))
    if envelope['identity'] != _json_value(identity):
        raise ValueError('cell identity mismatch')
    raw = envelope['result']
    for item in raw['equity_curve']:
        item['timestamp'] = datetime.fromisoformat(item['timestamp'])
    for item in raw['orders']:
        item['status'] = OrderStatus(item['status'])
        for field in ('occurred_at', 'signal_time', 'fill_time'):
            if item[field] is not None:
                item[field] = datetime.fromisoformat(item[field])
    for item in raw['trades']:
        for field in ('entry_time', 'exit_time'):
            item[field] = datetime.fromisoformat(item[field])
    return BacktestResult(
        tuple(EquityPoint(**v) for v in raw['equity_curve']),
        tuple(OrderRecord(**v) for v in raw['orders']),
        tuple(TradeRecord(**v) for v in raw['trades']),
        raw['final_equity'], raw['total_fees'], raw['total_slippage'])


def _metrics(result):
    return calculate_metrics(equity_curve=result.equity_curve, trades=result.trades,
                             orders=result.orders, total_fees=result.total_fees,
                             total_slippage=result.total_slippage)


def _cell(path, frame, config, request=None, identity=None):
    result = load_cell(path,identity) if path.exists() else run_backtest(frame, config)
    if request is not None:
        _validate_backtest_result(result, request)
    if not path.exists():
        save_cell(path, result,identity)
    return result


def _write_or_verify(path, value):
    if path.exists():
        if path.read_bytes() != _bytes(value):
            raise ValueError('stored summary differs from validated cells')
    else:
        _write(path,value)


def _report_bundle(path, identity, **kwargs):
    marker=path/'complete.json'
    if path.exists():
        if not marker.is_file():
            raise ValueError('incomplete report bundle; preserve and investigate')
        record=json.loads(marker.read_bytes())
        if record['identity'] != _json_value(identity):
            raise ValueError('report identity mismatch')
        names={p.name for p in path.iterdir() if p.name!='complete.json'}
        if names!=set(record['files']) or any(_hash((path/n).read_bytes())!=h for n,h in record['files'].items()):
            raise ValueError('report checksum mismatch')
        return
    # Incomplete staging directories are preserved; a later resume can publish a fresh one.
    staging=path.with_name(path.name+'.staging-'+uuid4().hex)
    write_report_bundle(staging,**kwargs)
    _write(staging/'complete.json',{'identity':identity,'files':{
        p.name:_hash(p.read_bytes()) for p in staging.iterdir() if p.is_file()}})
    staging.rename(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    validate_source_imports(root)
    frame = _read_walk_forward_csv(args.input)
    quality = _load_quality_provenance(args.input, len(frame))
    folds = tuple(build_rolling_folds(frame.index, WalkForwardConfig()))
    if len(folds) < 2:
        raise ValueError('at least two complete folds required')
    costs = tuple(c for c in registered_cost_scenarios() if c.cost_id in ('baseline','stress_20bps'))
    trial = registered_trials()[0]
    tracked = subprocess.check_output(['git','ls-files','-z'],cwd=root).decode().split('\0')
    snapshot = {name: _hash((root/name).read_bytes()) for name in tracked if name}
    dirty = subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=root).decode()
    if dirty:
        raise ValueError('commit tracked changes before recording an experiment')
    manifest = {
        'schema': 2, 'research_id': 'R20260908', 'evaluation': 'EXPLORATORY_NOT_OFFICIAL_PASS',
        'commit': subprocess.check_output(['git','rev-parse','HEAD'],cwd=root).decode().strip(),
        'data_sha256': _hash(args.input.read_bytes()), 'source_files': snapshot,
        'quality_sha256': _hash(args.input.with_name('quality.json').read_bytes()),
        'python': sys.version,
        'dependencies': {p:importlib.metadata.version(p) for p in ('backtrader','pandas','numpy','pandas-ta','scipy','httpx')},
        'configs': {n:candidate_config(BacktestConfig(),n) for n in CANDIDATES},
        'costs': costs, 'split': WalkForwardConfig(),
        'folds': [{'id':f.fold_id,'train_start':f.train_start.isoformat(),'train_end':f.train_end.isoformat(),
                   'test_start':f.test_start.isoformat(),'test_end':f.test_end.isoformat()} for f in folds],
        'first_row': frame.index[0].isoformat(), 'last_row': frame.index[-1].isoformat(),
        'prior_exposure': 'Seven years and previous OOS results have already been inspected; not untouched holdout.',
    }
    prepare_output(args.output, manifest, args.resume)
    experiment_hash=_hash(_bytes(manifest))
    archive = args.output/'source.zip'
    if not archive.exists():
        with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as bundle:
            for name in snapshot:
                bundle.write(root/name,name)
    with zipfile.ZipFile(archive) as bundle:
        if set(bundle.namelist()) != set(snapshot) or any(_hash(bundle.read(n)) != h for n,h in snapshot.items()):
            raise ValueError('source snapshot mismatch')
    data_copy = args.output/'processed.csv.gz'
    if not data_copy.exists():
        with data_copy.open('xb') as stream:
            stream.write(gzip.compress(args.input.read_bytes(),mtime=0))
    if _hash(gzip.decompress(data_copy.read_bytes())) != manifest['data_sha256']:
        raise ValueError('data snapshot mismatch')
    quality_copy=args.output/'quality.json'
    if not quality_copy.exists():
        with quality_copy.open('xb') as stream:
            stream.write(args.input.with_name('quality.json').read_bytes())
    if _hash(quality_copy.read_bytes()) != manifest['quality_sha256']:
        raise ValueError('quality snapshot mismatch')
    rows=[]
    for name in CANDIDATES:
        folder=args.output/name; folder.mkdir(exist_ok=True)
        for cost in costs:
            config=candidate_config(BacktestConfig(),name)
            config=replace(config,costs=replace(config.costs,fee_rate=cost.fee_rate,slippage_rate=cost.slippage_rate))
            enriched=compute_trend_indicators(frame,config.strategy)
            full_identity={'experiment':experiment_hash,'candidate':name,'cost':cost.cost_id,'phase':'FULL','config':config}
            full=_cell(folder/f'full-{cost.cost_id}.json.gz',enriched,config,identity=full_identity)
            full_metrics=_metrics(full)
            report_dir=folder/f'full-{cost.cost_id}'
            _report_bundle(report_dir,full_identity,result=full,metrics=full_metrics,quality=quality,config=config,
                data_path=args.input,benchmark=run_buy_and_hold(enriched,config.costs),source_root=root/'src/autobit')
            runs=[]
            for fold in folds:
                for phase in ('TRAIN','OOS'):
                    request=research_request(frame,fold,cost,phase,name)
                    path=folder/f'{fold.fold_id}-{cost.cost_id}-{phase}.json.gz'
                    try:
                        identity={'experiment':experiment_hash,'candidate':name,'cost':cost.cost_id,
                                  'fold':fold.fold_id,'phase':phase,'config':request.config}
                        result=_cell(path,request.frame,request.config,request,identity)
                        runs.append(WalkForwardRun(phase,fold.fold_id,trial.trial_id,cost.cost_id,'COMPLETED',result,_metrics(result)))
                    except Exception as error:
                        failure=folder/f'{fold.fold_id}-{cost.cost_id}-{phase}-failure.json'
                        if not failure.exists():
                            _write(failure,{'type':type(error).__name__,'message':str(error)})
                        raise
                print(f'{name} {cost.cost_id} {fold.fold_id} complete',flush=True)
            stitched=_stitch_oos(runs,folds,trial,cost)
            oos_record={'candidate':name,'cost':cost.cost_id,'experiment':experiment_hash,'stitched':stitched}
            detail=folder/f'oos-{cost.cost_id}.json'
            if detail.exists():
                if detail.read_bytes()!=_bytes(oos_record):
                    raise ValueError('stored OOS differs from validated cells')
            else:
                _write(detail,oos_record)
            train=[r.metrics.sharpe_ratio for r in runs if r.phase=='TRAIN']
            oos=[r for r in runs if r.phase=='OOS']
            rows.append({'candidate':name,'cost':cost.cost_id,'full':full_metrics,
                         'full_final_equity':full.final_equity,'oos':stitched.metrics,
                         'oos_final_equity':stitched.equity_curve[-1].equity,
                         'positive_folds':sum(r.metrics.expectancy>0 for r in oos),
                         'fold_count':len(folds),'mean_train_sharpe':sum(train)/len(train),
                         'folds':[{'id':r.fold_id,'metrics':r.metrics} for r in oos]})
            summary=folder/f'summary-{cost.cost_id}.json'
            _write_or_verify(summary,rows[-1])
    combined=args.output/'comparison.json'
    if not combined.exists():
        _write(combined,rows)
    elif combined.read_bytes()!=_bytes(rows):
        raise ValueError('stored comparison differs')
    checksums={p.relative_to(args.output).as_posix():_hash(p.read_bytes())
               for p in sorted(args.output.rglob('*')) if p.is_file() and p.name!='SHA256SUMS.json'}
    checks=args.output/'SHA256SUMS.json'
    if checks.exists():
        if checks.read_bytes()!=_bytes(checksums):
            raise ValueError('final artifact checksums differ')
    else:
        _write(checks,checksums)
    print('COMPLETE: exploratory evidence saved; production defaults unchanged',flush=True)


if __name__ == '__main__':
    main()
