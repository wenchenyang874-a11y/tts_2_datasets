"""Publish a tested immutable tag: draft, upload, download/hash, then publish."""
import argparse
import json
import subprocess
from pathlib import Path

from release_artifacts import ROOT, VERSION, digest

REPOSITORY = "wenchenyang874-a11y/tts_2_datasets"


def output(*command):
    return subprocess.check_output(command, cwd=ROOT, text=True, encoding="utf-8").strip()


def execute(*command):
    subprocess.run(command, cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--windows10-evidence", type=Path)
    parser.add_argument("--notes", type=Path, required=True)
    parser.add_argument("--download-dir", type=Path, required=True)
    args = parser.parse_args()
    release = args.release_dir.resolve()
    manifest_path = release / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    commit = output("git", "rev-parse", "HEAD")
    if output("git", "status", "--porcelain"):
        raise RuntimeError("Commit all release changes first")
    if manifest["version"] != VERSION or manifest["source_commit"] != commit:
        raise RuntimeError("Build source/version differs from release commit")
    if not evidence["ok"] or evidence["manifest_sha256"] != digest(manifest_path) or evidence["source_commit"] != commit:
        raise RuntimeError("Installer evidence does not match these exact release assets")
    if not evidence.get("windows11_verified"):
        raise RuntimeError("Windows 11 installer acceptance is required")
    if not manifest["prerelease"]:
        win10 = json.loads(args.windows10_evidence.read_text(encoding="utf-8")) if args.windows10_evidence else {}
        if not win10.get("ok") or not win10.get("windows10_verified") or win10.get("manifest_sha256") != digest(manifest_path) or win10.get("source_commit") != commit:
            raise RuntimeError("Stable releases also require matching --windows10-evidence")
    if not args.notes.is_file() or not args.notes.read_text(encoding="utf-8").strip():
        raise RuntimeError("Release notes are required")
    if args.download_dir.exists():
        raise RuntimeError("Choose a new remote download directory")
    for entry in manifest["assets"]:
        path = release / entry["name"]
        if path.stat().st_size != entry["size"] or digest(path) != entry["sha256"]:
            raise RuntimeError("Local release asset changed after testing")
    remote = output("git", "remote", "get-url", "origin")
    if remote.removesuffix(".git") not in (f"https://github.com/{REPOSITORY}", f"git@github.com:{REPOSITORY}"):
        raise RuntimeError("Unexpected origin repository")
    branch = output("git", "branch", "--show-current")
    if branch not in ("feature/windows-native", "main") or (not manifest["prerelease"] and branch != "main"):
        raise RuntimeError("Unexpected release branch")
    tag = manifest["tag"]
    exists = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"], cwd=ROOT, capture_output=True).returncode == 0
    if exists and output("git", "rev-list", "-n", "1", tag) != commit:
        raise RuntimeError("Existing tag points elsewhere; never move a published tag")
    existing = subprocess.run(["gh", "api", f"repos/{REPOSITORY}/releases/tags/{tag}"], capture_output=True, text=True, encoding="utf-8")
    existing_release = json.loads(existing.stdout) if existing.returncode == 0 else None
    if existing_release and not existing_release["draft"]:
        raise RuntimeError("Release is already published; create a new version")
    if existing.returncode and "404" not in existing.stderr:
        raise RuntimeError("Could not determine remote release state; stop without publishing")
    if not exists:
        execute("git", "tag", "-a", tag, "-m", f"TTS Dataset Studio {VERSION}", commit)
    execute("git", "-c", "http.proxy=", "-c", "https.proxy=", "push", "origin", branch)
    execute("git", "-c", "http.proxy=", "-c", "https.proxy=", "push", "origin", f"refs/tags/{tag}")
    if not existing_release:
        execute("gh", "release", "create", tag, "--repo", REPOSITORY, "--verify-tag", "--draft", "--title", f"TTS Dataset Studio {VERSION}", "--notes-file", str(args.notes), *( ["--prerelease"] if manifest["prerelease"] else []))
    current = json.loads(output("gh", "api", f"repos/{REPOSITORY}/releases/tags/{tag}"))
    # A draft may be resumed after an interrupted upload. Never clobber assets:
    # mismatching names/digests must be inspected, not silently overwritten.
    for entry in manifest["assets"]:
        asset = next((item for item in current["assets"] if item["name"] == entry["name"]), None)
        if asset:
            if asset["size"] != entry["size"] or asset.get("digest") != f"sha256:{entry['sha256']}":
                raise RuntimeError("Draft contains a differing asset; leave draft for inspection")
        else:
            execute("gh", "release", "upload", tag, str(release / entry["name"]), "--repo", REPOSITORY)
    args.download_dir.mkdir(parents=True)
    for entry in manifest["assets"]:
        execute("gh", "release", "download", tag, "--repo", REPOSITORY, "--pattern", entry["name"], "--dir", str(args.download_dir))
        downloaded = args.download_dir / entry["name"]
        if downloaded.stat().st_size != entry["size"] or digest(downloaded) != entry["sha256"]:
            raise RuntimeError("Remote asset verification failed; release remains a draft")
    execute("gh", "release", "edit", tag, "--repo", REPOSITORY, "--draft=false", "--latest=false" if manifest["prerelease"] else "--latest=true")
    published = json.loads(output("gh", "api", f"repos/{REPOSITORY}/releases/tags/{tag}"))
    result = {"ok": not published["draft"], "url": published["html_url"], "tag": tag, "source_commit": commit,
              "assets": manifest["assets"], "remote_bytes_and_sha256_verified": True}
    (args.download_dir / "verification.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(result["url"])


if __name__ == "__main__":
    main()
