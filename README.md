# DICOM CT 离线取数流水线 + SEG 覆盖核查

面向影像科研数据员的院内 PACS 取数工具：提交 `StudyInstanceUID`，经
**C-FIND 固定本批 SOP 清单 → C-MOVE 发起回传 → C-STORE 接收落盘 →
核对收齐 → 打包 ZIP 下载** 的完整流水线，支持多检查并行、慢回传交错、
重复投送去重、清单外实例拒收、保存失败判失败与重启恢复。

取回的检查在交给训练程序前，还可上传外部标注组返回的
**未压缩多帧 BINARY SEG** 做覆盖核查：按每帧的 SegmentNumber、
DerivationImageSequence 源 SOPInstanceUID 与解码掩膜，输出各标签
**已标注 / 未标注** 的本批切片清单；任何无法确定的情况都拒绝给出完整结论。

* **API 框架**：FastAPI（自带 Swagger/OpenAPI）
* **DICOM 栈**：pynetdicom 3.x（C-STORE SCP + C-FIND/C-MOVE SCU）、pydicom
* **持久化**：SQLite（WAL，全量落在数据卷）
* **模拟 PACS**：pynetdicom 实现的真实 DICOM 网络对端，内置虚构合成 CT，
  可配置慢回传与重复投送；另带 rogue 投送器演示拒收
* **虚构样例**：每个虚构检查配套一份多帧 BINARY SEG（含空掩膜帧与重复帧），
  无任何真实患者信息

---

## 1. 快速开始（Docker Compose）

```bash
# 构建并启动 API + 模拟 PACS（首次自动生成虚构 CT 与配套虚构 SEG）
docker compose up --build -d

# Swagger 文档
open http://localhost:8000/docs

# 一键端到端演示：交错回传 / 重复投送 / 清单外拒收 / 压缩拒收 /
# 保存失败 / 失败批迟到拒收 / SEG 覆盖核查 / 重启恢复
docker compose run --rm demo
```

端口：

| 服务 | 端口 | 用途 |
|---|---|---|
| api | 8000 | REST API + Swagger |
| api | 11113 | DICOM C-STORE SCP（AE=`RETRIEVER`）|
| pacs | 11112 | 模拟 PACS（AE=`MYPACS`）|

数据卷：`api-data`（SQLite、DICOM 文件、ZIP）、`pacs-data`（虚构 CT 与
`seg/` 下的虚构 SEG 样例）。

---

## 2. 业务规则与实现位置

| 规则 | 实现 |
|---|---|
| 只收**非压缩 CT** | SCP 只公布 Implicit/Explicit VR Little Endian 的 CT 存储上下文，压缩 TS 在协商阶段即被拒；`app/storage.py::validate_dataset` 二次校验 |
| 源端批内**不增删实例** | C-FIND（IMAGE 级）一次性固定清单写入 `instances` 表，`set_manifest` 只允许固定一次 |
| 回传**归入对应批** | C-STORE 按 `StudyInstanceUID` 经 `db.find_batch_for_study` 路由（按 rowid 取最新批） |
| **清单外实例不入包** | `db.is_manifest_uid` 校验，拒收并回 `0xC000`，事件 `STORE_REJECTED` |
| **重复实例不重复计数** | `(batch_id, sop_instance_uid)` 唯一约束 + `register_instance` 条件更新；重复直接回 SUCCESS，不写盘不计数，事件 `STORE_DUPLICATE` |
| **文件写完且登记成功才回 C-STORE 成功** | `handle_c_store` 严格顺序：临时文件 → fsync → rename → 目录 fsync → DB 登记；任一步失败回 `0xA700/0xC000`，登记失败会删除已写文件 |
| **C-MOVE 成功且清单收齐才可下载** | 下载接口仅允许 `COMPLETED` 且 zip_path 存在；MOVING/FAILED 返回 409 |
| 传输/保存失败、结束后缺件均**判失败** | C-MOVE 非 0x0000（含 0xA702/0xB000）失败；`missing_uids` 非空失败；写盘异常立即 `fail_batch` |
| **迟到回传不能恢复失败批** | 终态 FAILED 的批收到 C-STORE 一律拒收；DB 层 `transition_state` 禁止任何状态迁出终态 |
| 多检查**并行、慢回传互不阻塞** | 每批独立线程；C-STORE SCP 每关联一线程；C-MOVE 回传与其它批的 API/DICOM 处理无共享阻塞 |
| **重启恢复** | 启动时所有非终态批置 FAILED；COMPLETED 批保留可下载，ZIP 丢失自动按登记重建 |
| **SEG 仅未压缩多帧 BINARY** | `app/segcheck.py::load_seg`：压缩 TS / 非 SEG / 单帧 / FRACTIONAL / 缺关键序列一律 400 |
| **帧只能引用本批一个已登记 CT** | `verify_seg` 逐帧解析 DerivationImageSequence：无引用或清单外引用记 `MISSING_REFERENCE`，多个不同源记 `AMBIGUOUS_REFERENCE`，均指出帧号并 422 |
| **行列须匹配** | SEG 帧 Rows/Columns 与被引用 CT 不一致记 `DIMENSION_MISMATCH`（指出帧号） |
| **同标签同切片只计一次、空掩膜不计** | 按 `(SegmentNumber, SOP)` 聚合去重；全零掩膜帧统计但不计入标注 |
| **未知标签 / 解码失败拒绝完整结论** | `UNKNOWN_SEGMENT` / `AMBIGUOUS_SEGMENT` / `PIXEL_DECODE_FAILURE` 均指出帧号，HTTP 422 且 `labels` 为空 |
| **核查期间源文件丢失或被替换不能报完整** | 核查前对全部已登记文件拍指纹快照（size/mtime/inode），核查后复核；异常记 `SOURCE_FILE_MISSING/CHANGED`，HTTP 409 |
| **仅 COMPLETED 批可核查** | 其他状态上传 SEG 返回 409；核查结果写入批事件流水（`SEG_CHECK_*`） |

