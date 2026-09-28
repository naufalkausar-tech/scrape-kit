from __future__ import annotations

import csv
import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import unquote, urlsplit

import requests

from .polite import PoliteSession, RobotsDisallowed

MANIFEST_FIELDS = ["url", "filename", "status", "bytes", "sha256", "http_status", "error", "finished_at"]
DONE_STATUSES = {"downloaded", "skipped"}
_UNSAFE_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+|\*)-?(\d*)/(\d+|\*)")


@dataclass
class DownloadItem:
    url: str
    filename: str | None = None
    sha256: str | None = None
    size: int | None = None


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def filename_from_url(url: str) -> str:
    name = unquote(PurePosixPath(urlsplit(url).path).name)
    return name or "download-" + hashlib.sha1(url.encode()).hexdigest()[:12]


def sanitize_relative_path(name: str) -> str:
    parts = []
    for part in re.split(r"[\\/]+", name):
        part = _UNSAFE_CHARS.sub("_", part).strip(" .")
        if part and part != "..":
            parts.append(part)
    if not parts:
        raise ValueError(f"unusable filename {name!r}")
    return "/".join(parts)


def load_manifest(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["url"]: row for row in csv.DictReader(handle) if row.get("url")}


def read_items(path: str | Path) -> list[DownloadItem]:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        items = []
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                url = (row.get("url") or "").strip()
                if not url:
                    continue
                size = (row.get("size") or "").strip()
                items.append(
                    DownloadItem(
                        url=url,
                        filename=(row.get("filename") or "").strip() or None,
                        sha256=(row.get("sha256") or "").strip().lower() or None,
                        size=int(size) if size else None,
                    )
                )
        return items
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return [DownloadItem(url=line.strip()) for line in lines if line.strip() and not line.lstrip().startswith("#")]


