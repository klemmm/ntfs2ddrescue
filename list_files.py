#!/usr/bin/env python3
"""List every file recorded in an NTFS MFT.

Takes a disk/image and the record-number -> absolute-offset list produced
by 'mft_gen_map.py -r', reads every record, and writes one line per actual
file to an output file: its full path (reconstructed by following
$FILE_NAME parent links, with unresolvable chains placed under /ORPHAN)
and the absolute on-disk byte ranges holding its data. A sparse hole in
the data (no on-disk bytes) is listed as 'sparse-LENGTH' instead of a
START-LENGTH range. Extension records are coalesced (via $ATTRIBUTE_LIST
and base_record_ref) so a file split across several MFT records is only
listed once.
"""

import argparse
import sys

from mft_parser import (
    coalesce_mft_entry,
    parse_attribute_list,
    parse_file_name,
    parse_mft_entry,
    parse_standard_information,
    runs_to_absolute,
)

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def is_plain_file(entry) -> bool:
    """True for an ordinary file: not a directory, and not a reparse
    point (symlink/junction -- it has no real file content of its own).
    """
    if entry.is_directory:
        return False
    si_attr = next((a for a in entry.attributes if a.type_id == 0x10), None)
    if si_attr is not None and si_attr.content:
        si = parse_standard_information(si_attr.content)
        if si is not None and si.file_attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            return False
    return True


def read_record_positions(path: str):
    """Parse a record-list file (as written by 'mft_gen_map.py -r').

    Returns (positions, cluster_size, volume_start); cluster_size and
    volume_start are None if the file predates the geometry header
    comments (older mft_gen_map.py, or hand-written).
    """
    positions = {}
    cluster_size = None
    volume_start = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                parts = line[1:].split()
                if len(parts) == 2 and parts[0] == "cluster_size":
                    cluster_size = int(parts[1], 0)
                elif len(parts) == 2 and parts[0] == "volume_start":
                    volume_start = int(parts[1], 0)
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            record_number = int(parts[0])
            offset = int(parts[1], 0)
            positions[record_number] = offset
    return positions, cluster_size, volume_start


def best_file_name(entry):
    """Pick the best $FILE_NAME attribute on an entry (Win32 preferred).

    A file commonly has both a Win32 long name and a DOS 8.3 short-name
    alias as separate $FILE_NAME attributes; prefer the Win32 one so the
    same file isn't effectively listed twice under two different names.
    """
    names = []
    for attr in entry.attributes:
        if attr.type_id == 0x30 and attr.content:
            fn = parse_file_name(attr.content)
            if fn is not None:
                names.append(fn)
    if not names:
        return None
    for fn in names:
        if fn.namespace in (1, 3):  # Win32, Win32+DOS
            return fn
    return names[0]


def index_extensions_by_base(entries: dict) -> dict[int, dict]:
    """Group extension records by the base record number they point to.

    Every extension record carries its own base_record_ref back to its
    base (record number in the low 48 bits, sequence in the high 16),
    independent of whether the base's own $ATTRIBUTE_LIST is readable.
    """
    by_base: dict[int, dict] = {}
    for record_number, entry in entries.items():
        if entry.base_record_ref != 0:
            base_num = entry.base_record_ref & 0x0000FFFFFFFFFFFF
            by_base.setdefault(base_num, {})[record_number] = entry
    return by_base


def coalesce_record(record_number, entry, entries, extensions_by_base):
    """Coalesce one base record with its extension records, if any.

    Returns (coalesced_entry, extensions_used) so callers can also see
    which physical records contributed attributes.
    """
    attribute_list_entries = None
    al_attr = next((a for a in entry.attributes if a.type_id == 0x20), None)
    if al_attr is not None and not al_attr.non_resident and al_attr.content is not None:
        attribute_list_entries = parse_attribute_list(al_attr.content)

    extensions = extensions_by_base.get(record_number, {})
    if attribute_list_entries or extensions:
        coalesced, _, warnings = coalesce_mft_entry(
            entry, record_number, extensions, attribute_list_entries
        )
