"""Report the actual test runtime and Linux container memory high-water mark."""

from importlib.metadata import version
from pathlib import Path
import platform
import subprocess


def pytest_report_header():
    java = subprocess.check_output(["java", "-version"], stderr=subprocess.STDOUT, text=True)
    return [
        f"Runtime: Python {platform.python_version()} / {platform.system()} {platform.machine()}",
        java.splitlines()[0],
        "Packages: " + ", ".join(
            f"{package}={version(package)}" for package in ("pyspark", "psycopg", "paho-mqtt", "pytest")
        ),
    ]


def pytest_sessionfinish(session, exitstatus):
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter:
        for line in pytest_report_header():
            reporter.write_line(line)
        for field in ("memory.peak", "memory.max"):
            path = Path("/sys/fs/cgroup") / field
            observed = path.read_text().strip() if path.exists() else "unavailable (not cgroup v2)"
            reporter.write_line(f"Container {field}: {observed} bytes")
