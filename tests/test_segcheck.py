"""SEG 核查单元测试（无网络）：掩膜位解包、帧级校验、源文件稳定性。"""
from __future__ import annotations

import copy
import io
import os

import pytest
from pydicom.uid import ExplicitVRLittleEndian, JPEGBaseline8Bit

from app import segcheck
from app.db import Database
from app.segcheck import SegStructureError
from simulator.generate_data import build_ct_dataset
from simulator.generate_seg import (
    build_seg_dataset,
    circle_bits,
    ellipse_bits,
    pack_bits,
)

CT_CLASS = "1.2.840.10008.5.1.4.1.1.2"
TS = str(ExplicitVRLittleEndian)
STUDY = "1.2.3.9"


@pytest.fixture()
def batch(tmp_path):
    """已完成批：3 个 64x64 CT + 1 个 32x32 CT（行列不匹配场景用）。"""
    db = Database(tmp_path / "t.db")
    ct_dir = tmp_path / "ct"
    ct_dir.mkdir()
    sops = [f"1.2.3.{i}" for i in range(4)]
    db.create_batch("b1", STUDY, "")
    db.transition_state("b1", ["PENDING"], "FINDING")
    db.set_manifest("b1", [(s, CT_CLASS, TS) for s in sops])
    db.transition_state("b1", ["FINDING"], "MOVING")
    for i, sop in enumerate(sops):
        dims = 32 if i == 3 else 64
        ds = build_ct_dataset(
            study_uid=STUDY, series_uid="1.2.3.8", sop_uid=sop,
            patient_id="P1", patient_name="T^T", instance_number=i + 1,
            instance_count=4, rows=dims, cols=dims,
        )
        path = ct_dir / f"{i}.dcm"
        ds.save_as(str(path), enforce_file_format=True)
        assert db.register_instance("b1", sop, str(path), TS)
    db.complete_batch("b1", str(tmp_path / "z.zip"))
    yield db, sops, ct_dir
    db.close()


def _seg_bytes(ds) -> bytes:
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def _ds_for(frames, segments=None, rows=64, cols=64):
    return build_seg_dataset(
        study_uid=STUDY, frames=frames, segments=segments,
        rows=rows, cols=cols, seg_sop_uid="9.9.9.1", seg_series_uid="9.9.9.2",
    )


def _circle(r=5):
    return circle_bits(64, 64, 32, 32, r)


# ---------------------------------------------------------------- 位解包
def test_pack_bits_roundtrip_unaligned_frames():
    """5x5=25 位/帧（非字节对齐）的 3 帧：逐帧计数必须与打包一致。"""
    frames_bits = [
        [1 if i % 3 == 0 else 0 for i in range(25)],
        [0] * 25,
        [1] * 25,
    ]
    packed = pack_bits([b for bits in frames_bits for b in bits])
    assert len(packed) == (75 + 7) // 8
    for idx, bits in enumerate(frames_bits):
        assert segcheck.count_mask_bits(packed, idx, 5, 5) == sum(bits)


def test_count_mask_bits_insufficient_data():
    with pytest.raises(IndexError):
        segcheck.count_mask_bits(b"\x00" * 10, 1, 64, 64)


# ---------------------------------------------------------------- 完整结论
def test_happy_path_empty_mask_and_duplicate_not_counted(batch):
    db, sops, _ = batch
    frames = [
        (1, sops[0], _circle()),
        (1, sops[1], _circle(4)),
        (2, sops[1], ellipse_bits(64, 64, 32, 32, 10, 6)),
        (1, sops[2], None),          # 空掩膜：不计
        (1, sops[0], _circle(3)),    # 同标签同切片：只计一次
    ]
    result = segcheck.verify_seg(db, "b1", _seg_bytes(_ds_for(frames)))
    assert result.complete, result
    by_label = {c.segment_number: c for c in result.labels}
    assert by_label[1].annotated_sops == sorted([sops[0], sops[1]])
    assert by_label[1].unannotated_sops == sorted([sops[2], sops[3]])
    assert by_label[2].annotated_sops == [sops[1]]
    assert by_label[2].unannotated_sops == sorted([sops[0], sops[2], sops[3]])
    assert result.stats["frames_empty_mask"] == 1
    assert result.stats["frames_duplicate"] == 1
    assert result.stats["frames_total"] == 5


