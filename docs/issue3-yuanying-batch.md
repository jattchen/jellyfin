# 元婴目录一次性 12 秒批次

这是把一个目录里的影片做完就停，不是一直开着的队列，也不会在目录里出现新文件时自动加入。

## 窗口和模型

每扇窗口 12 秒，每隔 10 秒再开一扇。画面取第 0、3、6、9 秒，视觉预算 70，不写入时间戳。音轨使用整段 16 kHz 单声道。窗口版本是 `12s-10s-4f70-tail-v1`，档案名是 `jellyfin-12s-4f`。

模型地址只能是 `http://127.0.0.1:18784`。`processing_id` 只能是 `eg2-7c43611ad2e20661c3d3`。不要使用正式实例 8096、10 秒模型 18783，或共用的 18780。同一时刻只允许 1 个模型请求。不要启动原来的 6 秒 worker。

语言策略固定为 `auto-v1`：每一小段自己判断语言，不在认不出来时改成中文。不认识、不支持或同一小段里有多种语言时停下。1 毫秒的时间对齐规则不放宽。失败的转写不能当成一份成功的空文本。状态是未知、正在进行或无法确认完成的推理，不会自动再发一次。

默认仍是上面这条严格规则。只有恢复配置显式写了 `unsupportedAlignLanguagePolicy`，而且值正好是 `unsupported-align-language-skip-v1` 时，才多一条窄规则：转写模型认识、对齐模型不支持、原因正好是 `language-unsupported` 的一小段，可以不进文字索引。这一小段保留原文、原文哈希、检测到的语言、原因和策略版本，状态是 `skipped`，`alignment_executed` 为 false。它不是对齐成功，也不是空文本，也不改成中文，也不编造时间。文字候选里没有它。缺语言、同一小段多种语言、不认识的语言、越界、超时、未知和其它错误仍然停下整批。已经失败的旧检查点不会被自动改成跳过。

只有恢复配置另外显式写了 `alignmentTimePolicy`，而且值正好是 `last-unit-tail-80ms-v1` 时，才多一条时间规则。它只处理一种情况：对齐结果里只有最后一个字超出音频结尾，并且超出不超过 80 毫秒。这个字的结束时间收到音频结尾。原始对齐文件的字节不改。收过之后，仍用原来的 1 毫秒规则检查。这不是语言缺口，也不记成跳过。超过 80 毫秒、不是最后一个字、未知、超时和其它错误仍然停下整批，不会自动再跑。

## 怎样才算整部片完成

一部片的覆盖记为 full，必须同时满足：准备、转写、对齐、分段、音画和文字都成功，而且窗口从 0 盖到这部片的真实结尾。每扇窗口都是完整的 12 秒，不能截短。摘录或只做完一部分，仍记为未完成。

音画和文字分开记。`audioVisualCoverage` 为 full，表示窗口盖住整条时间线，而且每一扇音画向量都成功。`textCoverage` 为 full，表示没有语言缺口。出现上面那种允许的跳过时，`textCoverage` 为 gapped，整片 `coverage` 不是 full。缺口记录在该片的 `asr/align-language-gaps.json`：片号、块号、起止时间、原因、检测到的语言、策略版本，以及原文还在。进度里的公开缺口不含原文。有缺口就不能把文字说成完整，也不能导入。

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

不要用系统自带的 python3。真正开始转写时，父进程使用配置里的 `workerPython`，因为那个环境才有日语分词依赖。

`asrModel` 和 `alignerModel` 必须指向模型的版本目录。这个目录本身要能读到 `config.json`（一份非空的 JSON 对象）、`vocab.json`、`merges.txt`，以及非空权重。目录里如果有权重索引，索引点到的每个分片都要在，并且能读。只填外面那一层、版本号还在子目录里的路径，准备阶段会拒绝。程序不猜子目录，不下载，也不加载权重。这两项不要写进 Git。

`--check-model` 只向 `http://127.0.0.1:18784` 核对向量模型签名，不检查转写权重。

