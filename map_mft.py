#!/usr/bin/env python3
"""Read and parse an NTFS MFT entry from a file or device."""

import argparse
import os
import sys

from mft_parser import format_mft_entry, parse_mft_entry, parse_attribute_list, coalesce_mft_entry, runs_to_absolute, ATTRIBUTE_TYPES


def parse_offset(offset_str: str) -> int:
    """Parse an offset that may be decimal, hex (0x...), or have a suffix (k, m, g)."""
    offset_str = offset_str.strip().lower()
    multipliers = {"k": 1024, "m": 1024**2, "g": 1024**3}
    if offset_str[-1:] in multipliers:
        return int(offset_str[:-1], 0) * multipliers[offset_str[-1]]
    return int(offset_str, 0)


def _run_extent(dataruns: list[tuple[int, int]]) -> int:
    """Highest cluster (LCN) past the end of a relative datarun list.

    Sparse runs have no LCN and are skipped.
    """
    return max(
        (lcn + length for lcn, length in runs_to_absolute(dataruns) if lcn is not None),
        default=0,
    )


def write_ddrescue_mapfile(path, total_size, cluster_size, volume_start, dataruns):
    """Write a ddrescue domain mapfile covering only the given dataruns.

    Byte ranges inside `dataruns` are marked finished ('+'); everything
    else is marked non-tried ('?'). Fed to ddrescue as `-m <path>`, this
    restricts a rescue run to just those byte ranges (e.g. the MFT),
    since ddrescue's domain is the set of blocks marked finished in the
    domain mapfile.
    """
    byte_ranges = []
    for lcn, length in runs_to_absolute(dataruns):
        if lcn is None:  # sparse run: no on-disk bytes to rescue
            continue
        byte_ranges.append((volume_start + lcn * cluster_size, length * cluster_size))
    byte_ranges.sort()

    with open(path, "w") as out:
        out.write("# Mapfile. Created by map_mft.py\n")
        out.write("0x00000000     ?                 1\n")
        out.write("#      pos        size  status\n")
        pos = 0
        for start, length in byte_ranges:
            if start > pos:
                out.write(f"0x{pos:08X}  0x{start - pos:08X}  ?\n")
            out.write(f"0x{start:08X}  0x{length:08X}  +\n")
            pos = start + length
        if pos < total_size:
            out.write(f"0x{pos:08X}  0x{total_size - pos:08X}  ?\n")


def parse_finished_regions(log_path: str) -> list[tuple[int, int]]:
    """Read a ddrescue mapfile/log and return finished ('+') byte ranges
    as a merged, sorted list of (start, end) intervals.
    """
    status_chars = set("?*/-+")
    regions = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split()
            # The "current_pos current_status [current_pass]" line has a
            # single status character as its 2nd field; a block line's
            # 2nd field is always a multi-character hex size. That's the
            # only reliable way to tell the two apart.
            if len(fields) == 3 and len(fields[1]) == 1 and fields[1] in status_chars:
                continue
            if len(fields) < 3:
                continue
            try:
                pos = int(fields[0], 0)
                size = int(fields[1], 0)
            except ValueError:
                continue
            if fields[2] == "+":
                regions.append((pos, pos + size))
    regions.sort()
    merged = []
    for start, end in regions:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


class LayeredReader:
    """Reads from a ddrescue-recovered image when the requested bytes are
    marked finished there (per its mapfile), falling back to the raw
    disk otherwise -- so already-rescued data doesn't need re-reading
    from a slow or failing source.
    """

    def __init__(self, disk, rescue_image_path=None, rescue_log_path=None):
        self.disk = disk
        self.rescue_image = None
        self.finished_regions = []
        if rescue_image_path and rescue_log_path:
            self.rescue_image = open(rescue_image_path, "rb")
            self.finished_regions = parse_finished_regions(rescue_log_path)

    def _covered(self, pos: int, length: int) -> bool:
        end = pos + length
        for region_start, region_end in self.finished_regions:
            if region_start > pos:
                break  # sorted: no later region can cover an earlier pos
            if region_start <= pos and end <= region_end:
                return True
        return False

    def read(self, pos: int, length: int) -> bytes:
        if self.rescue_image is not None and self._covered(pos, length):
            self.rescue_image.seek(pos)
            print("Reader: Read from image")
            data = self.rescue_image.read(length)
            if len(data) == length:
                return data
        self.disk.seek(pos)
        print("Reader: Read from disk")
        return self.disk.read(length)


