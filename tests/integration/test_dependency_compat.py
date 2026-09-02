from __future__ import annotations

import sys


def test_supported_python_and_core_imports() -> None:
    assert sys.version_info[:2] == (3, 12)
    import backtrader
    import httpx
    import numpy
    import pandas
    import pandas_ta
    import scipy

    assert backtrader.__version__ == "1.9.78.123"
    assert httpx.__version__
    assert numpy.__version__
    assert pandas.__version__
    pandas_ta_version = getattr(pandas_ta, "__version__", getattr(pandas_ta, "version", ""))
    assert pandas_ta_version == "0.4.71b0"
    assert scipy.__version__


def test_package_version() -> None:
    import autobit

    assert autobit.__version__ == "0.1.0"
