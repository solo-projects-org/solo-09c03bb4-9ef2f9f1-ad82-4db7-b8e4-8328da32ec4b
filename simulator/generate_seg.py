"""虚构多帧 BINARY SEG 生成器（无任何真实患者信息）。

为虚构 CT 检查生成一份"外部标注组风格"的 Segmentation Storage 文件：

* 未压缩 Explicit VR Little Endian、多帧、SegmentationType=BINARY、1-bit 掩膜
* 两个虚构标签（1=NODULE、2=ORGAN），只覆盖部分切片
* 故意包含一个全零掩膜帧与一个同标签同切片重复帧，
  用于演示核查 API 的"空掩膜不计、重复只计一次"规则

文件布局：``<data_dir>/seg/<study_uid>.dcm``（与 CT 检查目录平级，
PACS 索引只认 CT，会跳过该目录）。

可独立运行，为已有数据卷补生成缺失的 SEG（幂等）::

    python -m simulator.generate_seg --data-dir /data/pacs
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence as DicomSequence
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    SegmentationStorage,
    generate_uid,
)

from .generate_data import UID_PREFIX, _make_uid

SEG_SOP_CLASS = str(SegmentationStorage)
CT_SOP_CLASS = str(CTImageStorage)

# 虚构标签表：segment_number -> 标签名
DEFAULT_SEGMENTS = {1: "NODULE", 2: "ORGAN"}


# ---------------------------------------------------------------- 位掩膜
def pack_bits(bits: list[int]) -> bytes:
    """LSB-first 打包 1-bit 像素（DICOM PS3.5 8.1.1）。"""
    out = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            out[i >> 3] |= 1 << (i & 7)
    return bytes(out)


def circle_bits(rows: int, cols: int, cx: float, cy: float, radius: float) -> list[int]:
    bits = [0] * (rows * cols)
    r2 = radius * radius
    for y in range(rows):
        for x in range(cols):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r2:
                bits[y * cols + x] = 1
    return bits


def ellipse_bits(rows: int, cols: int, cx: float, cy: float,
                 rx: float, ry: float) -> list[int]:
    bits = [0] * (rows * cols)
    for y in range(rows):
        for x in range(cols):
            if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0:
                bits[y * cols + x] = 1
    return bits


# ---------------------------------------------------------------- SEG 构造
def _segment_item(num: int, label: str) -> Dataset:
    item = Dataset()
    item.SegmentNumber = num
    item.SegmentLabel = label
    item.SegmentAlgorithmType = "MANUAL"
    item.SegmentAlgorithmName = "FICTIONAL-ANNOTATOR"
    return item


def _source_item(sop_uid: str) -> Dataset:
    src = Dataset()
    src.ReferencedSOPClassUID = CT_SOP_CLASS
    src.ReferencedSOPInstanceUID = sop_uid
    return src


def _frame_item(seg_num: int, refs) -> Dataset:
    item = Dataset()
    seg_id = Dataset()
    seg_id.ReferencedSegmentNumber = seg_num
    item.SegmentIdentificationSequence = DicomSequence([seg_id])
    if refs is not None:
        if isinstance(refs, str):
            refs = [refs]
        deriv = Dataset()
        deriv.SourceImageSequence = DicomSequence(
            [_source_item(uid) for uid in refs])
        item.DerivationImageSequence = DicomSequence([deriv])
    return item


def build_seg_dataset(*, study_uid: str, frames: list,
                      segments: dict[int, str] | None = None,
                      rows: int = 64, cols: int = 64,
                      patient_id: str = "P000", patient_name: str = "PHANTOM^X",
                      seg_sop_uid: str | None = None,
                      seg_series_uid: str | None = None) -> Dataset:
    """构造内存中的多帧 BINARY SEG Dataset。

    ``frames`` 每项为 ``(segment_number, referenced_sops, bits)``：

    * ``referenced_sops``：str（单个源 SOP）、str 列表（多源，用于构造
      歧义引用样例）或 None（不生成 DerivationImageSequence，缺失引用样例）
    * ``bits``：长度 ``rows*cols`` 的 0/1 列表；None 表示全零掩膜帧
    """
    segments = dict(segments or DEFAULT_SEGMENTS)
    seg_sop_uid = seg_sop_uid or generate_uid()
    seg_series_uid = seg_series_uid or generate_uid()

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SEG_SOP_CLASS
    file_meta.MediaStorageSOPInstanceUID = seg_sop_uid
    file_meta.TransferSyntaxUID = str(ExplicitVRLittleEndian)
    file_meta.ImplementationClassUID = _make_uid(9900)
    file_meta.ImplementationVersionName = "SIMPACS_1"

    ds = Dataset()
    ds.file_meta = file_meta
    ds.preamble = b"\0" * 128
    ds.SOPClassUID = SEG_SOP_CLASS
    ds.SOPInstanceUID = seg_sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = seg_series_uid
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.PatientBirthDate = "19700101"
    ds.PatientSex = "O"
    ds.Modality = "SEG"
    ds.StudyDate = "20261102"
    ds.StudyTime = "100000"
    ds.ContentDate = "20261102"
    ds.ContentTime = "100000"
    ds.AccessionNumber = f"SEG{patient_id}"
    ds.StudyID = f"S{patient_id}"
    ds.StudyDescription = "Fictional external annotations (synthetic)"
    ds.SeriesNumber = 99
    ds.SeriesDescription = "Fictional SEG labels"
    ds.InstanceNumber = 1
    ds.Manufacturer = "Synthetic PACS Demo"
    ds.ContentCreatorName = "ANNOTATOR^FICTIONAL"
    ds.ContentLabel = "DEMO_SEG"
    ds.ContentDescription = "Fully synthetic labels, no real patient data"

    ds.SegmentationType = "BINARY"
    ds.Rows = rows
    ds.Columns = cols
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 1
    ds.BitsStored = 1
    ds.HighBit = 0
    ds.PixelRepresentation = 0
    ds.NumberOfFrames = len(frames)
    ds.SegmentSequence = DicomSequence(
        [_segment_item(num, label) for num, label in sorted(segments.items())])
    ds.PerFrameFunctionalGroupsSequence = DicomSequence(
        [_frame_item(seg_num, refs) for seg_num, refs, _ in frames])

    all_bits: list[int] = []
    for _, _, bits in frames:
        all_bits.extend(bits if bits is not None else [0] * (rows * cols))
    ds.add_new(0x7FE00010, "OB", pack_bits(all_bits))  # PixelData，1-bit 显式 OB
    return ds


def plan_demo_frames(sop_uids: list[str], rows: int = 64, cols: int = 64):
    """确定性演示帧计划：部分切片两标签 + 一个空掩膜帧 + 一个重复帧。"""
    frames: list[tuple[int, str, list[int] | None]] = []
    n = len(sop_uids)
    if n == 0:
        return frames
    # NODULE：前 40% 切片，逐片微移的圆形灶
    for i in range(max(1, (n * 2) // 5)):
        frames.append((1, sop_uids[i],
                       circle_bits(rows, cols, cols / 2 + i, rows / 2 - i,
                                   5 + i % 3)))
    # ORGAN：中段约 30% 切片，居中椭圆
    start = n // 4
    for i in range(start, min(n, start + max(1, (n * 3) // 10))):
        frames.append((2, sop_uids[i],
                       ellipse_bits(rows, cols, cols / 2, rows / 2,
                                    cols / 4, rows / 6)))
    # 全零掩膜帧：核查时必须"不计"
    frames.append((1, sop_uids[min(n - 1, n // 2)], None))
    # 同标签同切片重复帧：核查时只计一次
    frames.append((1, sop_uids[0], circle_bits(rows, cols, cols / 2, rows / 2, 4)))
    return frames


def generate_seg_for_study(data_dir, study_uid: str, sop_uids: list[str],
                           patient_id: str, patient_name: str,
                           rows: int = 64, cols: int = 64) -> Path:
    """为一个虚构 CT 检查生成配套 SEG 文件，返回路径（幂等）。"""
    data_dir = Path(data_dir)
    seg_dir = data_dir / "seg"
    seg_dir.mkdir(parents=True, exist_ok=True)
    path = seg_dir / f"{study_uid}.dcm"
    suffix = study_uid[len(UID_PREFIX):] if study_uid.startswith(UID_PREFIX) else "0"
    ds = build_seg_dataset(
        study_uid=study_uid,
        frames=plan_demo_frames(list(sop_uids), rows, cols),
        rows=rows, cols=cols,
        patient_id=patient_id,
        patient_name=patient_name,
        seg_sop_uid=f"{UID_PREFIX}7{suffix}",
        seg_series_uid=f"{UID_PREFIX}8{suffix}",
    )
    ds.save_as(str(path), enforce_file_format=True)
    return path


def _scan_studies(data_dir: Path):
    """扫描数据卷中的 CT 检查目录，返回 (study_uid, patient, [sop_uids])。"""
    for study_dir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        if study_dir.name == "seg":
            continue
        files = sorted(study_dir.glob("*.dcm"))
        if not files:
            continue
        probe = pydicom.dcmread(str(files[0]), stop_before_pixels=True)
        if str(getattr(probe, "Modality", "")) != "CT":
            continue
        sop_uids = [
            str(pydicom.dcmread(str(f), stop_before_pixels=True).SOPInstanceUID)
            for f in files
        ]
        yield (str(probe.StudyInstanceUID),
               str(getattr(probe, "PatientID", "P000")),
               str(getattr(probe, "PatientName", "PHANTOM^X")),
               sop_uids)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="为已有虚构 CT 数据卷补生成配套 SEG（幂等）")
    parser.add_argument("--data-dir", required=True, help="PACS 数据卷目录")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 SEG")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    for study_uid, patient_id, patient_name, sop_uids in _scan_studies(data_dir):
        out = data_dir / "seg" / f"{study_uid}.dcm"
        if out.exists() and not args.force:
            print(f"已存在，跳过：{out}")
            continue
        path = generate_seg_for_study(
            data_dir, study_uid, sop_uids, patient_id, patient_name)
        print(f"生成虚构 SEG：{path}（{len(sop_uids)} 个源切片）")


if __name__ == "__main__":
    main()
