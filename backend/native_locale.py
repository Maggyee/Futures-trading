"""Provide the locale hard-coded by vnpy_ctp's Linux C++ callbacks."""

import os
import subprocess
import sys

from .config import RUNTIME


def prepare_ctp_locale():
    if not sys.platform.startswith("linux"):
        return
    localedir = RUNTIME / "locales"
    if localedir.exists():
        paths = os.environ.get("LOCPATH", "").split(os.pathsep)
        os.environ["LOCPATH"] = os.pathsep.join(
            dict.fromkeys([str(localedir), *(p for p in paths if p)])
        )

    def available():
        # Probe in a child: changing the main process locale is not thread-safe.
        return (
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import locale; locale.setlocale(locale.LC_ALL, 'zh_CN.GB18030')",
                ],
                capture_output=True,
                timeout=10,
            ).returncode
            == 0
        )

    if available():
        return
    localedir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        subprocess.run(
            [
                "localedef",
                "--no-archive",
                "-i",
                "zh_CN",
                "-f",
                "GB18030",
                str(localedir / "zh_CN.GB18030"),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "CTP 需要 zh_CN.GB18030 locale，自动生成失败。"
            "请安装系统 locales 包（需含 zh_CN 与 GB18030 数据），然后重新启动。"
        ) from exc
    paths = os.environ.get("LOCPATH", "").split(os.pathsep)
    os.environ["LOCPATH"] = os.pathsep.join(
        dict.fromkeys([str(localedir), *(p for p in paths if p)])
    )
    if not available():
        raise RuntimeError("zh_CN.GB18030 locale 校验失败，交易进程未启动")
