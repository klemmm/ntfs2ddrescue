#!/usr/bin/env python3
"""Generate a ddrescue domain mapfile from an mft_list.py file listing.

Takes the disk/image and the listing produced by 'mft_list.py -o' (full
path TAB comma-separated START-LENGTH byte ranges per file, with sparse
holes as 'sparse-LENGTH'), and writes a ddrescue domain mapfile that
restricts a rescue run to just the file content those ranges cover.
Sparse holes have no on-disk bytes, so they're skipped -- there's
nothing there for ddrescue to read. Regions separated by a small gap are
merged into one block, trading a bit of extra reading for fewer seeks.
"""

import argparse
import os
import sys


def parse_size(s: str) -> int:
    """Parse a size that may be decimal, hex (0x...), or have a suffix (k, m, g)."""
    s = s.strip().lower()
    multipliers = {"k": 1024, "m": 1024**2, "g": 1024**3}
    if s[-1:] in multipliers:
        return int(s[:-1], 0) * multipliers[s[-1]]
    return int(s, 0)


def parse_listing(path: str) -> list[tuple[int, int]]:
    """Extract every (start, length) byte range from an mft_list.py listing."""
    regions = []
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            _, _, region_str = line.partition("\t")
            if not region_str:
                continue
            for part in region_str.split(","):
                if part.startswith("sparse-"):
                    continue  # no on-disk bytes to rescue
                start_str, _, length_str = part.partition("-")
                if not start_str or not length_str:
                    continue
                regions.append((int(start_str, 0), int(length_str, 0)))
    return regions


def coalesce_regions(regions: list[tuple[int, int]], merge_gap: int) -> list[tuple[int, int]]:
    """Merge byte ranges that overlap or are within `merge_gap` bytes of
    each other into a single, larger range.
    """
    if not regions:
        return []
    merged = [list(r) for r in sorted(regions)]
    result = [merged[0]]
    for start, length in merged[1:]:
        last = result[-1]
        last_end = last[0] + last[1]
        end = start + length
        if start <= last_end + merge_gap:
            last[1] = max(last_end, end) - last[0]
        else:
            result.append([start, length])
    return [(start, length) for start, length in result]


def get_total_size(img) -> int:
    total_size = os.fstat(img.fileno()).st_size
    if total_size == 0:
        # stat() reports 0 for block devices; fall back to seeking to the
        # end, which the kernel resolves to the device's real capacity.
        saved_pos = img.tell()
        img.seek(0, os.SEEK_END)
        total_size = img.tell()
        img.seek(saved_pos)
    return total_size


def write_ddrescue_mapfile(path, total_size, regions):
    """Write a ddrescue domain mapfile marking `regions` as finished ('+').

    Everything outside `regions` is marked non-tried ('?'). Fed to
    ddrescue as `-m <path>`, this restricts a rescue run to exactly
    those byte ranges.
    """
    with open(path, "w") as out:
        out.write("# Mapfile. Created by map_files.py\n")
        out.write("0x00000000     ?                 1\n")
        out.write("#      pos        size  status\n")
        pos = 0
        for start, length in regions:
            if start > pos:
                out.write(f"0x{pos:08X}  0x{start - pos:08X}  ?\n")
            out.write(f"0x{start:08X}  0x{length:08X}  +\n")
            pos = start + length
        if pos < total_size:
            out.write(f"0x{pos:08X}  0x{total_size - pos:08X}  ?\n")


def main():
    parser = argparse.ArgumentParser(
        description="Generate a ddrescue domain mapfile that recovers "
        "just the file content listed by mft_list.py, coalescing nearby "
        "regions to cut down on seeks."
    )
    parser.add_argument("path", help="Path to file or device containing the volume")
    parser.add_argument(
        "listing",
        help="File listing produced by 'mft_list.py -o' "
        "(full path TAB comma-separated START-LENGTH byte ranges, "
        "sparse holes as sparse-LENGTH)",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        required=True,
        help="Write the ddrescue domain mapfile to PATH. Use it with "
        "'ddrescue -m PATH <source> <image> <rescue-mapfile>'.",
    )
    parser.add_argument(
        "--merge-gap",
        type=parse_size,
        default="2048k",
        metavar="SIZE",
        help="Merge regions separated by a gap no larger than this into "
        "a single ddrescue block, trading a bit of extra reading for "
        "fewer seeks. Supports hex (0x...) and suffixes (k, m, g). "
        "Default: 256k. Use 0 to disable merging.",
    )
    args = parser.parse_args()

    regions = parse_listing(args.listing)
    if not regions:
        print("Error: no file regions found in listing", file=sys.stderr)
        sys.exit(1)

    merged = coalesce_regions(regions, args.merge_gap)

    with open(args.path, "rb") as img:
        total_size = get_total_size(img)

    write_ddrescue_mapfile(args.output, total_size, merged)

    recovered_bytes = sum(length for _, length in merged)
    print(
        f"Wrote ddrescue domain mapfile to {args.output}: "
        f"{len(regions)} file regions coalesced into {len(merged)} blocks "
        f"({recovered_bytes} bytes, merge gap {args.merge_gap})"
    )


if __name__ == "__main__":
    main()