def test_shared_derivation_sequence_used_as_fallback(batch):
    """帧级无 DerivationImageSequence 时，Shared 级引用兜底。"""
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence as DicomSequence

    db, sops, _ = batch
    ds = _ds_for([(1, None, _circle()), (1, None, _circle(3))])
    shared = Dataset()
    deriv = Dataset()
    src = Dataset()
    src.ReferencedSOPClassUID = CT_CLASS
    src.ReferencedSOPInstanceUID = sops[0]
    deriv.SourceImageSequence = DicomSequence([src])
    shared.DerivationImageSequence = DicomSequence([deriv])
    ds.SharedFunctionalGroupsSequence = DicomSequence([shared])
    result = segcheck.verify_seg(db, "b1", _seg_bytes(ds))
    assert result.complete, result
    assert result.labels[0].annotated_sops == [sops[0]]  # 第二帧重复去重


def test_multiple_derivation_refs_same_sop_is_single_source(batch):
    """一帧多个 DerivationImageSequence 项但引用同一 SOP：去重后合法。"""
    db, sops, _ = batch
    frames = [(1, [sops[0], sops[0]], _circle()), (2, sops[1], _circle())]
    result = segcheck.verify_seg(db, "b1", _seg_bytes(_ds_for(frames)))
    assert result.complete, result
    assert result.labels[0].annotated_sops == [sops[0]]


# ---------------------------------------------------------------- 帧级拒绝
def test_unknown_segment_reports_frame_number(batch):
    db, sops, _ = batch
    frames = [(1, sops[0], _circle()), (99, sops[1], _circle())]
    result = segcheck.verify_seg(db, "b1", _seg_bytes(_ds_for(frames)))
    assert not result.complete
    assert result.labels == []  # 拒绝完整结论
    assert [(e.frame_number, e.kind) for e in result.frame_errors] == [
        (2, segcheck.ERR_UNKNOWN_SEGMENT)
    ]


def test_missing_reference_variants(batch):
    db, sops, _ = batch
    frames = [
        (1, None, _circle()),               # 帧1：无 DerivationImageSequence
        (1, "1.2.3.4.5.6.7.8.9", _circle()),  # 帧2：清单外 SOP
        (2, sops[0], _circle()),            # 帧3：正常
    ]
    result = segcheck.verify_seg(db, "b1", _seg_bytes(_ds_for(frames)))
    assert not result.complete
    assert [(e.frame_number, e.kind) for e in result.frame_errors] == [
        (1, segcheck.ERR_MISSING_REFERENCE),
        (2, segcheck.ERR_MISSING_REFERENCE),
    ]


def test_ambiguous_reference_reports_frame(batch):
    db, sops, _ = batch
    frames = [(1, [sops[0], sops[1]], _circle()), (2, sops[2], _circle())]
    result = segcheck.verify_seg(db, "b1", _seg_bytes(_ds_for(frames)))
    assert not result.complete
    assert [(e.frame_number, e.kind) for e in result.frame_errors] == [
        (1, segcheck.ERR_AMBIGUOUS_REFERENCE)
    ]


def test_dimension_mismatch_reports_frame(batch):
    db, sops, _ = batch  # sops[3] 是 32x32 CT
    frames = [(1, sops[3], _circle()), (1, sops[0], _circle())]
    result = segcheck.verify_seg(db, "b1", _seg_bytes(_ds_for(frames)))
    assert not result.complete
    assert [(e.frame_number, e.kind) for e in result.frame_errors] == [
        (1, segcheck.ERR_DIMENSION_MISMATCH)
    ]
    assert "32x32" in result.frame_errors[0].message