class BulkDownloader:
    def __init__(
        self,
        dest_dir: str | Path,
        manifest_path: str | Path | None = None,
        session: PoliteSession | None = None,
        chunk_size: int = 64 * 1024,
        resume_partial: bool = True,
    ):
        self.dest = Path(dest_dir).resolve()
        self.dest.mkdir(parents=True, exist_ok=True)
        self.manifest_path = Path(manifest_path) if manifest_path else self.dest / "manifest.csv"
        self.session = session or PoliteSession()
        self.chunk_size = chunk_size
        self.resume_partial = resume_partial
        self.previous = load_manifest(self.manifest_path)
        self._claimed = {row["filename"]: url for url, row in self.previous.items() if row.get("filename")}

    def run(self, items: Iterable[DownloadItem]) -> list[dict[str, str]]:
        results = []
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.manifest_path.exists()
        with self.manifest_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
            if new_file:
                writer.writeheader()
            for item in items:
                row = self.download(item)
                writer.writerow(row)
                handle.flush()
                results.append(row)
        self._rewrite_manifest(results)
        return results

    def target_path(self, item: DownloadItem) -> Path:
        name = sanitize_relative_path(item.filename or filename_from_url(item.url))
        owner = self._claimed.get(name)
        if owner is not None and owner != item.url and not item.filename:
            stem, dot, ext = name.rpartition(".")
            tag = hashlib.sha1(item.url.encode()).hexdigest()[:8]
            name = f"{stem}-{tag}.{ext}" if dot and stem else f"{name}-{tag}"
        path = (self.dest / name).resolve()
        if not path.is_relative_to(self.dest):
            raise ValueError(f"filename escapes destination: {name!r}")
        self._claimed[name] = item.url
        return path

    def download(self, item: DownloadItem) -> dict[str, str]:
        try:
            path = self.target_path(item)
        except ValueError as exc:
            return self._row(item, "", "error", error=str(exc))
        relative = path.relative_to(self.dest).as_posix()
        if path.exists():
            ok, digest = self._verify_existing(item, path)
            if ok:
                return self._row(item, relative, "skipped", size=path.stat().st_size, sha256=digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        return self._fetch(item, path, part, relative, allow_resume=self.resume_partial)

    def _verify_existing(self, item: DownloadItem, path: Path) -> tuple[bool, str]:
        size = path.stat().st_size
        if item.size is not None and size != item.size:
            return False, ""
        previous = self.previous.get(item.url)
        if item.sha256:
            digest = sha256_file(path)
            return digest == item.sha256.lower(), digest
        if previous and previous.get("status") in DONE_STATUSES and previous.get("bytes") == str(size):
            return True, previous.get("sha256", "")
        return True, sha256_file(path)

    def _fetch(self, item: DownloadItem, path: Path, part: Path, relative: str, allow_resume: bool) -> dict[str, str]:
        offset = part.stat().st_size if allow_resume and part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            response = self.session.get(item.url, headers=headers, stream=True)
        except RobotsDisallowed as exc:
            return self._row(item, relative, "blocked", error=str(exc))
        except requests.RequestException as exc:
            return self._row(item, relative, "error", error=f"{type(exc).__name__}: {exc}")
        with response:
            status = response.status_code
            if status == 416 and offset:
                total = self._content_range(response)[2]
                if total is not None and total == offset:
                    return self._finalize(item, path, part, relative, status, expected_total=total)
                part.unlink(missing_ok=True)
                return self._fetch(item, path, part, relative, allow_resume=False)
            if status not in (200, 206):
                return self._row(item, relative, "error", http_status=status, error=f"HTTP {status}")
            mode, expected_total = "wb", None
            if status == 206 and offset:
                start, _, total = self._content_range(response)
                if start != offset:
                    part.unlink(missing_ok=True)
                    return self._fetch(item, path, part, relative, allow_resume=False)
                mode, expected_total = "ab", total
            elif self._plain_length(response) is not None:
                expected_total = self._plain_length(response)
            try:
                with part.open(mode) as handle:
                    for chunk in response.iter_content(self.chunk_size):
                        if chunk:
                            handle.write(chunk)
            except (requests.RequestException, OSError) as exc:
                return self._row(item, relative, "partial", http_status=status, size=part.stat().st_size if part.exists() else 0, error=f"{type(exc).__name__}: {exc}")
            return self._finalize(item, path, part, relative, status, expected_total)

    def _finalize(self, item, path, part, relative, status, expected_total) -> dict[str, str]:
        size = part.stat().st_size
        if expected_total is not None and size < expected_total:
            return self._row(item, relative, "partial", http_status=status, size=size, error=f"got {size} of {expected_total} bytes")
        if expected_total is not None and size > expected_total:
            part.unlink(missing_ok=True)
            return self._row(item, relative, "error", http_status=status, error=f"got {size} bytes, expected {expected_total}")
        if item.size is not None and size != item.size:
            part.unlink(missing_ok=True)
            return self._row(item, relative, "size_mismatch", http_status=status, size=size, error=f"expected {item.size} bytes, got {size}")
        digest = sha256_file(part)
        if item.sha256 and digest != item.sha256.lower():
            part.unlink(missing_ok=True)
            return self._row(item, relative, "checksum_mismatch", http_status=status, size=size, sha256=digest, error=f"expected sha256 {item.sha256}")
        os.replace(part, path)
        return self._row(item, relative, "downloaded", http_status=status, size=size, sha256=digest)

    @staticmethod
    def _content_range(response: requests.Response) -> tuple[int | None, int | None, int | None]:
        match = _CONTENT_RANGE.match(response.headers.get("Content-Range", ""))
        if not match:
            return None, None, None
        start, end, total = match.groups()
        return (
            int(start) if start.isdigit() else None,
            int(end) if end.isdigit() else None,
            int(total) if total.isdigit() else None,
        )

    @staticmethod
    def _plain_length(response: requests.Response) -> int | None:
        encoding = response.headers.get("Content-Encoding", "identity").lower()
        length = response.headers.get("Content-Length", "")
        return int(length) if length.isdigit() and encoding in ("", "identity") else None

    @staticmethod
    def _row(item, filename, status, http_status=None, size=None, sha256="", error="") -> dict[str, str]:
        return {
            "url": item.url,
            "filename": filename,
            "status": status,
            "bytes": "" if size is None else str(size),
            "sha256": sha256 or "",
            "http_status": "" if http_status is None else str(http_status),
            "error": error,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def _rewrite_manifest(self, results: list[dict[str, str]]) -> None:
        merged = dict(load_manifest(self.manifest_path))
        for row in results:
            merged[row["url"]] = row
        temp = self.manifest_path.with_name(self.manifest_path.name + ".tmp")
        with temp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(merged.values())
        os.replace(temp, self.manifest_path)
