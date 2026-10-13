"""Single release version for the app, PE resources, installer and Git tag."""

VERSION = "2.0.0-beta.5"


def windows_version(version: str = VERSION) -> tuple[int, int, int, int]:
    import re

    match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-beta\.([1-9]\d*))?", version)
    if not match:
        raise ValueError("Use major.minor.patch or major.minor.patch-beta.N")
    # Windows only stores four unsigned 16-bit numbers. Stable releases sort
    # after every beta of the same release, avoiding a downgrade on beta -> GA.
    parts = tuple(int(part) for part in match.groups()[:3])
    revision = int(match[4]) if match[4] else 65535
    if any(part > 65535 for part in parts) or not 1 <= revision <= 65535 or (match[4] and revision == 65535):
        raise ValueError("Windows version component is out of range")
    return (*parts, revision)
