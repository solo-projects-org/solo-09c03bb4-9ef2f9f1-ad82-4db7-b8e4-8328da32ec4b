"""FastAPI 入口：批管理 REST API + Swagger 文档 + DICOM 服务生命周期。"""
from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import dicom, segcheck, storage
from .config import settings
from .db import (
    STATE_COMPLETED,
    Database,
    set_db,
)
from .pipeline import Pipeline, recover_on_startup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("api")

# 进程级单例（lifespan 中初始化）
db: Database | None = None
pipeline: Pipeline | None = None
_scp_server = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, pipeline, _scp_server
    settings.ensure_dirs()
    db = Database(settings.DB_PATH)
    set_db(db)
    recover_on_startup(db)
    _scp_server = dicom.start_store_scp(db)
    pipeline = Pipeline(db)
    log.info("API 就绪，数据卷=%s", settings.DATA_DIR)
    try:
        yield
    finally:
        if _scp_server is not None:
            _scp_server.shutdown()
        if db is not None:
            db.close()


app = FastAPI(
    title="DICOM CT 离线取数流水线",
    version="1.1.0",
    lifespan=lifespan,
    description=(
        "提交 StudyInstanceUID → C-FIND 固定 SOP 清单 → C-MOVE 回传 → "
        "C-STORE 接收非压缩 CT → 核对收齐后打包 ZIP 下载。\n\n"
        "规则：清单外实例不入包、重复实例不重复计数、保存失败即批失败、"
        "失败批迟到回传不可恢复、重启后未完成批判失败。\n\n"
        "SEG 核查：对 COMPLETED 批上传未压缩多帧 BINARY SEG，按每帧 "
        "SegmentNumber / DerivationImageSequence 源 SOP / 解码掩膜，输出各标签"
        "已标注与未标注切片清单；未知标签、缺失或歧义引用、行列不匹配、"
        "像素解码失败、核查期间源文件丢失或被替换，均拒绝给出完整结论。"
    ),
)


# ------------------------------------------------------------------ schemas
class BatchCreate(BaseModel):
    study_instance_uid: str = Field(
        ..., description="源端检查的 StudyInstanceUID",
        examples=["1.2.840.10008.1.2.99.1"],
    )
    fault_inject: str = Field(
        default="",
        description="可选故障注入：save_failure=保存失败演示；留空为正常流程",
    )


class BatchView(BaseModel):
    id: str
    study_instance_uid: str
    state: str
    expected_count: int
    received_count: int
    failure_reason: str | None
    zip_path: str | None
    fault_inject: str
    created_at: str
    updated_at: str
    downloadable: bool