def test_pixel_decode_failure_reports_frame(batch):
    db, sops, _ = batch
    ds = _ds_for([(1, sops[0], _circle()), (1, sops[1], _circle())])
    raw = ds.PixelData
    ds.PixelData = raw[: len(raw) // 2]  # 截断：第二帧位数据缺失
    result = segcheck.verify_seg(db, "b1", _seg_bytes(ds))
    assert not result.complete
    assert [(e.frame_number, e.kind) for e in result.frame_errors] == [
        (2, segcheck.ERR_PIXEL_DECODE)
    ]


def test_multiple_errors_all_reported(batch):
    """错误聚合：一次返回全部问题帧，而不是遇错即停。"""
    db, sops, _ = batch
    frames = [
        (99, sops[0], _circle()),                 # 帧1 未知标签
        (1, [sops[0], sops[1]], _circle()),       # 帧2 歧义引用
        (1, sops[3], _circle()),                  # 帧3 行列不匹配
    ]
    result = segcheck.verify_seg(db, "b1", _seg_bytes(_ds_for(frames)))
    assert not result.complete
    assert [e.frame_number for e in result.frame_errors] == [1, 2, 3]


# ---------------------------------------------------------------- 结构拒绝
def test_structure_rejects_non_seg_compressed_single_frame(batch):
    db, sops, _ = batch
    # 非 DICOM
    with pytest.raises(SegStructureError):
        segcheck.verify_seg(db, "b1", b"this is not a dicom file")
    # 非 SEG（CT 图像）
    ct = build_ct_dataset(study_uid=STUDY, series_uid="1.2.3.8",
                          sop_uid=sops[0], patient_id="P1", patient_name="T^T",
                          instance_number=1, instance_count=1)
    with pytest.raises(SegStructureError, match="Segmentation"):
        segcheck.verify_seg(db, "b1", _seg_bytes(ct))
    # 压缩传输语法（声明 JPEG；去掉 PixelData 以绕过 pydicom 写出时的
    # 封装校验——核查端对传输语法的拒绝先于像素检查，不影响用例目的）
    ds = _ds_for([(1, sops[0], _circle()), (1, sops[1], _circle())])
    ds.file_meta.TransferSyntaxUID = JPEGBaseline8Bit
    del ds.PixelData
    with pytest.raises(SegStructureError, match="未压缩"):
        segcheck.verify_seg(db, "b1", _seg_bytes(ds))
    # FRACTIONAL
    ds2 = _ds_for([(1, sops[0], _circle()), (1, sops[1], _circle())])
    ds2.SegmentationType = "FRACTIONAL"
    with pytest.raises(SegStructureError, match="BINARY"):
        segcheck.verify_seg(db, "b1", _seg_bytes(ds2))
    # 单帧
    ds3 = _ds_for([(1, sops[0], _circle())])
    with pytest.raises(SegStructureError, match="多帧"):
        segcheck.verify_seg(db, "b1", _seg_bytes(ds3))


def test_structure_rejects_frame_count_mismatch(batch):
    db, sops, _ = batch
    ds = _ds_for([(1, sops[0], _circle()), (1, sops[1], _circle())])
    ds.NumberOfFrames = 5  # 与 PerFrameFunctionalGroupsSequence 项数不符
    with pytest.raises(SegStructureError, match="NumberOfFrames"):
        segcheck.verify_seg(db, "b1", _seg_bytes(ds))


# ---------------------------------------------------------------- 源文件稳定性
def test_source_file_deleted_during_check(batch):
    db, sops, ct_dir = batch
    snapshot = segcheck.snapshot_sources(db, "b1")
    os.remove(ct_dir / "0.dcm")  # 快照后、核查前删除
    frames = [(1, sops[1], _circle()), (2, sops[2], _circle())]
    result = segcheck.verify_seg(db, "b1", _seg_bytes(_ds_for(frames)),
                                 snapshot=snapshot)
    assert not result.complete
    assert result.labels == []
    assert any(p.kind == segcheck.ERR_SOURCE_MISSING
               and p.sop_instance_uid == sops[0]
               for p in result.source_problems)


def test_source_file_replaced_during_check(batch, tmp_path):
    db, sops, ct_dir = batch
    snapshot = segcheck.snapshot_sources(db, "b1")
    # 用另一个合法 CT 覆盖（inode 变化 = 被替换）
    other = build_ct_dataset(study_uid=STUDY, series_uid="1.2.3.8",
                             sop_uid=sops[0], patient_id="P1",
                             patient_name="T^T", instance_number=1,
                             instance_count=4)
    replacement = tmp_path / "replacement.dcm"
    other.save_as(str(replacement), enforce_file_format=True)
    os.replace(replacement, ct_dir / "0.dcm")
    frames = [(1, sops[1], _circle()), (2, sops[2], _circle())]
    result = segcheck.verify_seg(db, "b1", _seg_bytes(_ds_for(frames)),
                                 snapshot=snapshot)
    assert not result.complete
    assert any(p.kind == segcheck.ERR_SOURCE_CHANGED
               and p.sop_instance_uid == sops[0]
               for p in result.source_problems)


def test_source_file_missing_at_read_time(batch):
    """帧引用的 CT 在核查读取时已丢失：同样不能报完整。"""
    db, sops, ct_dir = batch
    os.remove(ct_dir / "0.dcm")
    frames = [(1, sops[0], _circle()), (2, sops[1], _circle())]
    result = segcheck.verify_seg(db, "b1", _seg_bytes(_ds_for(frames)))
    assert not result.complete
    assert any(p.kind == segcheck.ERR_SOURCE_MISSING
               and p.sop_instance_uid == sops[0]
               for p in result.source_problems)


def test_snapshot_self_check_clean(batch):
    db, sops, _ = batch
    snapshot = segcheck.snapshot_sources(db, "b1")
    assert segcheck.verify_snapshot(snapshot) == []
    assert len(snapshot) == 4
