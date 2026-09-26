"""Controller-only baseline/gold/recorded-patch replay. Never embed in a solver."""
from __future__ import annotations
import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from mvp_v2.harness import parse_patch_targets, task_pytest_plugins
from tools.build_trace_notebook import CORE_TASK_IDS


def main():
    options = argparse.ArgumentParser()
    options.add_argument('--output', default='work/trace-v21-review/replay')
    options.add_argument('--tasks', nargs='+', default=CORE_TASK_IDS)
    options.add_argument('--variants', nargs='+', default=['baseline', 'gold', 'v21'])
    args = options.parse_args()
    output = Path(args.output);output.mkdir(parents=True, exist_ok=True)
    tasks = {t['instance_id']:t for t in map(json.loads, (PROJECT/'data'/'tasks.jsonl').read_text().splitlines())}
    records = []
    for task_id in args.tasks:
        task=tasks[task_id]
        for variant in args.variants:
            with tempfile.TemporaryDirectory(prefix='trace-replay-') as tmp:
                repo=Path(tmp)
                # Do not copy .git's repository history into each verification clone.
                source=PROJECT/'work/review-snapshots'/task_id
                archive=subprocess.run(['git','archive','HEAD'],cwd=source,capture_output=True,check=True).stdout
                with tarfile.open(fileobj=io.BytesIO(archive)) as ar:ar.extractall(repo,filter='data')
                for cmd in [['git','init','-q'],['git','add','-A'],['git','-c','user.email=e@e','-c','user.name=e','commit','-qm','base']]:
                    subprocess.run(cmd,cwd=repo,capture_output=True,check=True)
                patch = task['patch'] if variant=='gold' else ''
                if variant=='v21':
                    patch=next((PROJECT/'work/kaggle-trace-v21/trace_runs').glob('*/'+task_id+'*/final/agent.patch')).read_text()
                if patch:
                    subprocess.run(['git','apply','--whitespace=nowarn','-'],input=patch.rstrip()+'\n',text=True,cwd=repo,check=True,capture_output=True)
                targets=parse_patch_targets(task['test_patch'])
                for target in targets:
                    subprocess.run(['git','checkout','HEAD','--',target],cwd=repo,capture_output=True)
                    subprocess.run(['git','clean','-f','--',target],cwd=repo,capture_output=True)
                subprocess.run(['git','apply','--whitespace=nowarn','-'],input=task['test_patch'].rstrip()+'\n',text=True,cwd=repo,check=True,capture_output=True)
                env={**os.environ,'PYTHONPATH':os.pathsep.join([str(repo/'src'),str(repo)]),
                     'PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1','PYTEST_PLUGINS':task_pytest_plugins(Path(sys.executable),repo),
                     'TEST_TMPDIR':'/tmp','PYTHONDONTWRITEBYTECODE':'1'}
                try:
                    result=subprocess.run([sys.executable,'-m','pytest',*targets,'-p','no:anyio','-o','timeout=0','-q','--tb=short'],
                                          cwd=repo,env=env,capture_output=True,text=True,timeout=240)
                    log=result.stdout+'\n'+result.stderr;code=result.returncode
                except subprocess.TimeoutExpired as error:
                    log=str(error.stdout)+str(error.stderr);code=124
                (output/f'{task_id}-{variant}.log').write_text(log)
                record={'task':task_id,'variant':variant,'exit_code':code,'tail':log[-1800:]}
                records.append(record)
                (output/'summary.json').write_text(json.dumps(records,indent=2))
                print(task_id,variant,code,log.splitlines()[-2:] if log else '',flush=True)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
