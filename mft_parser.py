"""NTFS MFT entry parser."""

import struct
from dataclasses import dataclass, field, replace


@dataclass
class MFTAttribute:
    type_id: int
    length: int
    non_resident: bool
    name_length: int
    name_offset: int
    flags: int
    instance: int
    name: str
    # Resident attributes
    content_length: int | None = None
    content_offset: int | None = None
    content: bytes | None = None
    # Non-resident attributes
    start_vcn: int | None = None
    end_vcn: int | None = None
    datarun_offset: int | None = None
    allocated_size: int | None = None
    real_size: int | None = None
    initialized_size: int | None = None
    dataruns: list[tuple[int, int]] = field(default_factory=list)

    @property
    def type_name(self) -> str:
        return ATTRIBUTE_TYPES.get(self.type_id, f"Unknown(0x{self.type_id:X})")


ATTRIBUTE_TYPES = {
    0x10: "$STANDARD_INFORMATION",
    0x20: "$ATTRIBUTE_LIST",
    0x30: "$FILE_NAME",
    0x40: "$OBJECT_ID",
    0x50: "$SECURITY_DESCRIPTOR",
    0x60: "$VOLUME_NAME",
    0x70: "$VOLUME_INFORMATION",
    0x80: "$DATA",
    0x90: "$INDEX_ROOT",
    0xA0: "$INDEX_ALLOCATION",
    0xB0: "$BITMAP",
    0xC0: "$REPARSE_POINT",
    0xD0: "$EA_INFORMATION",
    0xE0: "$EA",
    0x100: "$LOGGED_UTILITY_STREAM",
}

MFT_ENTRY_FLAGS = {
    0x01: "IN_USE",
    0x02: "DIRECTORY",
}


@dataclass
class StandardInformation:
    creation_time: int
    modification_time: int
    mft_modification_time: int
    access_time: int
    file_attributes: int


@dataclass
class FileName:
    parent_ref: int
    parent_seq: int
    creation_time: int
    modification_time: int
    mft_modification_time: int
    access_time: int
    allocated_size: int
    real_size: int
    flags: int
    namespace: int
    name: str

    @property
    def namespace_name(self) -> str:
        return {0: "POSIX", 1: "Win32", 2: "DOS", 3: "Win32+DOS"}.get(
            self.namespace, f"Unknown({self.namespace})"
        )


