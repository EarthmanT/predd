# Fix: gh_run Should Not Retry Permanent Errors

## Problem

`gh_run` retries up to 3 times on any non-zero exit. This means 404s (wrong repo, deleted issue, missing label), 401s (auth failure), and 422s (invalid request) all get retried unnecessarily — adding 0+5+10=15 seconds of delay and noise in logs before ultimately failing.

## Fix

Only retry on transient errors. Fail immediately on permanent ones.

**Retry** (transient):
- `rate limit` in stderr
- `502`, `503`, `504` in stderr
- `timeout` in stderr
- `connection` in stderr

**Fail immediately** (permanent):
- `404` / `not found`
- `401` / `403` / `unauthorized` / `forbidden`
- `422` / `unprocessable`
- `already exists`

```python
PERMANENT_ERRORS = ("not found", "404", "401", "403", "unauthorized",
                    "forbidden", "422", "unprocessable", "already exists")

def gh_run(args, check=True):
    for attempt in range(3):
        result = subprocess.run(["gh"] + args, capture_output=True, text=True)
        if result.returncode == 0 or not check:
            return result
        stderr = result.stderr.lower()
        if any(x in stderr for x in PERMANENT_ERRORS):
            result.check_returncode()  # fail immediately
        if any(x in stderr for x in TRANSIENT_ERRORS):
            wait = 2 ** attempt * 5
            logger.warning(...)
            time.sleep(wait)
            continue
        result.check_returncode()  # unknown error, fail immediately
    result.check_returncode()
    return result
```

Apply to both `predd.py` and `hunter.py`.