已经失败的输出、写错的协议和检查点都留在原地。下一次使用一个新的输出目录。准备阶段如果发现旧协议里的模型路径或 `worker_python` 和当前配置不是同一个路径，会拒绝，并且不改旧文件。路径比较可以先变成绝对路径，但文件里原来写的字符串保持不动。

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
  "unsupportedAlignLanguagePolicy": "unsupported-align-language-skip-v1",
  "alignmentTimePolicy": "last-unit-tail-80ms-v1",
  "recoverySource": "$OLD_OUTPUT",
  "recoverySnapshot": "$V2_SNAPSHOT",
  "expectedFilmCount": 46,
  "template": "$EMPTY_TEMPLATE",
  "references": "$REFERENCE_CACHE",
  "importOutput": "$NEW_INDEX_DIR"
}
```

`$FROZEN_SOURCES` 是只读的冻结清单。`$EMPTY_TEMPLATE` 是空索引模板，文件名必须以 `.template.db` 结尾。`$NEW_INDEX_DIR` 必须是一个还不存在的新目录。输出目录不能放进样本目录、`asr-` 目录或正在使用的索引目录。

`unsupportedAlignLanguagePolicy` 可以不写。不写时保持严格停下。恢复时才写上面的版本值。`alignmentTimePolicy` 也可以不写。不写时，末字越界仍按失败停下。写了之后，只有新的、还没做完的转写协议会带上这条规则。已经做完的协议不会因为只改顶层配置而更新，所以要换一个新的输出目录。

`recoverySource` 是旧输出目录，只读。`recoverySnapshot` 是这份旧输出的文件清单，每一项有路径、大小和 sha256。恢复前先核对清单和磁盘一致。新的 `outputRoot` 必须是另一个还不存在的目录。它可以和旧目录不在同一块盘上。

## 命令

在仓库工作副本里执行。先准备，确认就绪后再跑；中断后用 resume 从已成功的检查点继续。导入在处理完成之后单独做，并且只写新索引。

```bash
PY=/Volumes/Turbo/Library/embedding-service/.venv/bin/python
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py preflight --config "$BATCH_CONFIG"
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py run --config "$BATCH_CONFIG"
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py status --config "$BATCH_CONFIG"
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py resume --config "$BATCH_CONFIG"
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py import --config "$BATCH_CONFIG"
```

要从旧的失败输出换到一个新目录时，用同一份恢复配置。它包含上面的批次字段，再加旧目录、快照和新的输出目录。准备只复制和离线复核，不推理。

语言缺口那一次，先处理有缺口的那一部，再跑其余的：

```bash
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-recovery.py prepare --config "$RECOVERY_CONFIG"
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py run --config "$RECOVERY_CONFIG" --film F006
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py run --config "$RECOVERY_CONFIG"
```

末字越界这一次，配置里要有 `alignmentTimePolicy`。先接受那一部已知的失败，再跑其余还没做完的：

```bash
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-recovery.py prepare --config "$RECOVERY_CONFIG"
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py run --config "$RECOVERY_CONFIG" --film F011
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py run --config "$RECOVERY_CONFIG"
```

`prepare` 先按快照核对旧目录的每一个文件。核对通过后，把要保留的产物逐个复制到新目录。复制是按块读出再写入新文件，源和目标可以在两块盘上。新文件不是硬链接，后来写入新目录不会改到旧文件。

旧清单里的绝对路径按下面四类处理：

- 改绑切块音频：`chunks.json` 里的 `wav_path` 不沿用旧位置，也不打开那个旧路径。只有快照里的副本 sha256 等于这条切块的 `wav_sha256` 时，才改成新目录里的路径。缺文件或哈希不一致，就拒绝，而且这时还没有创建输出目录。
- 改绑嵌入协议：每部片的 `asr_run`，以及 `shared_document_cache` 里的 `directory`。缓存对象的其它字段保留。
- 保持原样：媒体路径、转写模型、对齐模型、`worker_python`、`prepare.json` 里的相对路径 `full_wav`，以及原始转写和对齐 JSON。已经做完的片子，转写协议字节保持原样，包括已经记成语言缺口的那一部。
- 不复制：`preflight.json`、`progress.json`、旧的 `recovery-receipt.json`、`exit-receipt.json`、`run.log`、`run-identity.json`。旧目录里的这些文件不改。

没有时间规则时，只把那一处 `language-unsupported` 失败在新目录里记成跳过，并记下它来自哪一份旧检查点。已经是跳过的语言缺口整份复制，不再派生一次。

写了 `alignmentTimePolicy` 时，还要正好有一处原因是 `validation:last-unit-out-of-bounds` 的失败。准备阶段在新目录里离线复核这一处，不重新推理。原始对齐文件字节不变，旧目录里的失败记录也不变。新检查点的原因是 `derived-tail-boundary-validated`。这一部里还没对齐的块，留给后面的 `run` 继续对齐。还没做完的片子会带上这条时间规则；以后模型返回同样窄的末字越界时，对齐阶段就收口。超出 80 毫秒、不是最后一个字、还有别的失败、未知状态或孤立输出，都会拒绝整批准备，并且不改旧目录。

再跑一次 `prepare` 时，新目录的字节如果和回执一致，就不再重写，也不再复核。`--film` 只进入点名的那一部。它可以重复。写了不存在的编号会拒绝。它不放宽失败即停。

屏幕上的进度只显示 `F001` 这类编号、覆盖状态和数量，不显示路径、原文、向量或令牌。带路径的清单留在输出目录的私有文件里。

`run` 和 `resume` 的退出码：0 表示每一部都是 full。1 表示准备未就绪，例如模型目录不完整。2 表示普通阶段失败、还有未完成的片，或已完成的片里仍有文字缺口。3 表示停在未知、超时或被打断的推理上。普通失败、未知和超时都会停住整批，不会继续下一部，也不会把未完成的结果写成 full。已经按规则跳过语言、并且音画已经完整的片，不会挡住下一部；这种批次退出 2，进度里有缺口，但没有 `stopped`。只跑 `--film` 时，没被点名的片保持原状态，整批因此也不是 0。`--film F011` 时如果这一部没有硬失败，退出码也是 2，进度里没有 `stopped`。其余片都跑完以后，只要还留着文字缺口，退出码仍是 2，覆盖是 gapped，没有 `stopped`。退出码 2 并且进度里有 `stopped`，才是真的失败把整批停住。有文字缺口的片不导入。每一阶段的日志写在该片已有的 `logs` 目录里，后一阶段继续使用这个目录。

导入产物是新目录里的 `prepared-index.db`。它保持 `manual_pause=1`、`imported_candidates_only=1` 和 `http://127.0.0.1:18784`。完成的片在索引里是 `complete`。不要打开正在运行的 `index.db`，也不要把 10 秒签名写进去。

## 验收

用上面的绝对解释器，在本工作副本中逐条运行：

```bash
$PY -B plugins/EmbeddingVideoSearch/scripts/full-film-transcribe-test.py
$PY -B plugins/EmbeddingVideoSearch/scripts/full-film-embed-test.py
$PY -B plugins/EmbeddingVideoSearch/scripts/ten-second-contract-test.py
$PY -B plugins/EmbeddingVideoSearch/scripts/sample-c-contract-test.py
$PY -B plugins/EmbeddingVideoSearch/scripts/sample-c-import-test.py
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-batch-test.py
$PY -B plugins/EmbeddingVideoSearch/scripts/rebind-full-source-test.py
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-recovery-test.py
git diff --check
```

`rebind-full-source-test.py` 检查只读预览。预览用 `immutable=1` 读取已经完成检查点、但文件头仍是 WAL 的数据库，不能改文件字节，也不能生成 `-wal` 或 `-shm`。普通 `mode=ro` 在不同 SQLite 上结果不一样，不能拿它当作唯一标准。非空 WAL 和历史原索引仍然拒绝。

## 不在这次

连续结果是另一项工作，相似结果也是另一项工作。这次只对应 issue 3。
