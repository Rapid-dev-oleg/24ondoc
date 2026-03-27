"""Admin panel — .env file read/write implementation of EnvSettingsPort."""

from __future__ import annotations

import re

from admin.application.ports import EnvSettingsPort


class DotEnvSettingsPort(EnvSettingsPort):
    """Reads and writes key=value pairs in a .env file."""

    def __init__(self, env_file_path: str) -> None:
        self._path = env_file_path

    def get_setting(self, key: str) -> str | None:
        try:
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    if k.strip() == key:
                        return v.strip()
        except FileNotFoundError:
            pass
        return None

    def update_setting(self, key: str, value: str) -> None:
        """Update or append KEY=VALUE in the .env file."""
        try:
            with open(self._path, encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []

        pattern = re.compile(rf"^{re.escape(key)}\s*=.*$")
        replaced = False
        new_lines: list[str] = []
        for line in lines:
            if pattern.match(line.rstrip()):
                new_lines.append(f"{key}={value}\n")
                replaced = True
            else:
                new_lines.append(line)

        if not replaced:
            new_lines.append(f"{key}={value}\n")

        with open(self._path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
