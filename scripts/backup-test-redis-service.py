#!/usr/bin/env python3
"""Root-only Redis backup timer. Existing GCS credentials; separate redis-v1 prefix."""
import json
import sys
from backup_job import safe_entry

if __name__ == '__main__':
    try:
        sys.exit(safe_entry('redis'))
    except Exception as error:
        print(json.dumps({'success': False, 'error_type': type(error).__name__}), file=sys.stderr)
        sys.exit(1)
