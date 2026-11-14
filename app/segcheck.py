"""DICOM SEG 核查：外部标注组的多帧 BINARY SEG 与已取回批的对账。

核查规则
========
* 仅接受**未压缩**（Implicit/Explicit VR Little Endian）、**多帧**、
  ``SegmentationType=BINARY`` 的 Segmentation Storage 文件。
* 每帧必须经 DerivationImageSequence 引用**本批恰好一个**已登记 CT：
  无引用或引用清单外 SOP 记“缺失引用”；引用 ≥2 个不同 SOP 记“歧义引用”。
* SEG 的 Rows/Columns 必须与被引用 CT 的行列一致。
* 同一标签对同一切片的多帧只计一次；全零掩膜帧不计入标注。
* 未知标签、缺失/歧义引用、行列不匹配、像素解码失败：
  指出帧号（1 起）并拒绝给出完整结论。
* 核查期间批内源文件丢失或被替换（快照 + 复核指纹）：同样不能报完整。
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass

import pydicom
from pydicom.dataset import Dataset
from pydicom.errors import InvalidDicomError
from pydicom.uid import (
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    SegmentationStorage,
)

from .db import Database

UNCOMPRESSED_TS = {str(ImplicitVRLittleEndian), str(ExplicitVRLittleEndian)}
SEG_SOP_CLASS = str(SegmentationStorage)

# ---- 帧级错误 kind（响应中原样返回，便于下游程序化处理）----
ERR_UNKNOWN_SEGMENT = "UNKNOWN_SEGMENT"            # 帧引用了标签表外的 SegmentNumber
ERR_AMBIGUOUS_SEGMENT = "AMBIGUOUS_SEGMENT"        # 一帧引用多个标签
ERR_MISSING_REFERENCE = "MISSING_REFERENCE"        # 无源引用或引用清单外 SOP
ERR_AMBIGUOUS_REFERENCE = "AMBIGUOUS_REFERENCE"    # 一帧引用多个不同源 SOP
ERR_DIMENSION_MISMATCH = "DIMENSION_MISMATCH"      # SEG 与源 CT 行列不一致
ERR_PIXEL_DECODE = "PIXEL_DECODE_FAILURE"          # 1-bit 掩膜解码失败

# ---- 源文件稳定性问题 kind ----
ERR_SOURCE_MISSING = "SOURCE_FILE_MISSING"         # 核查期间源文件丢失/不可读
ERR_SOURCE_CHANGED = "SOURCE_FILE_CHANGED"         # 核查期间源文件被替换/损坏


class SegStructureError(Exception):
    """SEG 整体结构不合法（非 SEG/压缩/非多帧/非 BINARY/缺关键序列）。"""


@dataclass
class FrameError:
    frame_number: int  # 1 起，与 DICOM 帧号惯例一致
    kind: str
    message: str


@dataclass
class SourceProblem:
    kind: str
    sop_instance_uid: str
    message: str


@dataclass
class LabelCoverage:
    segment_number: int
    label: str
    annotated_sops: list[str]
    unannotated_sops: list[str]


@dataclass
class SegCheckResult:
    complete: bool
    labels: list[LabelCoverage]
    frame_errors: list[FrameError]
    source_problems: list[SourceProblem]
    stats: dict
    seg_info: dict


# ============================================================ 源文件快照
@dataclass(frozen=True)
class SourceFingerprint:
    """已登记 CT 文件的指纹；size 为 None 表示拍摄时已缺失。"""

    path: str
    size: int | None
    mtime_ns: int | None
    inode: int | None


def snapshot_sources(db: Database, batch_id: str) -> dict[str, SourceFingerprint]:
    """对本批全部已登记 CT 文件拍指纹快照（不抛异常，缺失记入指纹）。"""
    snap: dict[str, SourceFingerprint] = {}
    for sop, path in db.received_files(batch_id):
        try:
            st = os.stat(path)
            snap[sop] = SourceFingerprint(path, st.st_size, st.st_mtime_ns, st.st_ino)
        except OSError:
            snap[sop] = SourceFingerprint(path, None, None, None)
    return snap


def verify_snapshot(snapshot: dict[str, SourceFingerprint]) -> list[SourceProblem]:
    """复核快照：文件丢失或指纹变化（被替换）都不能报完整。"""
    problems: list[SourceProblem] = []
    for sop, fp in sorted(snapshot.items()):
        if fp.size is None:
            problems.append(SourceProblem(
                ERR_SOURCE_MISSING, sop,
                f"核查开始时源文件已丢失：SOP={sop}"))
            continue
        try:
            st = os.stat(fp.path)
        except OSError:
            problems.append(SourceProblem(
                ERR_SOURCE_MISSING, sop,
                f"核查期间源文件丢失：SOP={sop}"))
            continue
        if (st.st_size, st.st_mtime_ns, st.st_ino) != (fp.size, fp.mtime_ns, fp.inode):
            problems.append(SourceProblem(
                ERR_SOURCE_CHANGED, sop,
                f"核查期间源文件被替换：SOP={sop}"))
    return problems


# ============================================================ SEG 结构解析
def load_seg(seg_bytes: bytes) -> Dataset:
    """解析并做整体结构校验；不合法抛 :class:`SegStructureError`。"""
    try:
        ds = pydicom.dcmread(io.BytesIO(seg_bytes))
    except (InvalidDicomError, EOFError, ValueError, AttributeError) as exc:
        raise SegStructureError(f"无法解析为 DICOM 文件：{exc}") from exc

    ts = str(getattr(ds.file_meta, "TransferSyntaxUID", "") or "")
    if ts not in UNCOMPRESSED_TS:
        raise SegStructureError(
            f"SEG 必须为未压缩传输语法（Implicit/Explicit VR LE），实际 "
            f"{ts or '缺失 TransferSyntaxUID'}")
    if str(getattr(ds, "SOPClassUID", "") or "") != SEG_SOP_CLASS:
        raise SegStructureError(
            f"SOPClassUID={getattr(ds, 'SOPClassUID', '缺失')}，"
            "不是 Segmentation Storage，请上传 SEG 文件")
    seg_type = str(getattr(ds, "SegmentationType", "") or "")
    if seg_type != "BINARY":
        raise SegStructureError(
            f"SegmentationType={seg_type or '缺失'}，仅接受 BINARY SEG")
    try:
        n_frames = int(str(getattr(ds, "NumberOfFrames", "")))
    except ValueError:
        raise SegStructureError("NumberOfFrames 缺失或非法，须为多帧 SEG") from None
    if n_frames < 2:
        raise SegStructureError(
            f"NumberOfFrames={n_frames}，仅接受多帧（>=2）BINARY SEG")
    if int(getattr(ds, "BitsAllocated", 0) or 0) != 1:
        raise SegStructureError("BINARY SEG 的 BitsAllocated 必须为 1")
    for attr in ("Rows", "Columns"):
        if getattr(ds, attr, None) is None:
            raise SegStructureError(f"缺少 {attr}")
    if not getattr(ds, "PixelData", None):
        raise SegStructureError("缺少 PixelData")
    return ds


def segment_map(ds: Dataset) -> dict[int, str]:
    """SegmentSequence -> {SegmentNumber: Label}；表本身不合法抛结构错误。"""
    seq = getattr(ds, "SegmentSequence", None)
    if not seq:
        raise SegStructureError("缺少 SegmentSequence，无法识别标签表")
    out: dict[int, str] = {}
    for item in seq:
        num = getattr(item, "SegmentNumber", None)
        if num is None:
            raise SegStructureError("SegmentSequence 存在缺 SegmentNumber 的项")
        num = int(num)
        if num in out:
            raise SegStructureError(
                f"SegmentSequence 中 SegmentNumber={num} 重复，标签表歧义")
        out[num] = str(getattr(item, "SegmentLabel", "") or f"Segment {num}")
    return out


def _frame_segment_numbers(frame_item: Dataset) -> set[int]:
    nums: set[int] = set()
    for item in getattr(frame_item, "SegmentIdentificationSequence", None) or []:
        num = getattr(item, "ReferencedSegmentNumber", None)
        if num is not None:
            nums.add(int(num))
    return nums


def _frame_referenced_sops(frame_item: Dataset,
                           shared_item: Dataset | None) -> set[str]:
    """帧引用的源 SOPInstanceUID 集合（帧级优先，Shared 级兜底）。"""
    deriv_seq = getattr(frame_item, "DerivationImageSequence", None)
    if not deriv_seq and shared_item is not None:
        deriv_seq = getattr(shared_item, "DerivationImageSequence", None)
    uids: set[str] = set()
    for deriv in deriv_seq or []:
        found = False
        # 标准嵌套：DerivationImageSequence 项 -> SourceImageSequence -> 引用
        for src in getattr(deriv, "SourceImageSequence", None) or []:
            uid = getattr(src, "ReferencedSOPInstanceUID", None)
            if uid:
                uids.add(str(uid))
                found = True
        if not found:
            # 兼容把引用直接放在 DerivationImageSequence 项内的实现
            uid = getattr(deriv, "ReferencedSOPInstanceUID", None)
            if uid:
                uids.add(str(uid))
    return uids


# ============================================================ 掩膜解码
def count_mask_bits(pixel_data: bytes, frame_index: int,
                    rows: int, columns: int) -> int:
    """数第 ``frame_index`` 帧（0 起）1-bit 掩膜中置位像素的个数。

    DICOM 1-bit 像素为连续位流、LSB-first 打包，帧间不做字节对齐；
    位数据不足时抛 :class:`IndexError`（像素解码失败）。
    """
    nbits = rows * columns
    start = frame_index * nbits
    end = start + nbits
    if (end + 7) >> 3 > len(pixel_data):
        raise IndexError(
            f"PixelData 仅 {len(pixel_data)} 字节，不足解码第 "
            f"{frame_index + 1} 帧（需要 {(end + 7) >> 3} 字节）")
    if start % 8 == 0 and nbits % 8 == 0:
        chunk = pixel_data[start >> 3:end >> 3]
        return int.from_bytes(chunk, "little").bit_count()
    ones = 0
    for i in range(start, end):
        ones += (pixel_data[i >> 3] >> (i & 7)) & 1
    return ones


# ============================================================ 主核查流程
def verify_seg(db: Database, batch_id: str, seg_bytes: bytes, *,
               snapshot: dict[str, SourceFingerprint] | None = None
               ) -> SegCheckResult:
    """核查上传的 SEG 与已登记批的一致性。

    ``snapshot`` 为 None 时现拍现核；调用方可传入预先拍摄的快照以覆盖
    更长的核查窗口（测试与断点复核用）。
    """
    ds = load_seg(seg_bytes)
    segments = segment_map(ds)
    rows, cols = int(ds.Rows), int(ds.Columns)
    n_frames = int(str(ds.NumberOfFrames))
    per_frame = getattr(ds, "PerFrameFunctionalGroupsSequence", None)
    if per_frame is None or len(per_frame) != n_frames:
        raise SegStructureError(
            f"PerFrameFunctionalGroupsSequence 项数 "
            f"({0 if per_frame is None else len(per_frame)}) 与 "
            f"NumberOfFrames ({n_frames}) 不一致")
    pixel_data = ds.PixelData
    shared_item = None
    shared_seq = getattr(ds, "SharedFunctionalGroupsSequence", None)
    if shared_seq:
        shared_item = shared_seq[0]

    received = db.received_files(batch_id)
    paths = {sop: path for sop, path in received}
    registered = set(paths)
    if snapshot is None:
        snapshot = snapshot_sources(db, batch_id)

    frame_errors: list[FrameError] = []
    source_problems: list[SourceProblem] = []
    annotated: dict[int, set[str]] = {num: set() for num in segments}
    ct_dims_cache: dict[str, tuple[int, int] | None] = {}

    def ct_dims(sop: str) -> tuple[int, int] | None:
        if sop not in ct_dims_cache:
            try:
                ct = pydicom.dcmread(paths[sop], stop_before_pixels=True)
                ct_dims_cache[sop] = (int(ct.Rows), int(ct.Columns))
            except OSError as exc:
                source_problems.append(SourceProblem(
                    ERR_SOURCE_MISSING, sop,
                    f"核查期间源文件丢失或不可读：SOP={sop}（{exc}）"))
                ct_dims_cache[sop] = None
            except Exception as exc:  # 内容损坏/被替换为非 DICOM
                source_problems.append(SourceProblem(
                    ERR_SOURCE_CHANGED, sop,
                    f"源文件内容损坏或被替换：SOP={sop}（{exc}）"))
                ct_dims_cache[sop] = None
        return ct_dims_cache[sop]

    frames_processed = frames_empty = frames_duplicate = 0
    referenced: set[str] = set()
    for idx, frame_item in enumerate(per_frame):
        frame_no = idx + 1  # 帧号从 1 起，与 DICOM 惯例一致

        # ---- 标签：必须恰好命中标签表中的一个 SegmentNumber ----
        nums = _frame_segment_numbers(frame_item)
        if not nums:
            frame_errors.append(FrameError(
                frame_no, ERR_UNKNOWN_SEGMENT,
                "帧缺少 SegmentIdentificationSequence/ReferencedSegmentNumber，"
                "无法确定标签"))
            continue
        if len(nums) > 1:
            frame_errors.append(FrameError(
                frame_no, ERR_AMBIGUOUS_SEGMENT,
                f"帧引用多个标签 {sorted(nums)}，无法确定唯一标签"))
            continue
        seg_num = next(iter(nums))
        if seg_num not in segments:
            frame_errors.append(FrameError(
                frame_no, ERR_UNKNOWN_SEGMENT,
                f"ReferencedSegmentNumber={seg_num} 不在 SegmentSequence "
                f"标签表 {sorted(segments)} 中"))
            continue

        # ---- 源引用：必须恰好引用本批一个已登记 CT ----
        refs = _frame_referenced_sops(frame_item, shared_item)
        if not refs:
            frame_errors.append(FrameError(
                frame_no, ERR_MISSING_REFERENCE,
                "帧缺少 DerivationImageSequence 源 SOPInstanceUID 引用"))
            continue
        if len(refs) > 1:
            frame_errors.append(FrameError(
                frame_no, ERR_AMBIGUOUS_REFERENCE,
                f"帧引用 {len(refs)} 个不同源 SOP，无法确定唯一来源"))
            continue
        sop = next(iter(refs))
        if sop not in registered:
            frame_errors.append(FrameError(
                frame_no, ERR_MISSING_REFERENCE,
                f"引用的源 SOP={sop} 不属于本批已登记 CT"))
            continue

        # ---- 行列须与源 CT 匹配 ----
        dims = ct_dims(sop)
        if dims is None:
            continue  # 源文件问题已记录
        if dims != (rows, cols):
            frame_errors.append(FrameError(
                frame_no, ERR_DIMENSION_MISMATCH,
                f"SEG 帧 {rows}x{cols} 与源 CT {dims[0]}x{dims[1]} "
                f"（SOP={sop}）行列不匹配"))
            continue

        # ---- 解码 1-bit 掩膜 ----
        try:
            ones = count_mask_bits(pixel_data, idx, rows, cols)
        except (IndexError, TypeError, ValueError) as exc:
            frame_errors.append(FrameError(
                frame_no, ERR_PIXEL_DECODE, f"掩膜解码失败：{exc}"))
            continue

        frames_processed += 1
        referenced.add(sop)
        if ones == 0:
            frames_empty += 1
            continue  # 空掩膜不计
        if sop in annotated[seg_num]:
            frames_duplicate += 1
            continue  # 同标签同切片只计一次
        annotated[seg_num].add(sop)

    # ---- 核查结束复核：源文件在核查期间丢失/被替换不能报完整 ----
    source_problems.extend(verify_snapshot(snapshot))
    seen: set[tuple[str, str]] = set()
    unique_problems: list[SourceProblem] = []
    for p in source_problems:
        key = (p.kind, p.sop_instance_uid)
        if key not in seen:
            seen.add(key)
            unique_problems.append(p)

    complete = not frame_errors and not unique_problems
    all_sops = sorted(registered)
    labels: list[LabelCoverage] = []
    if complete:
        # 结论只在完整时给出，避免下游误用不完整清单
        for num in sorted(segments):
            ann = sorted(annotated[num])
            labels.append(LabelCoverage(
                segment_number=num,
                label=segments[num],
                annotated_sops=ann,
                unannotated_sops=[s for s in all_sops if s not in annotated[num]],
            ))

    stats = {
        "frames_total": n_frames,
        "frames_processed": frames_processed,
        "frames_empty_mask": frames_empty,
        "frames_duplicate": frames_duplicate,
        "frames_effective": frames_processed - frames_empty - frames_duplicate,
        "segments": len(segments),
        "referenced_ct": len(referenced),
        "batch_instances": len(registered),
    }
    seg_info = {
        "sop_instance_uid": str(getattr(ds, "SOPInstanceUID", "")),
        "segmentation_type": str(getattr(ds, "SegmentationType", "")),
        "number_of_frames": n_frames,
        "rows": rows,
        "columns": cols,
        "transfer_syntax": str(ds.file_meta.TransferSyntaxUID),
    }
    return SegCheckResult(
        complete=complete,
        labels=labels,
        frame_errors=frame_errors,
        source_problems=unique_problems,
        stats=stats,
        seg_info=seg_info,
    )
