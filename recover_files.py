#!/usr/bin/env python3
"""Recover actual file content from an mft_list.py listing.

Takes the disk/image, the listing produced by 'mft_list.py -o' (full path
TAB comma-separated START-LENGTH byte ranges per file, with sparse holes
as 'sparse-LENGTH'), and an output directory, then reads each file's
content straight off the disk at those byte ranges (zero-filling sparse
holes instead of reading them) and writes it out under the same path,
recreating the directory tree (including /ORPHAN) inside the output
directory.
"""

import argparse
import os
import sys

CHUNK_SIZE = 1024 * 1024
SPARSE_PREFIX = "sparse-"


def parse_region_str(region_str: str) -> list[tuple[int | None, int]]:
    """Parse one file's region field into (start, length) tuples.

    start is None for a sparse hole (no on-disk bytes; zero-fill it).
    """
    regions = []
    if not region_str:
        return regions
    for part in region_str.split(","):
        if part.startswith(SPARSE_PREFIX):
            length_str = part[len(SPARSE_PREFIX):]
            if not length_str:
                continue
            regions.append((None, int(length_str, 0)))
            continue
        start_str, _, length_str = part.partition("-")
        if not start_str or not length_str:
            continue
        regions.append((int(start_str, 0), int(length_str, 0)))
    return regions


def read_listing(path: str):
    """Yield (full_path, regions) for every line in an mft_list.py listing."""
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            full_path, _, region_str = line.partition("\t")
            yield full_path, parse_region_str(region_str)


def sanitize_component(name: str) -> str:
    """Neutralize a path component recovered from (untrusted, possibly
    corrupted) disk data so it can't escape the output directory or
    contain characters the filesystem chokes on.
    """
    name = name.replace("\x00", "_")
    if name in (".", ".."):
        name = "_" + name
    return name


def safe_output_path(output_dir: str, recovered_path: str) -> str:
    """Map a recovered NTFS path onto a path guaranteed to stay inside
    output_dir, however the recovered path is composed.
    """
    components = [sanitize_component(c) for c in recovered_path.split("/") if c]
    return os.path.join(output_dir, *components) if components else output_dir


def copy_region(img, out, start: int, length: int) -> int:
    """Copy `length` bytes from `img` at `start` to `out`, zero-filling
    whatever can't be read (I/O error, or a source that's truncated/
    shorter than expected) so the file's internal byte alignment is
    preserved. Returns the number of bytes that had to be zero-filled.
    """
    remaining = length
    try:
        img.seek(start)
    except OSError:
        out.write(b"\x00" * remaining)
        return remaining

    while remaining > 0:
        try:
            chunk = img.read(min(CHUNK_SIZE, remaining))
        except OSError:
            chunk = b""
        if not chunk:
            break
        out.write(chunk)
        remaining -= len(chunk)

    if remaining > 0:
        out.write(b"\x00" * remaining)
    return remaining


def recover_file(img, out_path: str, regions: list[tuple[int | None, int]]) -> tuple[int, int]:
    """Write one file's content to out_path. Returns (total_bytes, missing_bytes).

    A sparse region (start is None) is zero-filled directly; it was never
    on disk, so it doesn't count toward missing_bytes.
    """
    total = sum(length for _, length in regions)
    missing = 0
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as out:
        for start, length in regions:
            if start is None:
                out.write(b"\x00" * length)
            else:
                missing += copy_region(img, out, start, length)
    return total, missing


def main():
    parser = argparse.ArgumentParser(
        description="Recover actual file content from an NTFS volume "
        "using the listing produced by mft_list.py."
    )
    parser.add_argument("path", help="Path to file or device containing the volume")
    parser.add_argument(
        "listing",
        help="File listing produced by 'mft_list.py -o' "
        "(full path TAB comma-separated START-LENGTH byte ranges, "
        "sparse holes as sparse-LENGTH)",
    )
    parser.add_argument("output_dir", help="Directory to recover files into")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    recovered = 0
    skipped = 0
    total_missing_bytes = 0
    with open(args.path, "rb") as img:
        for full_path, regions in read_listing(args.listing):
            target = safe_output_path(args.output_dir, full_path)

            if not regions:
                # No $DATA: almost always a directory. Recreate it so the
                # tree is complete even where it holds no recoverable data.
                try:
                    os.makedirs(target, exist_ok=True)
                except OSError as e:
                    print(f"Warning: {full_path}: could not create directory: {e}", file=sys.stderr)
                continue

            try:
                total, missing = recover_file(img, target, regions)
            except OSError as e:
                print(f"Warning: {full_path}: could not write file: {e}", file=sys.stderr)
                skipped += 1
                continue

            recovered += 1
            total_missing_bytes += missing
            if missing:
                print(
                    f"Warning: {full_path}: {missing} of {total} bytes "
                    "unrecoverable (zero-filled)",
                    file=sys.stderr,
                )

    print(
        f"Recovered {recovered} files to {args.output_dir} "
        f"({skipped} skipped, {total_missing_bytes} bytes unrecoverable)"
    )


if __name__ == "__main__":
    main()
