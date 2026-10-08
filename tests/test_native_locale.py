import os
import subprocess
import sys

from backend import native_locale


def test_ctp_locale_is_available_to_callbacks_and_child_processes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(native_locale, "RUNTIME", tmp_path)
    monkeypatch.setenv("LOCPATH", str(tmp_path / "locales"))
    native_locale.prepare_ctp_locale()
    native_locale.prepare_ctp_locale()
    assert len(os.environ["LOCPATH"].split(os.pathsep)) == 1
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import locale; locale.setlocale(locale.LC_ALL, 'zh_CN.GB18030'); "
            "assert locale.nl_langinfo(locale.CODESET) == 'GB18030'",
        ],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode()
