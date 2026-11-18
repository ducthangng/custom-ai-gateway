# deprecated/

Tests and release-check code taken out to keep the demo small and easy to read. Nothing here runs:
pytest only collects `quick_tests/`, and the release check is only `release_check/run.py`.
Everything is kept so it can be put back.

## What is here

| Path | What it was |
|---|---|
| `quick_tests/test_chat_api.py` | chat API suite (12 tests): settings, errors, cache hit/miss, router fallbacks, UI steps |
| `quick_tests/test_router_jj.py` | JJ suite (14 tests): matching rules, model choice, checks on the real `routes.yaml` |
| `quick_tests/test_redis_cache.py` | Redis cache suite (4 tests), including Redis down and the `integration` test on a real Redis |
| `quick_tests/test_release_check.py` | tests of the old release check tool (8 tests) |
| `quick_tests/conftest.py`, `helpers.py`, `fake_router.py` | test gateway fixtures, SSE helpers and the fake router these tests use |
| `release_check/` | the old release check tool: 5 checks (`config_is_valid`, `api_unchanged`, `routing_unchanged`, `cache_works`, `speed_under_load`), approved results with tolerances, a fingerprint cache that skips unchanged checks, a "what got worse" report, practice scenarios `broken_config` / `worse_routing`, and its Jenkinsfile (Redis sidecar, PVC `qualify-cache`, `SCENARIO` / `NO_CACHE` params) |

The CI also had an `Integration tests` stage (real Redis sidecar) and a coverage gate: 80% with the full
suite (measured 84%). With only the 3 small tests coverage is 7%, so the gate was removed.

## Putting it back

```bash
cp deprecated/quick_tests/*.py quick_tests/
rm -r release_check && cp -r deprecated/release_check release_check
```

Then in `../Jenkinsfile` put back the Redis sidecar, the `Integration tests` stage, `--cov=gateway` on
pytest and the coverage gate stage, and in `mlops-gitops/jenkins/jobs/qualify.xml` the `SCENARIO`
parameter (see git history for both).
