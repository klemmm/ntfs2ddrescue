#!/usr/bin/env python3
"""Wrap ddrescue to stop as soon as it uncovers a valid MFT record.

Spawns a normal, unmodified ddrescue with whatever arguments you give it,
and watches ddrescue's own mapfile for newly finished ('+') byte ranges.
Each time new ground is covered, the new bytes are scanned for the "FILE"
signature at sector-aligned offsets, and any candidate is fully validated
with mft_parser.parse_mft_entry (which checks the record's fixup array
across every sector) before being accepted. On the first valid hit,
ddrescue is sent SIGINT -- exactly what Ctrl-C does -- so it shuts down
cleanly and leaves a valid, resumable mapfile.

All the actual disk reading, retrying, and bad-sector handling is done by
ddrescue itself; this only supervises it and reads its mapfile, so the
underlying ddrescue invocation is a completely ordinary one and can be
resumed/reused normally afterwards.

Usage:
    mft_hunt.py --image PATH --log PATH [--poll-interval SECONDS] \\
        -- <ddrescue arguments...>

--image and --log must match ddrescue's own <outfile> and <mapfile>
arguments (its 2nd and 3rd positional arguments) exactly, since that's
how this tool knows what to watch. Example:

    ./mft_hunt.py --image rescue.bin --log rescue.log \\
        -- -f /dev/sda rescue.bin rescue.log

If --log already has finished regions in it (resuming an earlier run),
those are scanned before ddrescue is even started, in case the answer is
already sitting in already-rescued data.
"""

import argparse
import signal
import subprocess
import sys
import time

from mft_parser import format_mft_entry, parse_mft_entry

STATUS_CHARS = set("?*/-+")
SCAN_CHUNK_SIZE = 64 * 1024 * 1024
SECTOR_SIZE = 512


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent (start, end) intervals."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def subtract_intervals(
    a: list[tuple[int, int]], b: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Return the parts of interval list `a` not covered by any of `b`."""
    remaining = list(a)
    for bs, be in b:
        next_remaining = []
        for s, e in remaining:
            if be <= s or bs >= e:
                next_remaining.append((s, e))
                continue
            if bs > s:
                next_remaining.append((s, bs))
            if be < e:
                next_remaining.append((be, e))
        remaining = next_remaining
    return remaining


def parse_finished_regions(log_path: str) -> list[tuple[int, int]]:
    """Read a ddrescue mapfile/log and return finished ('+') byte ranges
    as a merged list of (start, end) intervals. Returns [] if the log
    doesn't exist yet (ddrescue hasn't started writing it).
    """
    regions = []
    try:
        f = open(log_path)
    except FileNotFoundError:
        return []
    with f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split()
            # The "current_pos current_status [current_pass]" line has a
            # single status character as its 2nd field; a block line's
            # 2nd field is always a multi-character hex size. That's the
            # only reliable way to tell the two apart.
            if len(fields) == 3 and len(fields[1]) == 1 and fields[1] in STATUS_CHARS:
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
    return merge_intervals(regions)


def find_signature_candidates(img, start: int, end: int):
    """Yield sector-aligned absolute offsets of "FILE" byte matches in
    img[start:end), reading in bounded chunks (regardless of how large
    the range is) with a small carry-over so a match straddling a chunk
    boundary isn't missed.
    """
    signature = b"FILE"
    pos = start
    tail = b""
    while pos < end:
        img.seek(pos)
        chunk = img.read(min(SCAN_CHUNK_SIZE, end - pos))
        if not chunk:
            break
        buf = tail + chunk
        buf_start = pos - len(tail)
        search_from = 0
        while True:
            idx = buf.find(signature, search_from)
            if idx == -1:
                break
            abs_pos = buf_start + idx
            if abs_pos % SECTOR_SIZE == 0:
                yield abs_pos
            search_from = idx + 1
        tail = buf[-(len(signature) - 1):]
        pos += len(chunk)


def looks_like_real_mft_record(entry) -> bool:
    """Extra plausibility checks beyond parse_mft_entry() not raising.

    parse_mft_entry() only cross-checks the fixup array when fixup_count
    implies at least one sector to check; a record whose fixup_count
    happens to decode to 0 or 1 skips that check entirely, so a stray
    "FILE" match followed by unrelated bytes can still "parse" cleanly.
    These catch that gap for a signature found in otherwise-arbitrary
    disk data (as opposed to parse_mft_entry's other callers, which only
    ever call it on offsets already known by geometry to be real slots).
    """
    return (
        entry.fixup_count >= 3
        and entry.allocated_size == 1024
        and 0 < entry.used_size <= entry.allocated_size
        and entry.first_attr_offset <= entry.used_size
    )


def validate_candidate(img, offset: int):
    """Try to parse a full, plausible MFT record at `offset`. Returns the
    parsed entry on success, or None.
    """
    img.seek(offset)
    data = img.read(1024)
    if len(data) != 1024:
        return None
    try:
        entry = parse_mft_entry(data)
    except ValueError:
        return None
    return entry if looks_like_real_mft_record(entry) else None


def stop_process(proc: subprocess.Popen) -> None:
    """Ask ddrescue to stop the way Ctrl-C would, escalating if it doesn't."""
    proc.send_signal(signal.SIGINT)
    for timeout, escalate in ((10, proc.terminate), (5, proc.kill)):
        try:
            proc.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            escalate()
    proc.wait()


