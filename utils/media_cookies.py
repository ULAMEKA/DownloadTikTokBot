"""Resolve optional Netscape cookie files for media downloaders.

The default convention is ``<service>.cookies.txt`` inside
``MEDIA_COOKIES_DIR`` (for example ``tiktok.cookies.txt``). Custom domains
and filenames can be added through ``MEDIA_COOKIE_MAP`` without code changes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit


_SERVICE_ALIASES = {
    "youtu.be": "youtube",
    "youtube-nocookie.com": "youtube",
}


def _expanded_path(value: str, base_dir: Path | None = None) -> Path:
    expanded = Path(os.path.expandvars(value)).expanduser()
    if not expanded.is_absolute() and base_dir is not None:
        expanded = base_dir / expanded
    return expanded


def _cookies_dir() -> Path | None:
    value = os.getenv("MEDIA_COOKIES_DIR", "").strip()
    return _expanded_path(value) if value else None


def _url_host(url: str) -> str:
    candidate = url if "://" in url else f"https://{url}"
    return (urlsplit(candidate).hostname or "").lower().strip(".")


def _domain_names(host: str) -> list[str]:
    if not host:
        return []

    names = [host]
    normalized = host.removeprefix("www.")
    if normalized not in names:
        names.append(normalized)

    labels = normalized.split(".")
    if len(labels) >= 2:
        root_domain = ".".join(labels[-2:])
        if root_domain not in names:
            names.append(root_domain)
        service = labels[-2]
        if service not in names:
            names.append(service)

    for domain, alias in _SERVICE_ALIASES.items():
        if host == domain or host.endswith(f".{domain}"):
            if alias not in names:
                names.append(alias)
    return names


def _existing_file(value: str, base_dir: Path | None = None) -> Path | None:
    path = _expanded_path(value, base_dir)
    try:
        return path if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def _path_values(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not isinstance(value, str) or not value.strip():
        return []
    stripped = value.strip()
    if stripped.startswith("["):
        try:
            decoded = json.loads(stripped)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = None
        if isinstance(decoded, list):
            return [str(item).strip() for item in decoded if str(item).strip()]
    return [part.strip() for part in stripped.split(os.pathsep) if part.strip()]


def _append_file(files: list[Path], path: Path | None) -> None:
    if path is None:
        return
    key = str(path.absolute()).casefold()
    if all(str(current.absolute()).casefold() != key for current in files):
        files.append(path)


def _mapped_cookie_files(
    url: str,
    host: str,
    names: list[str],
    base_dir: Path | None,
) -> list[Path]:
    raw_mapping = os.getenv("MEDIA_COOKIE_MAP", "").strip()
    if not raw_mapping:
        return []
    try:
        mapping = json.loads(raw_mapping)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(mapping, dict):
        return []

    normalized_mapping = {
        str(key).lower().strip(): value
        for key, value in mapping.items()
        if _path_values(value)
    }
    files: list[Path] = []

    def add_values(value: object) -> None:
        for item in _path_values(value):
            _append_file(files, _existing_file(item, base_dir))

    for key in (*names, "*"):
        if value := normalized_mapping.get(key):
            add_values(value)

    for key, value in normalized_mapping.items():
        domain = key.removeprefix("*.").removeprefix(".")
        if key.startswith(("*.", ".")) and (
            host == domain or host.endswith(f".{domain}")
        ):
            add_values(value)
        elif "://" in key and url.lower().startswith(key):
            add_values(value)
    return files


def _directory_cookie_files(base_dir: Path, names: list[str]) -> list[Path]:
    files: list[Path] = []
    try:
        if not base_dir.is_dir():
            return files
        for name in names:
            patterns = (
                f"{name}.cookies.txt",
                f"{name}.*.cookies.txt",
                f"{name}-*.cookies.txt",
                f"{name}/*.cookies.txt",
            )
            for pattern in patterns:
                for path in sorted(base_dir.glob(pattern)):
                    _append_file(files, _existing_file(str(path)))
    except OSError:
        return files
    return files


def cookie_files_for_url(url: str) -> list[Path]:
    """Return ordered non-empty cookie files for anonymous-to-auth retries."""
    host = _url_host(url)
    names = _domain_names(host)
    base_dir = _cookies_dir()
    files = _mapped_cookie_files(url, host, names, base_dir)

    for name in names:
        env_name = f"{name.upper().replace('.', '_').replace('-', '_')}_COOKIES_FILE"
        for value in _path_values(os.getenv(env_name, "")):
            _append_file(files, _existing_file(value, base_dir))

    if base_dir is not None:
        for path in _directory_cookie_files(base_dir, names):
            _append_file(files, path)

    for value in _path_values(os.getenv("MEDIA_COOKIES_FILE", "")):
        _append_file(files, _existing_file(value, base_dir))
    return files


def cookie_file_for_url(url: str) -> Path | None:
    """Return the first configured cookie file for compatibility callers."""
    files = cookie_files_for_url(url)
    return files[0] if files else None
