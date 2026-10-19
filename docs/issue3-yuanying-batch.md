# 元婴目录一次性 12 秒批次

这是把一个目录里的影片做完就停，不是一直开着的队列，也不会在目录里出现新文件时自动加入。

## 窗口和模型

每扇窗口 12 秒，每隔 10 秒再开一扇。画面取第 0、3、6、9 秒，视觉预算 70，不写入时间戳。音轨使用整段 16 kHz 单声道。窗口版本是 `12s-10s-4f70-tail-v1`，档案名是 `jellyfin-12s-4f`。

模型地址只能是 `http://127.0.0.1:18784`。`processing_id` 只能是 `eg2-7c43611ad2e20661c3d3`。不要使用正式实例 8096、10 秒模型 18783，或共用的 18780。同一时刻只允许 1 个模型请求。不要启动原来的 6 秒 worker。

语言策略固定为 `auto-v1`：每一小段自己判断语言，不在认不出来时改成中文。不认识、不支持或同一小段里有多种语言时停下。1 毫秒的时间对齐规则不放宽。失败的转写不能当成一份成功的空文本。状态是未知、正在进行或无法确认完成的推理，不会自动再发一次。

## 怎样才算整部片完成

一部片的覆盖记为 full，必须同时满足：准备、转写、对齐、分段、音画和文字都成功，而且窗口从 0 盖到这部片的真实结尾。每扇窗口都是完整的 12 秒，不能截短。摘录或只做完一部分，仍记为未完成。

默认导入要求清单里的每一部都是 full。少一部就完全不写索引。加上 `--allow-partial` 时，只把已经 full 的写入新索引，命令以非 0 结束，整批覆盖记为 partial，不能把整个目录说成已经完成。

## 已有 120 秒样本

三部样本原来的文件、模型、缓存和失败记录都不改。

每部样本有 12 扇窗口。整片按 10 秒格子重算时，其中 11 扇的绝对时间能对上，另外 1 扇是样本自己的尾窗，不在整片格子上。三部合计是 33 扇时间重合、3 扇尾窗不重合。

时间重合也不复制结果。输入哈希把候选对象算进去，样本里的候选编号和 `full_film: false` 都跟整片不同。准备阶段只把这个结论写进新目录的审计文件，不复制样本响应。`preflight`、`run`、`resume` 和 `import` 都不会调用复制函数，所以 `sampleReuse.reused` 在这条路径上是 0。哈希不同记为 `input-hash-not-equivalent`。不能改写样本原来的哈希。

如果整片长度刚好等于样本结尾，尾窗的时间可能重合，哈希仍然不同，仍然不复用。

## 冻结的源事实

准备阶段只读一份已经核对过的清单，不调用宿主接口，也不读取浏览器令牌。清单里每条有 `label`、`path`、`source` 和 `probe`。`source.presence` 按文件中的原值保存，不改写。它只表示当时能不能读到文件，不参加来源身份比较。身份仍是文件、媒体和版本字段；这些字段、模型签名和输入哈希都按原值逐项比较。旧样本协议没有 `presence` 时，不能因此记成来源不一致。`contentVersion` 使用清单里的原值，准备程序不重算。

`label` 使用清单里的 `F001` 这种编号，不按文件名重新编号。只处理源目录顶层的 mp4，其它文件和子目录跳过。

## 解释器

验收和编排命令使用：

```text
/Volumes/Turbo/Library/embedding-service/.venv/bin/python
```

不要用系统自带的 python3。真正开始转写时，父进程使用配置里的 `workerPython`，因为那个环境才有日语分词依赖。权重目录由配置里的 `asrModel` 和 `alignerModel` 指向，不要写进 Git。

## 配置

配置放在 Git 外面。下面的 `$BATCH_CONFIG` 只是占位，不要把填好的文件提交进来。

```json
{
  "sourceDirectory": "$MEDIA_DIR",
  "sourceFacts": "$FROZEN_SOURCES",
  "outputRoot": "$BATCH_OUTPUT",
  "workerPython": "$WORKER_PYTHON",
  "asrModel": "$ASR_MODEL",
  "alignerModel": "$ALIGNER_MODEL",
  "embeddingBaseUrl": "http://127.0.0.1:18784",
  "processingId": "eg2-7c43611ad2e20661c3d3",
  "maxInflightEmbeddings": 1,
  "transcriptionLanguagePolicy": "auto-v1",
  "expectedFilmCount": 46,
  "template": "$EMPTY_TEMPLATE",
  "references": "$REFERENCE_CACHE",
  "importOutput": "$NEW_INDEX_DIR"
}
```

`$FROZEN_SOURCES` 是只读的冻结清单。`$EMPTY_TEMPLATE` 是空索引模板，文件名必须以 `.template.db` 结尾。`$NEW_INDEX_DIR` 必须是一个还不存在的新目录。输出目录不能放进样本目录、`asr-` 目录或正在使用的索引目录。

## 命令

在仓库工作副本里执行。先准备，确认就绪后再跑；中断后用 resume 从已成功的检查点继续。导入在处理完成之后单独做，并且只写新索引。

```bash
PY=/Volumes/Turbo/Library/embedding-service/.venv/bin/python
$PY plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py preflight --config "$BATCH_CONFIG"
$PY plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py run --config "$BATCH_CONFIG"
$PY plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py status --config "$BATCH_CONFIG"
$PY plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py resume --config "$BATCH_CONFIG"
$PY plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py import --config "$BATCH_CONFIG"
```

屏幕上的进度只显示 `F001` 这类编号、覆盖状态和数量，不显示路径、原文、向量或令牌。带路径的清单留在输出目录的私有文件里。

`run` 和 `resume` 的退出码：0 表示全部 full；1 表示准备未就绪；2 表示还有未完成的片；3 表示停在未知或被打断的推理上，后面的片不会继续发请求。每一阶段的日志写在该片已有的 `logs` 目录里，后一阶段继续使用这个目录。

导入产物是新目录里的 `prepared-index.db`。它保持 `manual_pause=1`、`imported_candidates_only=1` 和 `http://127.0.0.1:18784`。完成的片在索引里是 `complete`。不要打开正在运行的 `index.db`，也不要把 10 秒签名写进去。

## 验收

用上面的绝对解释器，在本工作副本中逐条运行：

```bash
$PY plugins/EmbeddingVideoSearch/scripts/full-film-transcribe-test.py
$PY plugins/EmbeddingVideoSearch/scripts/full-film-embed-test.py
$PY plugins/EmbeddingVideoSearch/scripts/ten-second-contract-test.py
$PY plugins/EmbeddingVideoSearch/scripts/sample-c-contract-test.py
$PY plugins/EmbeddingVideoSearch/scripts/sample-c-import-test.py
$PY plugins/EmbeddingVideoSearch/scripts/yuanying-batch-test.py
$PY plugins/EmbeddingVideoSearch/scripts/rebind-full-source-test.py
git diff --check
```

`rebind-full-source-test.py` 检查只读预览。预览用 `immutable=1` 读取已经完成检查点、但文件头仍是 WAL 的数据库，不能改文件字节，也不能生成 `-wal` 或 `-shm`。普通 `mode=ro` 在不同 SQLite 上结果不一样，不能拿它当作唯一标准。非空 WAL 和历史原索引仍然拒绝。

## 不在这次

连续结果是另一项工作，相似结果也是另一项工作。这次只对应 issue 3。