def write_record_list(path, cluster_size, volume_start, dataruns):
    """Write every MFT record's number and absolute disk offset to PATH.

    MFT records are fixed 1024-byte slots packed sequentially into the
    $MFT's data stream (record N sits at logical byte N*1024 of that
    stream). This walks `dataruns` in stream order, converting each
    slot's logical position into an absolute on-disk byte offset.
    """
    entries_per_cluster = cluster_size // 1024
    with open(path, "w") as out:
        out.write(f"# cluster_size {cluster_size}\n")
        out.write(f"# volume_start 0x{volume_start:X}\n")
        out.write("# record_number  absolute_offset\n")
        vcn = 0
        for lcn, length in runs_to_absolute(dataruns):
            if lcn is None:  # sparse run: no records stored here
                vcn += length
                continue
            for cluster_index in range(length):
                cluster_pos = volume_start + (lcn + cluster_index) * cluster_size
                cluster_vcn = vcn + cluster_index
                for slot in range(entries_per_cluster):
                    record_number = cluster_vcn * entries_per_cluster + slot
                    record_pos = cluster_pos + slot * 1024
                    out.write(f"{record_number}  0x{record_pos:X}\n")
            vcn += length


def main():
    parser = argparse.ArgumentParser(description="Read and parse an NTFS MFT entry")
    parser.add_argument("path", help="Path to file or device containing MFT data")
    parser.add_argument(
        "offset",
        nargs="?",
        default="0",
        help="Byte offset to the MFT entry (default: 0). "
        "Supports hex (0x...) and suffixes (k, m, g)",
    )
    parser.add_argument(
        "--mft",
        choices=["main", "mirror"],
        default=None,
        help="Force interpretation of the given offset as the live $MFT "
        "('main') or the $MFTMirr backup ('mirror'), instead of "
        "auto-detecting. Use this when auto-detection reports both "
        "candidates as plausible.",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        default=None,
        help="Write a ddrescue domain mapfile to PATH that restricts a "
        "rescue to just the recovered $MFT byte ranges. Use it with "
        "'ddrescue -m PATH <source> <image> <rescue-mapfile>'.",
    )
    parser.add_argument(
        "-r",
        "--records",
        metavar="PATH",
        default=None,
        help="Write the list of every $MFT record number and its "
        "absolute on-disk byte offset to PATH.",
    )
    parser.add_argument(
        "--rescue-image",
        metavar="PATH",
        default=None,
        help="A ddrescue-recovered image of (part of) this disk. Used "
        "together with --rescue-log: every read this tool needs first "
        "tries this image (if the bytes are marked finished there), and "
        "only falls back to the disk itself when they aren't.",
    )
    parser.add_argument(
        "--rescue-log",
        metavar="PATH",
        default=None,
        help="The ddrescue mapfile/log that goes with --rescue-image.",
    )
    args = parser.parse_args()

    if bool(args.rescue_image) != bool(args.rescue_log):
        print("Error: --rescue-image and --rescue-log must be given together", file=sys.stderr)
        sys.exit(1)

    offset = parse_offset(args.offset)
    disk = open(args.path, "rb")
    reader = LayeredReader(disk, args.rescue_image, args.rescue_log)

    # NTFS stores $MFT as record #0 and $MFTMirr as record #1, back to
    # back. Both the live $MFT and the $MFTMirr backup start with a copy
    # of these same two records, so reading 2 KiB here works whichever
    # one `offset` actually points at.
    print("Reading MFT and MFTMirror entries at: ", hex(offset))
    mft_bytes = reader.read(offset, 1024)
    mftmirr_bytes = reader.read(offset + 1024, 1024)

    if len(mft_bytes) + len(mftmirr_bytes) < 2048:
        print(
            f"Error: Only read {len(mft_bytes) + len(mftmirr_bytes)} bytes at offset {offset} "
            f"(need 2048)",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        mft_entry = parse_mft_entry(mft_bytes)
        mftmirr_entry = parse_mft_entry(mftmirr_bytes)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print("Using MFT start offset: ", hex(offset))

    # --- Derive cluster size and the $MFT data run start from record #0 ---
    has_split = False  # currently unused
    mft_lcn = None
    cluster_size_votes = set()
    has_attribute_list = False
    for attr in mft_entry.attributes:
        if attr.non_resident:
            # Cluster size = attribute's total bytes / total clusters it
            # occupies. Only valid when this attribute isn't split across
            # extension records (see $ATTRIBUTE_LIST handling below), so
            # every non-resident attribute "votes" and we require
            # unanimous agreement on a power-of-two result.
            candidate_cluster_size = attr.allocated_size // (attr.end_vcn + 1)
            if (candidate_cluster_size & (candidate_cluster_size - 1)) == 0:
                cluster_size_votes.add(candidate_cluster_size)
        if ATTRIBUTE_TYPES[attr.type_id] == "$ATTRIBUTE_LIST":
            has_attribute_list = True
            attribute_list_attr = attr
        if ATTRIBUTE_TYPES[attr.type_id] == "$DATA":
            mft_data_runs = attr.dataruns
            mft_lcn = attr.dataruns[0][0]
    if len(cluster_size_votes) != 1:
        print("Failed to determine cluster size")
        sys.exit(1)
    cluster_size = list(cluster_size_votes)[0]
    print("Cluster size is: ", cluster_size)
    if mft_lcn is None:
        print("Failed to determine $MFT data run start")
        sys.exit(1)
    print("MFT data run start is: ", mft_lcn)

    # --- Derive the $MFTMirr data run start from record #1 ---
    mftmirr_lcn = None
    mftmirr_data_runs = None
    for attr in mftmirr_entry.attributes:
        if ATTRIBUTE_TYPES[attr.type_id] == "$DATA":
            mftmirr_lcn = attr.dataruns[0][0]
            mftmirr_data_runs = attr.dataruns
    if mftmirr_lcn is None:
        print("Failed to determine $MFT mirror data run start")

    print("MFT mirror data run start is: ", mftmirr_lcn)

    # mft_lcn / mftmirr_lcn are hypothesis-independent (both
    # copies of records 0 and 1 are byte-identical wherever we read them
    # from), so the only unknown is whether `offset` sits at the live $MFT
    # or at the $MFTMirr backup. Build both candidates and reject any that
    # can't physically fit in the image, rather than guessing by magnitude.
    total_size = os.fstat(disk.fileno()).st_size
    if total_size == 0:
        # stat() reports 0 for block devices; fall back to seeking to the
        # end, which the kernel resolves to the device's real capacity.
        saved_pos = disk.tell()
        disk.seek(0, os.SEEK_END)
        total_size = disk.tell()
        disk.seek(saved_pos)
    mft_extent = _run_extent(mft_data_runs)
    mftmirr_extent = _run_extent(mftmirr_data_runs) if mftmirr_data_runs else 0

    def fits_in_image(volume_start_candidate):
        if volume_start_candidate is None or volume_start_candidate < 0:
            return False
        if volume_start_candidate + mft_extent * cluster_size > total_size:
            return False
        if mftmirr_data_runs and volume_start_candidate + mftmirr_extent * cluster_size > total_size:
            return False
        return True

    volume_start_main = offset - mft_lcn * cluster_size
    volume_start_mirr = (
        offset - mftmirr_lcn * cluster_size
        if mftmirr_lcn is not None
        else None
    )

    # Resolve which physical location `offset` points at: forced via
    # --mft, or auto-detected by keeping only the candidate(s) whose
    # required extents actually fit inside the image.
    if args.mft == "main":
        volume_start = volume_start_main
        print("Volume start (using MFT, forced by --mft main): ", hex(volume_start))
    elif args.mft == "mirror":
        if volume_start_mirr is None:
            print("Cannot force --mft mirror: no MFT mirror data run found")
            sys.exit(1)
        volume_start = volume_start_mirr
        print("Volume start (using MFTMirror, forced by --mft mirror): ", hex(volume_start))
    else:
        candidates = []
        if fits_in_image(volume_start_main):
            candidates.append(("MFT", volume_start_main))
        if fits_in_image(volume_start_mirr):
            candidates.append(("MFTMirror", volume_start_mirr))

        if len(candidates) == 0:
            print("Failed to determine volume start: no candidate fits within the image")
            sys.exit(1)
        elif len(candidates) > 1:
            print("Both MFT and MFTMirror candidates are plausible (ambiguous):")
            for candidate_label, candidate_volume_start in candidates:
                print(f"  {candidate_label}: {hex(candidate_volume_start)}")
            print("Re-run with --mft main or --mft mirror to disambiguate.")
            sys.exit(1)
        else:
            candidate_label, volume_start = candidates[0]
            print(f"Volume start (using {candidate_label}): ", hex(volume_start))

    if volume_start < 0:
        raise ValueError("Invalid volume start")
    if has_attribute_list:
        # The $MFT's own record ran out of space for all its attributes,
        # so some (typically the $DATA runs covering the rest of the MFT)
        # were pushed out into extension records, indexed by
        # $ATTRIBUTE_LIST.
        print("MFT entry has split attributes")
        extension_records = {}
        if attribute_list_attr.non_resident:
            attribute_list_data = b''
            for attr_list_lcn, run_length in runs_to_absolute(attribute_list_attr.dataruns):
                attr_list_pos = volume_start + attr_list_lcn * cluster_size
                print("Reading split attribute data at: ", hex(attr_list_pos))
                attribute_list_data += reader.read(attr_list_pos, run_length * cluster_size)
        else:
            attribute_list_data = attribute_list_attr.content

        attribute_list_entries = parse_attribute_list(attribute_list_data)
        data_is_split = False
        for ale in attribute_list_entries:
            if (ATTRIBUTE_TYPES[ale.type_id] == "$DATA") and ale.record_num != mft_entry.record_number:
                data_is_split = True
                # MFT records are fixed 1024-byte slots packed sequentially
                # into $MFT's data stream. Locate which cluster (VCN) and
                # which slot within that cluster holds this record number.
                entries_per_cluster = cluster_size // 1024
                vcn = ale.record_num // entries_per_cluster
                entry_index_in_cluster = ale.record_num % entries_per_cluster
                run_start_vcn = 0
                run_found = False
                run_start_lcn = None
                # Walk the $MFT data runs (converted to absolute LCNs)
                # until we reach the run that contains the target VCN.
                for lcn, run_length in runs_to_absolute(mft_data_runs):
                    if run_start_vcn + run_length > vcn:
                        run_found = True
                        run_start_lcn = lcn
                        break
                    run_start_vcn += run_length
                if not run_found:
                    raise ValueError("Extension record not indexed (should not happen) " + str(ale.record_num))
                ext_pos = (run_start_lcn + (vcn - run_start_vcn)) * cluster_size + volume_start + 1024 * entry_index_in_cluster
                print("Reading MFT extension records at: ", hex(ext_pos))
                extension_data = reader.read(ext_pos, 1024)
                extension_records[ale.record_num] = parse_mft_entry(extension_data)

        if data_is_split:
            (coalesced, _, warnings) = coalesce_mft_entry(mft_entry, mft_entry.record_number, extension_records, attribute_list_entries)
            print(warnings)
            for attr in coalesced.attributes:
                if ATTRIBUTE_TYPES[attr.type_id] == "$DATA":
                    mft_data_runs = attr.dataruns

    if args.output:
        write_ddrescue_mapfile(args.output, total_size, cluster_size, volume_start, mft_data_runs)
        print(f"Wrote ddrescue domain mapfile to {args.output}")

    if args.records:
        write_record_list(args.records, cluster_size, volume_start, mft_data_runs)
        print(f"Wrote MFT record list to {args.records}")


if __name__ == "__main__":
    main()
