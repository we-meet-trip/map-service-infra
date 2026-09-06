#!/usr/bin/env python3
"""Root-only GCP test backup timer entry point; never handles production data."""
import datetime as dt
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

CONFIG = Path('/etc/map-deploy/backup.env')
REPO = Path('/home/mapadmin26/map-service-infra')
STATE = Path('/var/lib/map-deploy/backup-status.json')
ALLOWED = {'BACKUP_DIR','BACKUP_REMOTE','BACKUP_S3_ENDPOINT','BACKUP_GCP_CREDENTIALS_FILE'}

def main():
    os.umask(0o077)
    info=CONFIG.stat()
    if os.geteuid() != 0 or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError('backup configuration must be private and root owned')
    values={}
    for line in CONFIG.read_text().splitlines():
        if not line or line.startswith('#'): continue
        key, sep, value=line.partition('=')
        if not sep or key not in ALLOWED | {'BACKUP_REQUIRE_REMOTE'}:
            raise RuntimeError('unsupported backup configuration')
        values[key]=value
    if not values.get('BACKUP_REMOTE'):
        raise RuntimeError('off-host backup destination is required')
    # Exclude inherited debug and unrelated application credentials.
    env={key:os.environ[key] for key in ('PATH','HOME','LANG') if key in os.environ}
    env.update(values, BACKUP_REQUIRE_REMOTE='1')
    # Keep the verified backup implementation independent of application rollbacks.
    loader = "from pathlib import Path; import pg_backup; pg_backup.ROOT=Path('/home/mapadmin26/map-service-infra'); raise SystemExit(pg_backup.main())"
    result=subprocess.run([sys.executable,'-c',loader,'backup','--test'],
                          cwd='/usr/local/lib/map-deploy',
                          env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    previous=json.loads(STATE.read_text()) if STATE.exists() else {}
    now=dt.datetime.now(dt.timezone.utc).isoformat()
    status={'last_attempt_at':now,'success':result.returncode==0,
            'last_success_at':now if result.returncode==0 else previous.get('last_success_at')}
    STATE.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    pending=STATE.with_suffix('.new');pending.write_text(json.dumps(status)+'\n');pending.replace(STATE)
    print(json.dumps(status))
    return result.returncode

if __name__=='__main__':
    try: sys.exit(main())
    except Exception as exc:
        print(json.dumps({'success':False,'error_type':type(exc).__name__}),file=sys.stderr)
        sys.exit(1)