### 批状态机

```
PENDING ──C-FIND──▶ FINDING ──清单固定+故障武装──▶ MOVING
                                          │
                  收齐+C-MOVE 成功 ────────┴──▶ COMPLETED（可下载、可 SEG 核查，终态）
                  任何失败/缺件 ──────────────▶ FAILED（终态，迟到回传拒收）
重启时 PENDING/FINDING/MOVING ───────────────▶ FAILED
```

---

## 3. REST API

Swagger：`GET /docs`（OpenAPI JSON：`/openapi.json`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/batches` | 提交检查，body：`{"study_instance_uid": "...", "fault_inject": ""}`，`fault_inject` 可选 `save_failure` |
| GET | `/batches` | 列出全部批 |
| GET | `/batches/{id}` | 批状态与进度（`expected_count/received_count/state/downloadable`）|
| GET | `/batches/{id}/events` | 事件流水（接收、重复、拒收、状态迁移、故障、SEG 核查）|
| GET | `/batches/{id}/instances` | 清单、已收、缺失 SOP 列表 |
| GET | `/batches/{id}/download` | 下载 ZIP（仅 COMPLETED），ZIP 内含 `dicom/` 与 `MANIFEST.csv` |
| POST | `/batches/{id}/seg-check` | 上传未压缩多帧 BINARY SEG（multipart 字段 `file`），核查各标签覆盖 |
| GET | `/health` | 健康检查 |
| GET | `/internal/pacs-studies` | （演示）C-FIND 列出模拟 PACS 检查 |
| POST | `/internal/suicide` | （演示）退出进程，由 compose 自动重启以演示恢复 |

### SEG 核查接口

```
POST /batches/{batch_id}/seg-check
Content-Type: multipart/form-data; 字段 file=<SEG 文件>
```

* **200 核查通过**：`complete=true`，`labels[]` 给出每个标签
  （`segment_number`/`label`）的 `annotated_sops` 与 `unannotated_sops`
  完整清单；`stats` 含帧总数、空掩膜帧数、重复帧数等。
* **422 帧级错误**：`complete=false`、`labels=[]`，`frame_errors[]` 逐项给出
  `frame_number`（1 起）、`kind`（`UNKNOWN_SEGMENT` / `AMBIGUOUS_SEGMENT` /
  `MISSING_REFERENCE` / `AMBIGUOUS_REFERENCE` / `DIMENSION_MISMATCH` /
  `PIXEL_DECODE_FAILURE`）与说明——拒绝完整结论。
* **409**：批非 COMPLETED，或核查期间源文件丢失/被替换
  （`source_problems[]` 给出 SOP 与原因）。
* **400**：上传内容不是未压缩多帧 BINARY SEG（非 DICOM、非 SEG、压缩 TS、
  单帧、FRACTIONAL、缺 SegmentSequence 等）。
* **404 / 413**：批不存在 / 文件超过 `MAX_SEG_BYTES`（默认 64 MiB）。

手工调用示例：

```bash
BASE=http://localhost:8000
STUDY=$(curl -s $BASE/internal/pacs-studies | jq -r '.[0].study_instance_uid')

BID=$(curl -s -X POST $BASE/batches -H 'Content-Type: application/json' \
  -d "{\"study_instance_uid\":\"$STUDY\"}" | jq -r .id)

watch "curl -s $BASE/batches/$BID | jq"
curl -s $BASE/batches/$BID/events | jq
curl -OJ $BASE/batches/$BID/download

# SEG 覆盖核查（虚构样例在 pacs 数据卷 seg/ 下，与检查同 StudyInstanceUID 命名）
curl -s -X POST $BASE/batches/$BID/seg-check \
  -F "file=@/path/to/seg.dcm;type=application/dicom" | jq

