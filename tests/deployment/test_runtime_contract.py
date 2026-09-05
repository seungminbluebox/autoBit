from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "deploy" / "oci" / "runtime.env"


def _runtime_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in RUNTIME.read_text(encoding="utf-8").splitlines():
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*=[A-Za-z0-9_./:+%=-]+", raw)
        key, value = raw.split("=", 1)
        assert key not in values
        values[key] = value
    return values


def test_arm64_runtime_is_exactly_pinned():
    values = _runtime_values()
    assert values["RUNTIME_SCHEMA"] == "1"
    assert values["UV_VERSION"] == "0.12.10"
    assert values["UV_ARCHIVE_SHA256"] == "9ff6b9d4665edcdd3a88dcc73cd1eb641754deb927f14e8c62ebfde6bf4f5f5e"
    assert values["PYTHON_VERSION"] == "3.12.14"
    assert values["PYTHON_BUILD_TAG"] == "20260901"
    assert values["PYTHON_ARCHIVE_SHA256"] == "577b4bec0793ad1ff0cbff9adbd0df078eddde38a4c41bf5d83ad381a85ee39d"
    assert all("latest" not in value.lower() for value in values.values())


def test_uv_lock_and_exact_required_version_exist():
    assert (ROOT / "uv.lock").is_file()
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'required-version = "==0.12.10"' in project
