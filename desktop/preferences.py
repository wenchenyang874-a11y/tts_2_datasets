"""Desktop startup preferences, separate from datasets and cloud credentials."""
import json
import os
import sys
import tempfile
from pathlib import Path


def default_preferences_path():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local")) / "TTS Dataset Studio/desktop_preferences.json"


class DesktopPreferences:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_preferences_path()

    def data_directory(self):
        if not self.path.exists():
            return None
        value = json.loads(self.path.read_text(encoding="utf-8"))
        directory = value.get("data_directory") if isinstance(value, dict) else None
        if not isinstance(directory, str) or not directory or not Path(directory).is_absolute():
            raise ValueError("已保存的数据目录配置无效")
        return Path(directory).resolve()

    def validate_data_directory(self, value):
        directory = Path(value).expanduser().resolve()
        if getattr(sys, "frozen", False):
            installation = Path(sys.executable).resolve().parent
            if directory == installation or installation in directory.parents or directory in installation.parents:
                raise ValueError("数据目录必须与程序安装目录分离")
        directory.mkdir(parents=True, exist_ok=True)
        # The session owns migration/locking. This method checks only location
        # and write access; it never opens or rewrites the dataset itself.
        with tempfile.TemporaryFile(dir=directory, prefix=".tts-write-check-") as probe:
            probe.write(b"writable")
            probe.flush()
        return directory

    def save_data_directory(self, value):
        directory = self.validate_data_directory(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=self.path.parent, prefix=".desktop-pref-", suffix=".tmp", delete=False, encoding="utf-8") as stream:
                temporary = Path(stream.name)
                json.dump({"data_directory": str(directory)}, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return directory