#        for warning in warnings:
#            print(f"Warning: record {record_number}: {warning}", file=sys.stderr)
        return coalesced, extensions
    return entry, extensions


def data_locations(
    coalesced_entry, host_entries: dict, record_positions: dict, cluster_size: int, volume_start: int
) -> list[tuple[int | None, int]]:
    """Absolute (start, length) byte ranges holding a file's unnamed $DATA.

    A sparse datarun has no on-disk bytes to read; it's represented as
    (None, length) so callers can tell it apart from a real range and
    zero-fill it instead of reading from the image.
    """
    data_attr = next(
        (a for a in coalesced_entry.attributes if a.type_id == 0x80 and a.name == ""),
        None,
    )
    if data_attr is None:
        return []
    if data_attr.non_resident:
        regions = []
        for lcn, length in runs_to_absolute(data_attr.dataruns):
            if lcn is None:  # sparse: no on-disk bytes
                regions.append((None, length * cluster_size))
            else:
                regions.append((volume_start + lcn * cluster_size, length * cluster_size))
        return regions
    # Resident: the content lives inside whichever MFT record physically
    # holds this attribute (the base or one of its extensions); find it
    # by identity, then locate the attribute's own byte offset within it.
    for record_number, host_entry in host_entries.items():
        record_offset = host_entry.first_attr_offset
        for a in host_entry.attributes:
            if a is data_attr:
                pos = record_positions[record_number] + record_offset + (a.content_offset or 0)
                return [(pos, a.content_length or 0)]
            record_offset += a.length
    return []


def build_paths(name_info: dict, entries: dict) -> dict[int, str]:
    """Resolve each record's full path by following $FILE_NAME parent links
    up to the (self-referencing) root. A chain that hits a missing or
    unreadable parent, a stale parent sequence, or a cycle is placed
    under /ORPHAN instead of being silently dropped.
    """
    paths: dict[int, str] = {}
    for record_number in name_info:
        chain = []
        current = record_number
        visited = set()
        broken = False
        while True:
            if current not in name_info:
                broken = True
                break
            name, parent_ref, parent_seq = name_info[current]
            if current in visited:
                broken = True
                break
            visited.add(current)
            if current == parent_ref:
                # Self-referencing: this is the root. Confirm it's
                # self-consistent rather than a corrupted self-loop.
                if entries[current].sequence != parent_seq:
                    broken = True
                break
            chain.append(name)
            parent_entry = entries.get(parent_ref)
            if parent_entry is None or parent_entry.sequence != parent_seq:
                broken = True
                break
            current = parent_ref

        if broken:
            paths[record_number] = f"/ORPHAN/{record_number}_{name_info[record_number][0]}"
        else:
            chain.reverse()
            paths[record_number] = "/" + "/".join(chain)
    return paths


