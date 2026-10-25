#!/usr/bin/env python3
"""Container-only readiness probe used by Tokyo deployment after restart.

Authentication stays inside the process; stdout contains fixed health markers
and exception class names, never headers, credentials or arbitrary bodies.
"""
import os
import time
import urllib.request


def main():
    """Retry the local authenticated readiness endpoint within the deploy window."""
    key = os.environ.get('LITELLM_MASTER_KEY')
    if not key:
        print('MASTER_KEY_MISSING')
        return 1
    deadline = time.monotonic() + 180
    # Leave a margin inside the workflow poll window for SSM startup and readback.
    for attempt in range(36):
        if time.monotonic() >= deadline:
            break
        request = urllib.request.Request('http://127.0.0.1:4000/health/readiness',
            headers={'Authorization': 'Bearer ' + key})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 200:
                    print('HEALTH_OK')
                    return 0
        except Exception as error:
            print('readiness_retry reason=' + type(error).__name__, flush=True)
        time.sleep(5)
    print('HEALTH_FAIL')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
