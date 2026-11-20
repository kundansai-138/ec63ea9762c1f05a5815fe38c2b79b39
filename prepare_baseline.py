#!/usr/bin/env python3
"""Export the clean pinned Git tree and verify every archived Git blob.

The resulting baseline.tar and manifest.json can enter the map-builder image
without a Git installation. No sample patches, test numbers, or labels are read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile

PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def git(source: Path, *arguments: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(source), *arguments])


def verify_archive(archive: Path, files: dict[str, str]) -> None:
    """Check names, member types, exact contents, and complete tree coverage."""
    seen = set()
    with tarfile.open(archive, "r:") as contents:
        for member in contents:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                raise ValueError(f"unsafe archive member: {member.name}")
            if member.isdir():
                continue
            if member.name not in files or member.name in seen:
                raise ValueError(f"unexpected or duplicate archive member: {member.name}")
            if member.isfile():
                stream = contents.extractfile(member)
                if stream is None:
                    raise ValueError(f"unreadable archive member: {member.name}")
                payload = stream.read()
            elif member.issym():
                # Git stores a symbolic link's target as its blob contents.
                payload = member.linkname.encode("utf-8")
                target = PurePosixPath(member.linkname)
                if target.is_absolute() or ".." in target.parts:
                    raise ValueError(f"unsafe symbolic link: {member.name}")
            else:
                raise ValueError(f"unsupported archive member type: {member.name}")
            digest = hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()
            if digest != files[member.name]:
                raise ValueError(f"archive differs from Git blob: {member.name}")
            seen.add(member.name)
    missing = set(files) - seen
    if missing:
        raise ValueError(f"archive omits tracked files: {sorted(missing)[:5]}")


def prepare(source: Path, output: Path) -> dict:
    source, output = source.resolve(), output.resolve()
    if output == source or source in output.parents:
        raise ValueError("output directory must be outside the baseline checkout")
    actual = git(source, "rev-parse", "HEAD").decode("ascii").strip()
    if actual != PINNED_COMMIT:
        raise ValueError(f"expected {PINNED_COMMIT}, got {actual}")
    # Normalize working-copy CRLF when checking a Windows-created checkout.
    # Export below still uses the committed blobs, never working-copy contents.
    dirty = git(source, "-c", "core.autocrlf=true", "diff", "HEAD", "--name-only")
    if dirty.strip():
        raise ValueError("the baseline checkout has tracked modifications")
    files = {}
    for entry in git(source, "ls-tree", "-r", "-z", "--full-tree", "HEAD").split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, digest = metadata.split(b" ", 2)
        if kind == b"commit" or mode == b"160000":
            continue
        if kind != b"blob":
            raise ValueError(f"unexpected Git tree object: {kind!r}")
        files[name.decode("utf-8")] = digest.decode("ascii")
    if not files:
        raise ValueError("pinned tree contains no files")
    output.mkdir(parents=True, exist_ok=True)
    archive_temp = output / "baseline.tar.tmp"
    manifest_temp = output / "manifest.json.tmp"
    try:
        subprocess.run(
            ["git", "-C", str(source), "-c", "core.autocrlf=false", "-c", "core.eol=lf",
             "archive", "--format=tar", f"--output={archive_temp}", "HEAD"],
            check=True,
        )
        verify_archive(archive_temp, files)
        manifest = {"commit": PINNED_COMMIT, "files": files}
        manifest_temp.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(archive_temp, output / "baseline.tar")
        os.replace(manifest_temp, output / "manifest.json")
    finally:
        archive_temp.unlink(missing_ok=True)
        manifest_temp.unlink(missing_ok=True)
    return {"commit": PINNED_COMMIT, "files": len(files), "output_dir": str(output)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    options = parser.parse_args()
    try:
        result = prepare(options.source, options.output_dir)
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as error:
        parser.exit(1, f"Preparation failed: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