def _view(row: dict) -> BatchView:
    return BatchView(
        id=row["id"],
        study_instance_uid=row["study_instance_uid"],
        state=row["state"],
        expected_count=row["expected_count"],
        received_count=row["received_count"],
        failure_reason=row["failure_reason"],
        zip_path=row["zip_path"],
        fault_inject=row["fault_inject"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        downloadable=row["state"] == STATE_COMPLETED and bool(row["zip_path"]),
    )


# ------------------------------------------------------------------ routes
@app.post("/batches", response_model=BatchView, status_code=201,
          tags=["batches"], summary="提交检查，启动取数批")
def create_batch(body: BatchCreate) -> BatchView:
    if body.fault_inject not in ("", storage.FAULT_SAVE):
        raise HTTPException(400, "fault_inject 只支持 '' 或 'save_failure'")
    uid = body.study_instance_uid.strip()
    if not uid:
        raise HTTPException(400, "study_instance_uid 不能为空")

    if db.has_active_batch_for_study(uid):
        raise HTTPException(409, "该 Study 已有进行中的批，请等待其结束")

    batch_id = uuid.uuid4().hex
    db.create_batch(batch_id, uid, body.fault_inject)
    pipeline.submit(batch_id)
    return _view(db.get_batch(batch_id))


@app.get("/batches", response_model=list[BatchView],
         tags=["batches"], summary="列出全部批")
def list_batches() -> list[BatchView]:
    return [_view(r) for r in db.list_batches()]


@app.get("/batches/{batch_id}", response_model=BatchView,
         tags=["batches"], summary="查询批状态与进度")
def get_batch(batch_id: str) -> BatchView:
    row = db.get_batch(batch_id)
    if row is None:
        raise HTTPException(404, "批不存在")
    return _view(row)


@app.get("/batches/{batch_id}/events",
         tags=["batches"], summary="查询批事件流水（接收/重复/拒收/状态迁移）")
def get_events(batch_id: str) -> list[dict]:
    if db.get_batch(batch_id) is None:
        raise HTTPException(404, "批不存在")
    return db.list_events(batch_id)


@app.get("/batches/{batch_id}/instances",
         tags=["batches"], summary="查询批清单及每个 SOP 的接收情况")
def get_instances(batch_id: str) -> dict:
    row = db.get_batch(batch_id)
    if row is None:
        raise HTTPException(404, "批不存在")
    files = db.received_files(batch_id)
    received = {sop: path for sop, path in files}
    return {
        "batch_id": batch_id,
        "state": row["state"],
        "manifest": sorted(db.manifest_uids(batch_id)),
        "received": sorted(received),
        "missing": db.missing_uids(batch_id),
    }


@app.get("/batches/{batch_id}/download",
         tags=["batches"], summary="下载 ZIP（仅 COMPLETED 批可下载）")
def download_zip(batch_id: str) -> FileResponse:
    row = db.get_batch(batch_id)
    if row is None:
        raise HTTPException(404, "批不存在")
    if row["state"] != STATE_COMPLETED or not row["zip_path"]:
        raise HTTPException(
            409,
            f"批状态 {row['state']} 不可下载；仅 C-MOVE 成功且清单收齐的批可下载",
        )
    path = Path(row["zip_path"])
    if not path.exists():
        raise HTTPException(410, "ZIP 文件缺失（数据卷可能已损坏）")
    return FileResponse(
        path, media_type="application/zip",
        filename=f"{batch_id}.zip",
    )


# ------------------------------------------------------------------ SEG 核查
class FrameErrorView(BaseModel):
    frame_number: int = Field(..., description="帧号（1 起）")
    kind: str = Field(..., description="错误类别，如 UNKNOWN_SEGMENT")
    message: str


class SourceProblemView(BaseModel):
    kind: str = Field(..., description="SOURCE_FILE_MISSING / SOURCE_FILE_CHANGED")
    sop_instance_uid: str
    message: str


class LabelCoverageView(BaseModel):
    segment_number: int
    label: str
    annotated_count: int
    unannotated_count: int
    annotated_sops: list[str] = Field(..., description="该标签已标注的本批 SOP 清单")
    unannotated_sops: list[str] = Field(..., description="该标签未标注的本批 SOP 清单")


class SegInfoView(BaseModel):
    sop_instance_uid: str
    segmentation_type: str
    number_of_frames: int
    rows: int
    columns: int
    transfer_syntax: str


class SegCheckView(BaseModel):
    batch_id: str
    complete: bool = Field(..., description="结论是否完整；false 时 labels 为空")
    seg: SegInfoView
    stats: dict[str, int]
    labels: list[LabelCoverageView]
    frame_errors: list[FrameErrorView]
    source_problems: list[SourceProblemView]


def _seg_check_view(batch_id: str, result: segcheck.SegCheckResult) -> SegCheckView:
    return SegCheckView(
        batch_id=batch_id,
        complete=result.complete,
        seg=SegInfoView(**result.seg_info),
        stats=result.stats,
        labels=[
            LabelCoverageView(
                segment_number=c.segment_number,
                label=c.label,
                annotated_count=len(c.annotated_sops),
                unannotated_count=len(c.unannotated_sops),
                annotated_sops=c.annotated_sops,
                unannotated_sops=c.unannotated_sops,
            )
            for c in result.labels
        ],
        frame_errors=[FrameErrorView(**vars(e)) for e in result.frame_errors],
        source_problems=[SourceProblemView(**vars(p)) for p in result.source_problems],
    )


@app.post(
    "/batches/{batch_id}/seg-check",
    response_model=SegCheckView,
    tags=["seg-check"],
    summary="上传未压缩多帧 BINARY SEG，核查各标签覆盖的本批 CT 切片",
    responses={
        200: {"description": "核查通过，labels 给出各标签已标注/未标注 SOP 完整清单"},
        400: {"description": "SEG 结构不合法（非 SEG / 压缩 / 非多帧 / 非 BINARY / 缺关键序列）"},
        404: {"description": "批不存在"},
        409: {"description": "批非 COMPLETED，或核查期间源文件丢失/被替换"},
        413: {"description": "SEG 文件超过大小上限"},
        422: {"description": "帧级错误（未知标签 / 缺失或歧义引用 / 行列不匹配 / 像素解码失败），拒绝完整结论"},
    },
)
async def seg_check(
    batch_id: str,
    file: UploadFile = File(..., description="未压缩、多帧、BINARY 的 SEG 文件"),
):
    row = db.get_batch(batch_id)
    if row is None:
        raise HTTPException(404, "批不存在")
    if row["state"] != STATE_COMPLETED:
        raise HTTPException(
            409, f"批状态 {row['state']} 不可核查；仅 COMPLETED 批可核查")
    data = await file.read()
    if not data:
        raise HTTPException(400, "上传文件为空")
    if len(data) > settings.MAX_SEG_BYTES:
        raise HTTPException(
            413, f"SEG 文件超过 {settings.MAX_SEG_BYTES} 字节上限")
    try:
        result = await run_in_threadpool(segcheck.verify_seg, db, batch_id, data)
    except segcheck.SegStructureError as exc:
        db.add_event("SEG_CHECK_REJECTED", f"SEG 结构不合法：{exc}", batch_id)
        raise HTTPException(400, f"SEG 结构不合法：{exc}") from exc

    view = _seg_check_view(batch_id, result)
    if result.complete:
        db.add_event(
            "SEG_CHECK_COMPLETE",
            f"SEG 核查通过：{result.stats['frames_total']} 帧、"
            f"{result.stats['segments']} 个标签，覆盖结论完整",
            batch_id,
        )
        return view
    if result.source_problems:
        db.add_event(
            "SEG_CHECK_SOURCE_UNSTABLE",
            f"核查期间源文件异常 {len(result.source_problems)} 项，不能报完整",
            batch_id,
        )
        return JSONResponse(view.model_dump(), status_code=409)
    db.add_event(
        "SEG_CHECK_REJECTED",
        f"SEG 帧级错误 {len(result.frame_errors)} 处，拒绝完整结论",
        batch_id,
    )
    return JSONResponse(view.model_dump(), status_code=422)


@app.get("/health", tags=["system"], summary="健康检查")
def health() -> dict:
    return {"status": "ok", "store_port": settings.STORE_PORT,
            "ae_title": settings.APP_AET}


# ------------------------------------------------------------------ debug
@app.get("/internal/pacs-studies", tags=["internal"],
         summary="（演示用）STUDY 级 C-FIND 列出模拟 PACS 中的检查")
def pacs_studies() -> list[dict]:
    from pydicom.dataset import Dataset
    from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
    from pynetdicom import AE
    from pynetdicom.presentation import build_context
    from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelFind

    ae = AE(ae_title=settings.APP_AET)
    ae.requested_contexts = [build_context(
        str(StudyRootQueryRetrieveInformationModelFind),
        [str(ExplicitVRLittleEndian), str(ImplicitVRLittleEndian)])]
    assoc = ae.associate(settings.PACS_HOST, settings.PACS_PORT,
                         ae_title=settings.PACS_AET)
    if not assoc.is_established:
        raise HTTPException(502, "无法连接模拟 PACS")
    try:
        q = Dataset()
        q.QueryRetrieveLevel = "STUDY"
        q.StudyInstanceUID = ""
        q.PatientID = ""
        q.PatientName = ""
        q.Modality = ""
        q.NumberOfStudyRelatedInstances = ""
        out = []
        for status, ident in assoc.send_c_find(
            q, str(StudyRootQueryRetrieveInformationModelFind)
        ):
            if ident is None:
                continue
            out.append({
                "study_instance_uid": str(ident.StudyInstanceUID),
                "patient_id": str(getattr(ident, "PatientID", "")),
                "patient_name": str(getattr(ident, "PatientName", "")),
                "modality": str(getattr(ident, "Modality", "")),
                "instances": int(getattr(ident, "NumberOfStudyRelatedInstances", 0) or 0),
            })
        return out
    finally:
        if assoc.is_alive:
            assoc.release()


@app.post("/internal/suicide", tags=["internal"],
          summary="（演示用）退出进程以演示重启恢复；compose 自动重启")
def suicide(tasks: BackgroundTasks) -> dict:
    def _die() -> None:
        os._exit(1)
    tasks.add_task(_die)
    return {"detail": "进程即将退出，等待 Docker 重启"}
