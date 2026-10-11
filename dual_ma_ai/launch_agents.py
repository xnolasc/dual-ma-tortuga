"""Generate disabled user LaunchAgents in the repository. Never installs jobs."""
import argparse
from pathlib import Path
import plistlib
from .monitor import ROOT, STATE_ROOT


def configurations(root=ROOT):
    root=Path(root).resolve();python=root/'.venv/bin/python';logs=root/'data/dual_ma_ai/monitor'
    common=dict(Disabled=True,RunAtLoad=True,KeepAlive=True,ThrottleInterval=60,
                WorkingDirectory=str(root),ProcessType='Background',Umask=0o077,
                EnvironmentVariables=dict(PYTHONUNBUFFERED='1',PYTHONDONTWRITEBYTECODE='1'))
    return {
        'local.dual-ma-ai.market-data':dict(common,Label='local.dual-ma-ai.market-data',
            ProgramArguments=[str(python),'-m','dual_ma_ai.monitor','run'],
            StandardOutPath=str(logs/'worker-stdout.log'),StandardErrorPath=str(logs/'worker-stderr.log')),
        'local.dual-ma-ai.dashboard':dict(common,Label='local.dual-ma-ai.dashboard',
            ProgramArguments=[str(python),'-m','dual_ma_ai.dashboard','--read-only','--open-browser'],
            StandardOutPath=str(logs/'dashboard-stdout.log'),StandardErrorPath=str(logs/'dashboard-stderr.log')),
    }


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=ROOT/'launchd')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    for label,config in configurations().items():
        path=args.output/(label+'.plist');path.write_bytes(plistlib.dumps(config));print(path)


if __name__=='__main__':main()
