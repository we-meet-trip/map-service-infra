#!/usr/bin/env python3
"""Root-only GCP test PostgreSQL timer with shared deployment/backup locks."""
import json
import sys
from backup_job import safe_entry

if __name__ == '__main__':
    try:
        sys.exit(safe_entry('pg'))
    except Exception as error:
        print(json.dumps({'success': False, 'error_type': type(error).__name__}), file=sys.stderr)
        sys.exit(1)
