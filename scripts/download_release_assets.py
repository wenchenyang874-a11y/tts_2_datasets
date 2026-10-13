"""Read back draft/public GitHub assets with bounded parallel Range requests."""
import concurrent.futures
import hashlib
import json
import math
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def download_verified(repository, release_id, entries, destination, workers=8):
    destination = Path(destination)
    if destination.exists():
        raise RuntimeError("Remote download destination must be new")
    metadata = json.loads(subprocess.check_output(["gh", "api", f"repos/{repository}/releases/{release_id}"]))
    assets = {asset["name"]: asset for asset in metadata["assets"]}
    for entry in entries:
        asset = assets.get(entry["name"])
        if not asset or asset["size"] != entry["size"] or asset.get("digest") != "sha256:" + entry["sha256"]:
            raise RuntimeError("GitHub asset metadata differs from tested artifact")
    # Authentication lives only in memory, never in argv, files or logs.
    # Do NOT automatically follow the API redirect with Authorization: request
    # the signed asset URL separately, without carrying the token across hosts.
    token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    destination.mkdir(parents=True)
    opener = urllib.request.build_opener(NoRedirect())
    for entry in entries:
        name = entry["name"]
        if Path(name).name != name or "/" in name or "\\" in name:
            raise RuntimeError("Invalid asset filename")
        request = urllib.request.Request(f"https://api.github.com/repos/{repository}/releases/assets/{assets[name]['id']}",
                                         headers={"Accept": "application/octet-stream", "Authorization": "Bearer " + token})
        try:
            try:
                response = opener.open(request, timeout=60)
                response.close()
                raise RuntimeError("Expected signed asset redirect")
            except urllib.error.HTTPError as error:
                if error.code != 302:
                    raise RuntimeError("Asset redirect unavailable") from None
                signed_url = error.headers["Location"]
                error.close()
            parsed = urllib.parse.urlsplit(signed_url)
            if parsed.scheme != "https" or parsed.hostname not in ("release-assets.githubusercontent.com", "objects.githubusercontent.com"):
                raise RuntimeError("Unexpected asset download host")
        except Exception:
            # Exception strings may contain an authenticated/signed URL. Never
            # expose those in the caller's traceback or build/release evidence.
            raise RuntimeError(f"Unable to obtain download URL for {name}") from None
        count = min(workers, max(1, math.ceil(entry["size"] / (4 * 1024 * 1024))))
        chunk_size = math.ceil(entry["size"] / count)
        parts = destination / (name + ".parts")
        parts.mkdir()

        def fetch(index):
            start = index * chunk_size
            end = min(entry["size"], start + chunk_size) - 1
            part = parts / str(index)
            try:
                query = urllib.request.Request(signed_url, headers={"Range": f"bytes={start}-{end}"})
                with urllib.request.urlopen(query, timeout=60) as response:
                    if response.status == 206:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                        if not match or tuple(map(int, match.groups())) != (start, end, entry["size"]):
                            raise RuntimeError("Incorrect range response")
                    elif response.status != 200 or count != 1:
                        raise RuntimeError("Server did not honor Range")
                    with part.open("xb") as stream:
                        while block := response.read(1024 * 1024):
                            stream.write(block)
                if part.stat().st_size != end - start + 1:
                    raise RuntimeError("Incomplete range")
                print(f"{name}: part {index + 1}/{count} received", flush=True)
            except Exception:
                raise RuntimeError(f"Asset download failed: {name}, part {index + 1}") from None

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(fetch, range(count)))
        output = destination / name
        with output.open("xb") as stream:
            for index in range(count):
                with (parts / str(index)).open("rb") as part:
                    while block := part.read(1024 * 1024):
                        stream.write(block)
        with output.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        if output.stat().st_size != entry["size"] or checksum != entry["sha256"]:
            raise RuntimeError(f"Remote complete-file checksum mismatch: {name}")
        print(f"{name}: complete bytes and SHA256 verified", flush=True)