def main():
    parser = argparse.ArgumentParser(
        description="List every file in an NTFS MFT with its full path "
        "and on-disk data location, coalescing split (extension-record) "
        "attributes so each file appears once."
    )
    parser.add_argument("path", help="Path to file or device containing the volume")
    parser.add_argument(
        "records",
        help="Record list file produced by 'mft_gen_map.py -r' "
        "(includes cluster size and volume start, plus "
        "record_number / absolute_offset pairs)",
    )
    parser.add_argument(
        "--cluster-size",
        type=lambda s: int(s, 0),
        default=None,
        help="Cluster size in bytes. Only needed to override the value "
        "already embedded in the record list by mft_gen_map.py.",
    )
    parser.add_argument(
        "--volume-start",
        type=lambda s: int(s, 0),
        default=None,
        help="Volume start offset. Only needed to override the value "
        "already embedded in the record list by mft_gen_map.py.",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        required=True,
        help="Write the file listing to PATH: one line per file, "
        "'<full path>TAB<comma-separated START-LENGTH byte ranges, "
        "sparse holes as sparse-LENGTH>'.",
    )
    args = parser.parse_args()

    positions, file_cluster_size, file_volume_start = read_record_positions(args.records)
    if not positions:
        print("Error: no records found in record list", file=sys.stderr)
        sys.exit(1)

    cluster_size = args.cluster_size if args.cluster_size is not None else file_cluster_size
    volume_start = args.volume_start if args.volume_start is not None else file_volume_start
    if cluster_size is None or volume_start is None:
        print(
            "Error: cluster size / volume start not found in the record "
            "list (regenerate it with a current mft_gen_map.py, or pass "
            "--cluster-size/--volume-start explicitly)",
            file=sys.stderr,
        )
        sys.exit(1)
    # Record positions for resident files come straight from the record
    # list, baked in with whatever volume_start/cluster_size it was
    # generated with; non-resident locations are computed fresh with
    # whatever's in effect now. An override that disagrees with what's
    # embedded makes the two silently inconsistent with each other.
    if (
        (args.cluster_size is not None and file_cluster_size is not None and args.cluster_size != file_cluster_size)
        or (args.volume_start is not None and file_volume_start is not None and args.volume_start != file_volume_start)
    ):
        print(
            "Warning: --cluster-size/--volume-start override disagrees "
            "with the geometry embedded in the record list; resident "
            "files' locations (read from the record list) and "
            "non-resident files' locations (recomputed with the override) "
            "may end up inconsistent",
            file=sys.stderr,
        )

    # Parse every record up front, keyed by its positional record number
    # (the same numbering the $ATTRIBUTE_LIST's record references use), so
    # extension records are available for coalescing regardless of the
    # order base records are processed in.
    entries = {}
    with open(args.path, "rb") as img:
        for record_number, pos in positions.items():
            img.seek(pos)
            data = img.read(1024)
            if len(data) != 1024:
                print(
                    f"Warning: record {record_number} (offset 0x{pos:X}): "
                    "short read, skipped",
                    file=sys.stderr,
                )
                continue
            try:
                entry = parse_mft_entry(data)
            except ValueError as e:
                print(
                    f"Warning: record {record_number} (offset 0x{pos:X}): {e}",
                    file=sys.stderr,
                )
                continue
            if (
                entry.in_use
                and entry.record_number is not None
                and entry.record_number != record_number
            ):
                print(
                    f"Warning: record {record_number} (offset 0x{pos:X}): "
                    f"embedded record number is {entry.record_number}, "
                    "geometry/record list may be misaligned",
                    file=sys.stderr,
                )
            entries[record_number] = entry

    extensions_by_base = index_extensions_by_base(entries)

    # Coalesce every base record -- live or freed, since a freed directory
    # can still serve as a resolvable path component for a live child --
    # and collect its name/parent for path building plus its data location.
    name_info = {}  # record_number -> (name, parent_ref, parent_seq)
    locations = {}  # record_number -> [(start, length), ...]
    live_records = set()  # record_number of base records to actually list
    for record_number, entry in sorted(entries.items()):
        if entry.base_record_ref != 0:
            continue  # extension record: not a file/directory on its own

        coalesced, extensions = coalesce_record(record_number, entry, entries, extensions_by_base)
        fn = best_file_name(coalesced)
        if fn is None:
            continue
        name_info[record_number] = (fn.name, fn.parent_ref, fn.parent_seq)

        # Directories (and reparse points) still need to be in name_info
        # above so live children can resolve paths through them, but they
        # aren't plain files themselves and don't get their own line.
        if entry.in_use and is_plain_file(coalesced):
            live_records.add(record_number)
            host_entries = {record_number: entry, **extensions}
            locations[record_number] = data_locations(
                coalesced, host_entries, positions, cluster_size, volume_start
            )

    paths = build_paths(name_info, entries)

    lines = []
    for record_number in live_records:
        region_str = ",".join(
            f"sparse-0x{length:X}" if start is None else f"0x{start:X}-0x{length:X}"
            for start, length in locations[record_number]
        )
        lines.append((paths[record_number], region_str))
    lines.sort()

    with open(args.output, "w") as out:
        for path, region_str in lines:
            out.write(f"{path}\t{region_str}\n")

    print(f"Wrote {len(lines)} files to {args.output}")


if __name__ == "__main__":
    main()