# 保存失败演示
curl -s -X POST $BASE/batches -H 'Content-Type: application/json' \
  -d "{\"study_instance_uid\":\"$STUDY\",\"fault_inject\":\"save_failure\"}"
```

---

## 4. 演示场景说明

模拟 PACS 支持环境变量（见 `docker-compose.yml`）：

* `MOVE_DELAY=1.0`：每个实例发送前停顿 1 秒，制造慢回传，便于观察多批交错
* `MOVE_DUPLICATE_EVERY=3`：每 3 个实例额外用一条新关联重发一次，模拟源端重试
  （接收端应回 SUCCESS 但不计数）

rogue 投送器（演示容器/API 镜像内均可运行）：

```bash
# 清单外实例：期望 0xC000 拒收
python -m simulator.rogue_sender off-list   --study <StudyUID> --host api --port 11113
# 声明 JPEG 压缩：期望关联协商被拒
python -m simulator.rogue_sender compressed --study <StudyUID> --host api --port 11113
```

保存失败演示的实现是**真实故障**而非 mock：提交时把批目录 `chmod 555`，
非 root 容器用户写入必然 `PermissionError`，可观察到：
首个实例 C-STORE 回 `0xA700` → 批立即 FAILED → 后续实例全部以“已失败”拒收
→ C-MOVE 最终 `0xA702` → 下载返回 409。

虚构 SEG 样例由 PACS 初始化时一并生成（`pacs-data` 卷 `seg/<StudyUID>.dcm`）：
两个虚构标签（1=NODULE、2=ORGAN）覆盖部分切片，并故意包含一个全零掩膜帧
与一个同标签同切片重复帧，用于演示“空掩膜不计、重复只计一次”。为已有
数据卷补生成：`python -m simulator.generate_seg --data-dir /data/pacs`。

---

## 5. 目录结构

```
app/
  config.py      环境变量配置（AE、端口、数据卷路径、超时、SEG 上传上限）
  db.py          SQLite schema、状态机、清单/登记去重、重启恢复查询
  storage.py     非压缩 CT 校验、原子写盘+fsync、故障注入、ZIP 打包
  dicom.py       C-STORE SCP 处理器、C-FIND/C-MOVE SCU
  segcheck.py    SEG 结构校验、1-bit 掩膜解码、逐帧核查、源文件快照复核
  pipeline.py    每批后台线程编排与启动恢复
  main.py        FastAPI 路由、lifespan、Swagger
simulator/
  generate_data.py  虚构合成 CT 生成器（确定性模体像素，无真实患者信息）
  generate_seg.py   虚构多帧 BINARY SEG 生成器（配套样例 + 补生成工具）
  pacs.py           模拟 PACS（C-ECHO/C-FIND/C-MOVE，慢回传+重复投送）
  rogue_sender.py   清单外/压缩实例投送器
demo/
  run_demo.py       端到端演示编排（含 SEG 核查演示）
tests/
  test_db.py       状态机/去重/终态保护单元测试
  test_storage.py  校验/原子写/ZIP 单元测试
  test_segcheck.py SEG 核查单元测试（位解包/帧级错误/源文件稳定性）
  test_e2e.py      真实 pynetdicom 网络栈端到端测试（含 SEG 核查场景）
docker-compose.yml  Dockerfile  Dockerfile.pacs  entrypoint.sh
```

## 6. 本地测试（无需 Docker）

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

端到端测试会在临时目录启动完整的 PACS + API 真实进程对，覆盖：
正常收齐打包、并行交错、重复去重、清单外/压缩/未知 Study 拒收、
保存失败与迟到拒收、409 冲突、重启恢复，以及 SEG 核查的完整结论、
帧级拒绝（未知标签/缺失引用/歧义引用/行列不匹配）与状态守卫
（共 12 个端到端测试 + 28 个单元测试）。

## 7. 接入真实 PACS 的注意事项

1. 在真实 PACS 上注册 AE Title `RETRIEVER` 及本服务主机/11113 端口作为
   C-MOVE 目标，并放通防火墙。
2. 用 `PACS_AET/PACS_HOST/PACS_PORT` 指向真实源端；确认其 C-FIND 支持
   Study Root IMAGE 级查询并返回 `TransferSyntaxUID`。
3. 流水线只接受未压缩 little-endian CT；若源端只存压缩 TS，需在源端配置
   转码，或扩展 `app/dicom.py` 的上下文与 `storage.UNCOMPRESSED_TS` 策略。
4. 数据卷需保证持久化挂载与足够 inode/容量；SQLite 与 DICOM 文件均在
   `DATA_DIR` 下，备份该目录即可完整备份批状态与文件。
5. SEG 核查按“帧引用本批已登记 CT”对账：外部标注组若基于其他来源
   （如重新导出的副本）生成 SEG，需保证 DerivationImageSequence 中的
   SOPInstanceUID 与 PACS 源一致，否则将按缺失引用拒绝。