def main():
    parser = argparse.ArgumentParser(
        description="Run ddrescue and stop it automatically as soon as it "
        "uncovers a valid MFT record, instead of rescuing the whole disk.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--image",
        required=True,
        metavar="PATH",
        help="ddrescue's output image path (its 2nd positional argument) "
        "-- must match what you pass to ddrescue after '--'.",
    )
    parser.add_argument(
        "--log",
        required=True,
        metavar="PATH",
        help="ddrescue's own rescue mapfile (its 3rd positional argument) "
        "-- must match what you pass to ddrescue after '--'. If it "
        "already has finished regions (resuming a previous run), those "
        "are scanned before ddrescue is even started.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        metavar="SECONDS",
        help="How often to check ddrescue's mapfile for newly finished "
        "regions (default: 0.5).",
    )
    parser.add_argument(
        "--mapfile-save-interval",
        default="1s",
        metavar="INTERVAL",
        help="Passed to ddrescue as --mapfile-interval, unless you already "
        "gave your own --mapfile-interval after '--' (default: 1s). "
        "ddrescue's default 'auto' interval effectively only saves the "
        "mapfile at the end of a short, healthy, error-free run, which "
        "means this tool has nothing to react to until ddrescue is "
        "already done -- this makes sure there's something to poll.",
    )
    parser.add_argument(
        "ddrescue_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed straight through to ddrescue. Put them "
        "after '--', e.g.: -- -f /dev/sda rescue.bin rescue.log",
    )
    args = parser.parse_args()

    ddrescue_args = args.ddrescue_args
    if ddrescue_args and ddrescue_args[0] == "--":
        ddrescue_args = ddrescue_args[1:]
    if not any(a == "--mapfile-interval" or a.startswith("--mapfile-interval=") for a in ddrescue_args):
        ddrescue_args = [f"--mapfile-interval={args.mapfile_save_interval}", *ddrescue_args]
    if not ddrescue_args:
        print("Error: no ddrescue arguments given (put them after --)", file=sys.stderr)
        sys.exit(1)

    if args.image not in ddrescue_args:
        print(
            f"Warning: {args.image!r} doesn't appear in the ddrescue "
            "arguments -- make sure --image matches ddrescue's output file",
            file=sys.stderr,
        )
    if args.log not in ddrescue_args:
        print(
            f"Warning: {args.log!r} doesn't appear in the ddrescue "
            "arguments -- make sure --log matches ddrescue's mapfile",
            file=sys.stderr,
        )

    already_scanned: list[tuple[int, int]] = []

    def scan_new_regions(finished: list[tuple[int, int]]):
        nonlocal already_scanned
        new_regions = subtract_intervals(finished, already_scanned)
        hit = None
        if new_regions:
            with open(args.image, "rb") as img:
                for start, end in sorted(new_regions):
                    for candidate in find_signature_candidates(img, start, end):
                        entry = validate_candidate(img, candidate)
                        if entry is not None:
                            hit = (candidate, entry)
                            break
                    if hit:
                        break
        already_scanned = merge_intervals(already_scanned + new_regions)
        return hit

    def report_hit(offset: int, entry) -> None:
        print(f"\nFound a valid MFT record at offset {offset} (0x{offset:X})\n")
        print(format_mft_entry(entry))
        print(f"\nNext: ./map_mft.py <disk> 0x{offset:X}")

    # Resume-friendly: scan whatever's already finished before spawning
    # ddrescue at all, in case a previous run already covered the answer.
    hit = scan_new_regions(parse_finished_regions(args.log))
    if hit:
        report_hit(*hit)
        return

    print(f"Launching: ddrescue {' '.join(ddrescue_args)}")
    proc = subprocess.Popen(["ddrescue", *ddrescue_args])

    try:
        while True:
            if proc.poll() is not None:
                hit = scan_new_regions(parse_finished_regions(args.log))
                if hit:
                    report_hit(*hit)
                    sys.exit(0)
                print(
                    "ddrescue exited without finding an MFT signature "
                    f"(exit code {proc.returncode})",
                    file=sys.stderr,
                )
                sys.exit(1)

            time.sleep(args.poll_interval)
            hit = scan_new_regions(parse_finished_regions(args.log))
            if hit:
                offset, entry = hit
                stop_process(proc)
                report_hit(offset, entry)
                sys.exit(0)
    except KeyboardInterrupt:
        stop_process(proc)
        sys.exit(130)


if __name__ == "__main__":
    main()
