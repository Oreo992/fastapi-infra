# fastapi-infra search plugin example

This is a minimal external plugin package for `fastapi-infra`.

It demonstrates:

- declaring a `fastapi_infra.plugins` entry point
- exposing a strict config model
- registering a service
- contributing scaffold files, README sections, env vars, and config examples
- adding a production release check

Install it into an environment that already has `fastapi-infra` installed:

```bash
python -m pip install -e examples/search_plugin --no-deps
fastapi-infra new /tmp/search-api --plugins search
cd /tmp/search-api
fastapi-infra config-check --settings infra.toml
python -m pytest -q
```