@dataclass
class MFTEntry:
    signature: bytes
    fixup_offset: int
    fixup_count: int
    logfile_seq: int
    sequence: int
    hardlink_count: int
    first_attr_offset: int
    flags: int
    used_size: int
    allocated_size: int
    base_record_ref: int
    next_attr_id: int
    record_number: int | None
    attributes: list[MFTAttribute] = field(default_factory=list)

    @property
    def in_use(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def is_directory(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def flag_names(self) -> list[str]:
        return [name for bit, name in MFT_ENTRY_FLAGS.items() if self.flags & bit]


def _ntfs_time_to_unix(ntfs_time: int) -> float:
    """Convert NTFS timestamp (100ns intervals since 1601-01-01) to Unix timestamp."""
    return (ntfs_time - 116444736000000000) / 10_000_000


def _format_ntfs_time(ntfs_time: int) -> str:
    """Format an NTFS timestamp as ISO 8601."""
    import datetime

    if ntfs_time == 0:
        return "0 (not set)"
    try:
        ts = _ntfs_time_to_unix(ntfs_time)
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return f"0x{ntfs_time:016X} (out of range)"


def _parse_dataruns(data: bytes) -> list[tuple[int, int]]:
    """Parse non-resident attribute data runs.

    Returns list of (offset_clusters, length_clusters) tuples.
    Offsets are relative to the previous run (first run is absolute).
    """
    runs = []
    pos = 0
    while pos < len(data):
        header = data[pos]
        if header == 0:
            break
        len_size = header & 0x0F
        off_size = (header >> 4) & 0x0F
        pos += 1
        if pos + len_size + off_size > len(data):
            break
        length = int.from_bytes(data[pos : pos + len_size], "little", signed=False)
        pos += len_size
        if off_size > 0:
            offset = int.from_bytes(data[pos : pos + off_size], "little", signed=True)
            pos += off_size
        else:
            offset = 0  # sparse run
        runs.append((offset, length))
    return runs


def _parse_attribute(data: bytes, offset: int) -> MFTAttribute | None:
    """Parse a single MFT attribute at the given offset within data."""
    if offset + 16 > len(data):
        return None
    type_id = struct.unpack_from("<I", data, offset)[0]
    if type_id == 0xFFFFFFFF or type_id == 0:
        return None
    attr_len = struct.unpack_from("<I", data, offset + 4)[0]
    if attr_len < 16 or attr_len > len(data) - offset:
        return None
    non_resident = data[offset + 8] != 0
    name_length = data[offset + 9]
    name_offset = struct.unpack_from("<H", data, offset + 10)[0]
    flags = struct.unpack_from("<H", data, offset + 12)[0]
    instance = struct.unpack_from("<H", data, offset + 14)[0]

    attr_name = ""
    if name_length > 0 and name_offset + name_length * 2 <= attr_len:
        raw = data[offset + name_offset : offset + name_offset + name_length * 2]
        attr_name = raw.decode("utf-16-le", errors="replace")

    attr = MFTAttribute(
        type_id=type_id,
        length=attr_len,
        non_resident=non_resident,
        name_length=name_length,
        name_offset=name_offset,
        flags=flags,
        instance=instance,
        name=attr_name,
    )

    if non_resident:
        if offset + 64 > len(data):
            return attr
        (
            attr.start_vcn,
            attr.end_vcn,
            attr.datarun_offset,
            _comp_unit,
            _padding,
            attr.allocated_size,
            attr.real_size,
            attr.initialized_size,
        ) = struct.unpack_from("<QQHHIQQq", data, offset + 16)
        if attr.datarun_offset and offset + attr.datarun_offset < len(data):
            run_data = data[offset + attr.datarun_offset : offset + attr_len]
            attr.dataruns = _parse_dataruns(run_data)
    else:
        if offset + 22 > len(data):
            return attr
        attr.content_length = struct.unpack_from("<I", data, offset + 16)[0]
        attr.content_offset = struct.unpack_from("<H", data, offset + 20)[0]
        if attr.content_offset and attr.content_length:
            start = offset + attr.content_offset
            end = start + attr.content_length
            if end <= len(data):
                attr.content = data[start:end]

    return attr


def parse_standard_information(content: bytes) -> StandardInformation | None:
    if len(content) < 48:
        return None
    c, m, mm, a, flags = struct.unpack_from("<QQQQI", content, 0)
    return StandardInformation(
        creation_time=c,
        modification_time=m,
        mft_modification_time=mm,
        access_time=a,
        file_attributes=flags,
    )


def parse_file_name(content: bytes) -> FileName | None:
    if len(content) < 66:
        return None
    parent_ref_raw = struct.unpack_from("<Q", content, 0)[0]
    parent_ref = parent_ref_raw & 0x0000FFFFFFFFFFFF
    parent_seq = (parent_ref_raw >> 48) & 0xFFFF
    c, m, mm, a, alloc, real, flags = struct.unpack_from("<QQQQQQI", content, 8)
    name_len = content[64]
    namespace = content[65]
    name_bytes = content[66 : 66 + name_len * 2]
    name = name_bytes.decode("utf-16-le", errors="replace")
    return FileName(
        parent_ref=parent_ref,
        parent_seq=parent_seq,
        creation_time=c,
        modification_time=m,
        mft_modification_time=mm,
        access_time=a,
        allocated_size=alloc,
        real_size=real,
        flags=flags,
        namespace=namespace,
        name=name,
    )


@dataclass
class AttributeListEntry:
    type_id: int
    length: int
    start_vcn: int
    record_num: int
    record_seq: int
    instance: int
    name: str

    @property
    def type_name(self) -> str:
        return ATTRIBUTE_TYPES.get(self.type_id, f"Unknown(0x{self.type_id:X})")


def parse_attribute_list(content: bytes) -> list[AttributeListEntry]:
    """Parse $ATTRIBUTE_LIST content into its entries.

    Each entry names one attribute (or one fragment of a split non-resident
    attribute) and the MFT record that holds it.
    """
    entries = []
    pos = 0
    while pos + 26 <= len(content):
        type_id, length = struct.unpack_from("<IH", content, pos)
        if type_id == 0 or length < 26 or pos + length > len(content):
            break
        name_length = content[pos + 6]
        name_offset = content[pos + 7]
        start_vcn, record_ref, instance = struct.unpack_from(
            "<QQH", content, pos + 8
        )
        name = ""
        if name_length and pos + name_offset + name_length * 2 <= len(content):
            raw = content[pos + name_offset : pos + name_offset + name_length * 2]
            name = raw.decode("utf-16-le", errors="replace")
        entries.append(
            AttributeListEntry(
                type_id=type_id,
                length=length,
                start_vcn=start_vcn,
                record_num=record_ref & 0x0000FFFFFFFFFFFF,
                record_seq=(record_ref >> 48) & 0xFFFF,
                instance=instance,
                name=name,
            )
        )
        pos += length
    return entries


def runs_to_absolute(
    runs: list[tuple[int, int]],
) -> list[tuple[int | None, int]]:
    """Convert relative dataruns to (absolute_lcn, length); None = sparse."""
    abs_runs: list[tuple[int | None, int]] = []
    cur = 0
    for off, length in runs:
        if off == 0:
            abs_runs.append((None, length))
        else:
            cur += off
            abs_runs.append((cur, length))
    return abs_runs


def _runs_from_absolute(
    abs_runs: list[tuple[int | None, int]],
) -> list[tuple[int, int]]:
    """Convert (absolute_lcn, length) runs back to relative datarun form."""
    rel = []
    prev = 0
    for lcn, length in abs_runs:
        if lcn is None:
            rel.append((0, length))
        else:
            rel.append((lcn - prev, length))
            prev = lcn
    return rel


def _merge_attr_fragments(
    frags: list[MFTAttribute], warnings: list[str]
) -> MFTAttribute:
    """Merge VCN-ordered fragments of one non-resident attribute into one.

    The fragment covering VCN 0 carries the valid sizes; the others only
    extend the datarun list.
    """
    if len(frags) == 1:
        return frags[0]
    frags = sorted(frags, key=lambda a: a.start_vcn or 0)
    first = frags[0]
    abs_runs: list[tuple[int | None, int]] = []
    prev = None
    for frag in frags:
        if prev is not None and frag.start_vcn != (prev.end_vcn or 0) + 1:
            warnings.append(
                f"{first.type_name}: VCN gap between fragments "
                f"(end_vcn {prev.end_vcn} -> start_vcn {frag.start_vcn})"
            )
        abs_runs.extend(runs_to_absolute(frag.dataruns))
        prev = frag
    merged = replace(first, dataruns=_runs_from_absolute(abs_runs))
    merged.end_vcn = frags[-1].end_vcn
    return merged


def coalesce_mft_entry(
    base: MFTEntry,
    base_num: int,
    extensions: dict[int, MFTEntry],
    attr_list: list[AttributeListEntry] | None = None,
    strict: bool = True,
) -> tuple[MFTEntry, list[int], list[str]]:
    """Merge extension records' attributes into a copy of the base record.

    When the base's $ATTRIBUTE_LIST is available it drives the merge: each
    list entry is resolved to the attribute in the record it names, with
    sequence numbers checked to reject stale records.  Without it, all
    attributes of extensions whose base reference matches are appended.
    With strict=False, stale records (sequence mismatch, typical of
    deleted files whose records were freed) are used anyway, with a
    warning.
    Fragments of split non-resident attributes (same type and name) are
    stitched into a single attribute with a combined datarun list.

    Returns (merged_entry, used_extension_record_numbers, warnings).
    """
    warnings: list[str] = []
    used: set[int] = set()
    ordered: list[MFTAttribute] = []

    if attr_list:
        for ale in attr_list:
            if ale.record_num == base_num:
                src = base
            else:
                src = extensions.get(ale.record_num)
                if src is None:
                    warnings.append(
                        f"{ale.type_name} fragment in record "
                        f"{ale.record_num} not available"
                    )
                    continue
                if src.sequence != ale.record_seq:
                    action = "skipped" if strict else "using anyway"
                    warnings.append(
                        f"record {ale.record_num} is stale (sequence "
                        f"{src.sequence}, attribute list expects "
                        f"{ale.record_seq}); {action}"
                    )
                    if strict:
                        continue
            attr = next(
                (
                    a
                    for a in src.attributes
                    if a.type_id == ale.type_id
                    and a.instance == ale.instance
                    and a.name == ale.name
                ),
                None,
            )
            if attr is None:
                attr = next(
                    (
                        a
                        for a in src.attributes
                        if a.type_id == ale.type_id
                        and a.name == ale.name
                        and (a.start_vcn or 0) == ale.start_vcn
                    ),
                    None,
                )
            if attr is None:
                warnings.append(
                    f"{ale.type_name} (instance {ale.instance}) not found "
                    f"in record {ale.record_num}"
                )
                continue
            if ale.record_num != base_num:
                used.add(ale.record_num)
            ordered.append(attr)
        # The attribute list does not always list itself; keep it visible.
        al_attr = next(
            (a for a in base.attributes if a.type_id == 0x20), None
        )
        if al_attr is not None and al_attr not in ordered:
            pos = next(
                (
                    i
                    for i, a in enumerate(ordered)
                    if a.type_id > 0x20
                ),
                len(ordered),
            )
            ordered.insert(pos, al_attr)
    else:
        ordered = list(base.attributes)
        for num in sorted(extensions):
            ext = extensions[num]
            expected_seq = (ext.base_record_ref >> 48) & 0xFFFF
            if expected_seq and expected_seq != base.sequence:
                action = "skipped" if strict else "merging anyway"
                warnings.append(
                    f"extension record {num} refers to base sequence "
                    f"{expected_seq}, base has {base.sequence}; {action}"
                )
                if strict:
                    continue
            used.add(num)
            ordered.extend(ext.attributes)

    # Stitch fragments of split non-resident attributes together.
    slots: list[MFTAttribute | tuple[int, str]] = []
    groups: dict[tuple[int, str], list[MFTAttribute]] = {}
    for attr in ordered:
        if attr.non_resident:
            key = (attr.type_id, attr.name)
            if key in groups:
                groups[key].append(attr)
            else:
                groups[key] = [attr]
                slots.append(key)
        else:
            slots.append(attr)
    merged_attrs = [
        slot
        if isinstance(slot, MFTAttribute)
        else _merge_attr_fragments(groups[slot], warnings)
        for slot in slots
    ]

    merged = replace(base, attributes=merged_attrs)
    return merged, sorted(used), warnings


def parse_mft_entry(data: bytes) -> MFTEntry:
    """Parse a 1024-byte MFT entry and return an MFTEntry object."""
    if len(data) != 1024:
        raise ValueError(f"MFT entry must be 1024 bytes, got {len(data)}")

    signature = data[0:4]
    if signature not in (b"FILE", b"\x00\x00\x00\x00"):
        raise ValueError(
            f"Invalid MFT signature: {signature!r} (expected b'FILE')"
        )

    (
        fixup_offset,
        fixup_count,
        logfile_seq,
        sequence,
        hardlink_count,
        first_attr_offset,
        flags,
        used_size,
        allocated_size,
        base_record_ref,
        next_attr_id,
    ) = struct.unpack_from("<HHQHHHHIIQI", data, 4)

    record_number = None
    if fixup_offset >= 0x30:
        record_number = struct.unpack_from("<I", data, 0x2C)[0]

    # Apply fixup array
    if fixup_count > 1 and fixup_offset + fixup_count * 2 <= 1024:
        fixup_sig = struct.unpack_from("<H", data, fixup_offset)[0]
        data = bytearray(data)
        for i in range(1, fixup_count):
            sector_end = i * 512 - 2
            stored = struct.unpack_from("<H", data, sector_end)[0]
            if stored != fixup_sig:
                raise ValueError(
                    f"Fixup mismatch at sector {i}: expected 0x{fixup_sig:04X}, "
                    f"got 0x{stored:04X}"
                )
            replacement = struct.unpack_from("<H", data, fixup_offset + i * 2)[0]
            struct.pack_into("<H", data, sector_end, replacement)
        data = bytes(data)

    entry = MFTEntry(
        signature=signature,
        fixup_offset=fixup_offset,
        fixup_count=fixup_count,
        logfile_seq=logfile_seq,
        sequence=sequence,
        hardlink_count=hardlink_count,
        first_attr_offset=first_attr_offset,
        flags=flags,
        used_size=used_size,
        allocated_size=allocated_size,
        base_record_ref=base_record_ref,
        next_attr_id=next_attr_id,
        record_number=record_number,
    )

    offset = first_attr_offset
    while offset < min(used_size, 1024) - 4:
        attr = _parse_attribute(data, offset)
        if attr is None:
            break
        entry.attributes.append(attr)
        offset += attr.length

    return entry


def format_mft_entry(entry: MFTEntry, full_path_of=None) -> str:
    """Return a human-readable representation of a parsed MFT entry.

    full_path_of, if given, is called with each parsed FileName and must
    return the full path string to display for it.
    """
    lines = []
    lines.append(f"MFT Entry")
    lines.append(f"  Signature:       {entry.signature.decode(errors='replace')}")
    if entry.record_number is not None:
        lines.append(f"  Record Number:   {entry.record_number}")
    lines.append(f"  Sequence:        {entry.sequence}")
    lines.append(f"  Flags:           0x{entry.flags:04X} ({', '.join(entry.flag_names) or 'none'})")
    lines.append(f"  Hardlink Count:  {entry.hardlink_count}")
    lines.append(f"  Logfile Seq:     {entry.logfile_seq}")
    lines.append(f"  Used Size:       {entry.used_size}")
    lines.append(f"  Allocated Size:  {entry.allocated_size}")
    if entry.base_record_ref:
        lines.append(f"  Base Record Ref: {entry.base_record_ref}")
    lines.append(f"  Attributes:      {len(entry.attributes)}")

    for attr in entry.attributes:
        name_str = f' "{attr.name}"' if attr.name else ""
        resident = "non-resident" if attr.non_resident else "resident"
        lines.append(f"\n  [{attr.type_name}]{name_str} ({resident}, {attr.length} bytes)")

        if attr.non_resident:
            lines.append(f"    VCN range:       {attr.start_vcn} - {attr.end_vcn}")
            lines.append(f"    Allocated size:  {attr.allocated_size}")
            lines.append(f"    Real size:       {attr.real_size}")
            if attr.dataruns:
                lines.append(f"    Data runs:       {len(attr.dataruns)}")
                abs_offset = 0
                for offset_c, length_c in attr.dataruns:
                    abs_offset += offset_c
                    if offset_c == 0:
                        lines.append(f"      sparse: {length_c} clusters")
                    else:
                        lines.append(f"      offset={abs_offset}, length={length_c} clusters")
        else:
            if attr.content_length is not None:
                lines.append(f"    Content length:  {attr.content_length}")

        if attr.type_id == 0x10 and attr.content:
            si = parse_standard_information(attr.content)
            if si:
                lines.append(f"    Created:         {_format_ntfs_time(si.creation_time)}")
                lines.append(f"    Modified:        {_format_ntfs_time(si.modification_time)}")
                lines.append(f"    MFT Modified:    {_format_ntfs_time(si.mft_modification_time)}")
                lines.append(f"    Accessed:        {_format_ntfs_time(si.access_time)}")
                lines.append(f"    File Attributes: 0x{si.file_attributes:08X}")

        if attr.type_id == 0x30 and attr.content:
            fn = parse_file_name(attr.content)
            if fn:
                lines.append(f"    Name:            {fn.name}")
                if full_path_of is not None:
                    lines.append(f"    Full path:       {full_path_of(fn)}")
                lines.append(f"    Namespace:       {fn.namespace_name}")
                lines.append(f"    Parent Ref:      {fn.parent_ref} (seq {fn.parent_seq})")
                lines.append(f"    Created:         {_format_ntfs_time(fn.creation_time)}")
                lines.append(f"    Modified:        {_format_ntfs_time(fn.modification_time)}")
                lines.append(f"    Allocated Size:  {fn.allocated_size}")
                lines.append(f"    Real Size:       {fn.real_size}")

    return "\n".join(lines)
