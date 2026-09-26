"""Deployment examples must not ship an unauthenticated multi-tenant API."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env_values(name: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def test_staging_example_enables_authentication() -> None:
    values = _env_values(".env.staging.example")

    assert values["AUTH_ENABLED"] == "true"
    assert values["API_KEYS"] not in {"", "{}"}


def test_deployed_examples_require_a_health_admin_token() -> None:
    for name in (".env.staging.example", ".env.production.example"):
        values = _env_values(name)
        assert values["HEALTH_CHECKS_PUBLIC"] == "false", name
        assert values.get("HEALTH_ADMIN_TOKEN"), name
