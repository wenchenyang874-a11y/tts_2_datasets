"""Exercise the actual release installer in an owned directory and HKCU key."""
import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import zipfile
from pathlib import Path

from release_artifacts import ROOT, digest


def run(command, *, expect_success=True, timeout=180):
    result = subprocess.run([str(item) for item in command], timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
    if (result.returncode == 0) != expect_success:
        raise RuntimeError(f"Unexpected exit {result.returncode}: {Path(command[0]).name}")
    return result.returncode


def main():
    import winreg

    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--isolated-setup", type=Path)
    parser.add_argument("--app-id")
    parser.add_argument("--mutex-name")
    args = parser.parse_args()
    if any((args.isolated_setup, args.app_id, args.mutex_name)) and not all((args.isolated_setup, args.app_id, args.mutex_name)):
        parser.error("Isolated validation requires --isolated-setup, --app-id and --mutex-name together")
    release = args.release_dir.resolve()
    evidence = args.evidence.resolve()
    manifest_path = release / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["assets"]:
        path = release / entry["name"]
        if path.stat().st_size != entry["size"] or digest(path) != entry["sha256"]:
            raise RuntimeError("Artifact checksum mismatch")
    if evidence.exists():
        raise RuntimeError("Choose a new evidence directory; never overwrite previous tests")
    app_id = args.app_id or manifest["app_id"]
    if args.isolated_setup and app_id == manifest["app_id"]:
        raise RuntimeError("Isolated installer must have a different AppId")
    key_name = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{app_id}_is1"
    registry_flags = winreg.KEY_READ | winreg.KEY_WOW64_64KEY

    def registration():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name, 0, registry_flags) as key:
                values = {name: winreg.QueryValueEx(key, name)[0] for name in ("InstallLocation", "DisplayVersion")}
                try:
                    values["TDSNumericVersion"] = winreg.QueryValueEx(key, "TDSNumericVersion")[0]
                except FileNotFoundError:
                    values["TDSNumericVersion"] = ""
                return values
        except FileNotFoundError:
            return None

    # Use the unmodified production installer, only if its real per-user key is
    # absent. This avoids overwriting an existing user's installation. Once
    # created, every registry mutation/removal is bounded by InstallLocation.
    if registration() is not None:
        raise RuntimeError("App is already installed for this user; use a clean Windows test account")
    evidence.mkdir(parents=True)
    install = evidence / "安装 测试"
    data = evidence / "独立 数据"
    data.mkdir()
    sentinel = data / "preserve-me.txt"
    sentinel.write_text("owned test data - must survive uninstall\n", encoding="utf-8")
    production_setup = release / next(item["name"] for item in manifest["assets"] if item["name"].endswith("-setup.exe"))
    setup = args.isolated_setup.resolve() if args.isolated_setup else production_setup
    command = [setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOICONS", "/TASKS=", f"/DIR={install}"]
    report = {"ok": False, "platform": platform.platform(), "source_commit": manifest["source_commit"],
              "manifest_sha256": digest(manifest_path), "checks": [], "errors": [],
              "windows10_verified": False, "hardware_recording_verified": False, "real_cloud_verified": False,
              "upgrade_fixture": "simulated older installer registration and a damaged app binary"}
    report["installation_identity"] = "isolated AppId and mutex; same runtime and installer source" if args.isolated_setup else "production"
    report["test_installer_sha256"] = digest(setup)
    system = sys.getwindowsversion()
    report["windows10_verified"] = system.major == 10 and 17763 <= system.build < 22000
    report["windows11_verified"] = system.major == 10 and system.build >= 22000

    def owned_registration():
        values = registration()
        if not values or Path(values["InstallLocation"]).resolve() != install:
            raise RuntimeError("Refusing to touch unowned uninstall registration")
        return values

    def same_runtime():
        with zipfile.ZipFile(release / next(item["name"] for item in manifest["assets"] if item["name"].endswith("-portable.zip"))) as archive:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                relative = Path(*Path(entry.filename).parts[1:])
                target = install / relative
                if not target.is_file() or digest(target) != hashlib.sha256(archive.read(entry)).hexdigest():
                    raise RuntimeError(f"Installed runtime differs from portable package: {relative}")

    try:
        import pefile
        for binary in (production_setup, setup, ROOT / "dist/TTS Dataset Studio/TTS Dataset Studio.exe"):
            with pefile.PE(str(binary)) as metadata:
                info = metadata.VS_FIXEDFILEINFO[0]
                numeric = ".".join(str(part) for part in (info.FileVersionMS >> 16, info.FileVersionMS & 65535, info.FileVersionLS >> 16, info.FileVersionLS & 65535))
                if numeric != manifest["numeric_version"]:
                    raise RuntimeError("PE file version differs from release version")
        report["checks"].append("installer_and_app_pe_version_match")
        run([*command, f"/LOG={evidence / 'install.log'}"])
        user_file = install / "user-retained.txt"
        user_file.write_text("unknown files must not be deleted\n", encoding="utf-8")
        values = owned_registration()
        if values["DisplayVersion"] != manifest["version"] or values["TDSNumericVersion"] != manifest["numeric_version"]:
            raise RuntimeError("Incorrect installed version metadata")
        same_runtime()
        report["checks"].append("production_install_all_files_match_portable")
        # Presence of this same Win32 mutex is the app/installer contract. It
        # must reject both operations instead of silently terminating writers.
        sys.path.insert(0, str(ROOT))
        from desktop.installer_guard import InstallerGuard
        if args.mutex_name:
            import desktop.installer_guard as guard_module
            guard_module.MUTEX_NAME = args.mutex_name
        guard = InstallerGuard()
        try:
            run([*command, f"/LOG={evidence / 'running-upgrade.log'}"], expect_success=False)
            run([install / "unins000.exe", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={evidence / 'running-uninstall.log'}"], expect_success=False)
        finally:
            guard.close()
        same_runtime()
        report["checks"].append("running_app_blocks_upgrade_and_uninstall")
        owned_registration()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.SetValueEx(key, "TDSNumericVersion", 0, winreg.REG_SZ, "1.9.0.65535")
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, "1.9.0")
        executable = install / "TTS Dataset Studio.exe"
        executable.write_bytes(b"deliberately damaged owned upgrade fixture")
        run([*command, f"/LOG={evidence / 'upgrade.log'}"])
        if owned_registration()["DisplayVersion"] != manifest["version"]:
            raise RuntimeError("Upgrade did not update the registered version")
        same_runtime()
        if not sentinel.is_file() or not user_file.is_file():
            raise RuntimeError("Upgrade removed user data")
        report["checks"].append("upgrade_repairs_app_and_preserves_data")
        try:
            for index, newer in enumerate(("9.0.0.65535", "65535.0.0.65535")):
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
                    winreg.SetValueEx(key, "TDSNumericVersion", 0, winreg.REG_SZ, newer)
                run([*command, f"/LOG={evidence / f'downgrade-{index}.log'}"], expect_success=False)
        finally:
            owned_registration()
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
                winreg.SetValueEx(key, "TDSNumericVersion", 0, winreg.REG_SZ, manifest["numeric_version"])
        report["checks"].append("downgrade_rejected")
        # Remove developer environment hints; the installed runtime must locate
        # Python, Qt plugins and FFmpeg entirely inside its own dependency tree.
        original = dict(os.environ)
        try:
            for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "IMAGEIO_FFMPEG_EXE", "TTS_FFMPEG_PATH", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
                os.environ.pop(name, None)
            os.environ["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
            run([executable, "--smoke-test", evidence / "installed-smoke"])
            smoke = json.loads((evidence / "installed-smoke/result.json").read_text(encoding="utf-8"))
            if not smoke["ok"]:
                raise RuntimeError("Installed native acceptance failed")
            report["native_checks"] = smoke["checks"]
            run([executable, "--data-dir", data, "--preferences-file", evidence / "normal-preferences.json", "--verify-launch", evidence / "normal-launch"])
            normal = json.loads((evidence / "normal-launch/result.json").read_text(encoding="utf-8"))
            if not normal["ok"] or not normal["business_process_closed"]:
                raise RuntimeError("Installed normal startup/exit failed")
        finally:
            os.environ.clear()
            os.environ.update(original)
        report["checks"].append("installed_native_acceptance_and_normal_locked_startup")
        custom_preferences = evidence / "installed-smoke/desktop_preferences.json"
        selected_directory = Path(json.loads(custom_preferences.read_text(encoding="utf-8"))["data_directory"])
        run([executable, "--preferences-file", custom_preferences, "--verify-launch", evidence / "custom-directory-launch"])
        if not (selected_directory / "tts_dataset.sqlite3").is_file():
            raise RuntimeError("Cold startup ignored saved custom data directory")
        report["checks"].append("cold_start_uses_saved_custom_directory_without_command_override")
        report["ok"] = True
    except Exception as error:
        report["errors"].append(str(error))
    finally:
        if registration() is not None:
            owned_registration()
            run([install / "unins000.exe", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={evidence / 'uninstall.log'}"])
            if registration() is not None or (install / "TTS Dataset Studio.exe").exists() or (install / "_internal").exists():
                report["ok"] = False
                report["errors"].append("Uninstall left registered/runtime files")
            elif not sentinel.is_file() or not (install / "user-retained.txt").is_file():
                report["ok"] = False
                report["errors"].append("Uninstall removed user data")
            else:
                report["checks"].append("uninstall_removes_program_and_registration_preserves_data")
        (evidence / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
