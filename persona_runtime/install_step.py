"""Run one PCAR installation step inside the launcher's Windows job."""
import os
from pathlib import Path
import subprocess
import sys


if __name__=="__main__":
    log,executable,operation,*arguments=sys.argv[1:]
    if operation not in ('sync','venv'):
        is_model=(operation=='-B' and len(arguments) in (5,7)
                  and arguments[:3]==['-m','persona_runtime','--home'] and arguments[4]=='prepare-model'
                  and arguments[5:6] in ([],['--local-source'])
                  and arguments[3]==os.environ.get('PERSONA_RUNTIME_HOME'))
        if not is_model:raise SystemExit(2)
    with Path(log).open('a',encoding='utf-8') as output:
        result=subprocess.run([executable,operation,*arguments],stdout=output,stderr=subprocess.STDOUT,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    raise SystemExit(result.returncode)
