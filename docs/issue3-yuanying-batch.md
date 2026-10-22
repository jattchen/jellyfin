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

音画和文字分开记。`audioVisualCoverage` 为 full，表示窗口盖住整条时间线，而且每一扇音画向量都成功。`textCoverage` 为 full，表示没有语言缺口。出现上面那种允许的跳过时，`textCoverage` 为 gapped，整片 `coverage` 不是 full。缺口记录在该片的 `asr/align-language-gaps.json`：片号、块号、起止时间、原因、检测到的语言、策略版本，以及原文还在。进度里的公开缺口不含原文。有缺口时，文字覆盖保持 gapped。跳过的那一段不是空文本，也不是对齐成功。

默认导入要求清单里的每一部都是 full。有文字缺口时整批拒绝，不写索引。少一部也完全不写索引。加上 `--allow-partial` 时，只把已经 full 的写入新索引，命令以非 0 结束。证据写明清单片数、实际导入片数和被跳过的片号。被跳过的片子不计入已导入片数，整批覆盖记为 partial。

只有导入配置显式写了 `languageGapImportPolicy`，而且值正好是 `approved-language-gap-import-v1` 时，已经按 `unsupported-align-language-skip-v1` 跳过的语言缺口才可以和全部音画窗口、其余有效文字一起进入新索引。证据里的 `textGaps` 照实写下片号、块号、起止时间、原因、检测到的语言、跳过策略、`alignment_executed: false` 和原文哈希。原文本身不进证据，也不进索引。整批 `coverage` 记为 gapped，`fullFilm` 为 false，命令以退出码 2 结束。这条策略不改索引里的表，媒体行仍是 `complete`，表示音画窗口可以搜索。运行中的检索覆盖标记看不到这个文字缺口，要以导入证据为准。未知状态、没有批准的语言、缺文件、坏向量、签名不符，或同一批里还有第二种实际错误，都整批拒绝，不留下半个索引。这条策略打开时，`--allow-partial` 不能再丢掉失败的片子。

## 已有 120 秒样本

三部样本原来的文件、模型、缓存和失败记录都不改。

每部样本有 12 扇窗口。整片按 10 秒格子重算时，其中 11 扇的绝对时间能对上，另外 1 扇是样本自己的尾窗，不在整片格子上。三部合计是 33 扇时间重合、3 扇尾窗不重合。

时间重合也不复制结果。输入哈希把候选对象算进去，样本里的候选编号和 `full_film: false` 都跟整片不同。准备阶段只把这个结论写进新目录的审计文件，不复制样本响应。`preflight`、`run`、`resume` 和 `import` 都不会调用复制函数，所以 `sampleReuse.reused` 在这条路径上是 0。哈希不同记为 `input-hash-not-equivalent`。不能改写样本原来的哈希。

如果整片长度刚好等于样本结尾，尾窗的时间可能重合，哈希仍然不同，仍然不复用。

## 冻结的源事实

准备阶段只读一份已经核对过的清单，不调用宿主接口，也不读取浏览器令牌。清单里每条有 `label`、`path`、`source` 和 `probe`。`source.presence` 按文件中的原值保存，不改写。它只表示当时能不能读到文件，不参加来源身份比较。身份仍是文件、媒体和版本字段；这些字段、模型签名和输入哈希都按原值逐项比较。旧样本协议没有 `presence` 时，不能因此记成来源不一致。`contentVersion` 使用清单里的原值，准备程序不重算。

`label` 使用清单里的 `F001` 这种编号，不按文件名重新编号。只处理源目录顶层的 mp4，其它文件和子目录跳过。

默认不写 `sourceSelectionPolicy`。源目录顶层多出来的 mp4 不在冻结清单里时，准备不会就绪。`missingFiles` 表示目录里有这份 mp4，清单里没有对应路径。清单上的片子不在目录里，计入未匹配。大小或修改时间与清单不同，计入漂移。

只有显式写成 `frozen-facts-only-v1` 时，这些清单外的 mp4 才被忽略，并单独计入 `ignoredExtraFiles`。它们不进入 manifest，不分配新编号，也不产生窗口。原来清单上的任何一部缺失、大小或时间变化、编号或路径重复、身份字段对不上、窗口规划失败或探针异常，仍然拒绝。空字符串与不写这个字段相同，保持上面的拒绝。其它字符串在准备阶段拒绝，原因是 `config`。这个字段只在 `preflight` 和 `run` 读取配置时生效。它不写入每部片的协议，也不改写已经存在的 manifest 和成功缓存。忽略新增文件表示这一轮仍只准备冻结清单里的片子，不是整批已经完成。

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
  "sourceSelectionPolicy": "frozen-facts-only-v1",
  "languageGapImportPolicy": "approved-language-gap-import-v1",
  "recoverySource": "$OLD_OUTPUT",
  "recoverySnapshot": "$V2_SNAPSHOT",
  "storageInterruptionRecovery": {
    "version": "storage-interruption-v1",
    "film": "F011",
    "candidate": "$ONE_JOINT_ID",
    "inputHash": "$INPUT_HASH",
    "stateSha256": "$STATE_SHA256",
    "snapshotSha256": "$SNAPSHOT_SHA256"
  },
  "expectedFilmCount": 46,
  "template": "$EMPTY_TEMPLATE",
  "references": "$REFERENCE_CACHE",
  "importOutput": "$NEW_INDEX_DIR"
}
```

`$FROZEN_SOURCES` 是只读的冻结清单。`$EMPTY_TEMPLATE` 是空索引模板，文件名必须以 `.template.db` 结尾。`$NEW_INDEX_DIR` 必须是一个还不存在的新目录。输出目录不能放进样本目录、`asr-` 目录或正在使用的索引目录。

`unsupportedAlignLanguagePolicy` 可以不写。不写时保持严格停下。恢复时才写上面的版本值。`alignmentTimePolicy` 也可以不写。不写时，末字越界仍按失败停下。写了之后，只有新的、还没做完的转写协议会带上这条规则。已经做完的协议不会因为只改顶层配置而更新，所以要换一个新的输出目录。

`sourceSelectionPolicy` 也可以不写。不写或写成空字符串时，目录里多出来的 mp4 会让准备不就绪。只有新的配置副本需要忽略这些文件时，才写成 `frozen-facts-only-v1`。已经准备好的输出不用因此重做协议。旧的失败配置保持原样。

`languageGapImportPolicy` 也可以不写。不写或写成空字符串时，文字缺口不能导入。要把已批准的语言缺口连同全部音画和有效文字写入新索引时，才在这份导入配置里写成 `approved-language-gap-import-v1`。它不改已经完成的协议和缓存。未知字符串会在导入前拒绝。

`recoverySource` 是旧输出目录，只读。`recoverySnapshot` 是这份旧输出的文件清单，每一项有路径、大小和 sha256。恢复前先核对清单和磁盘一致。新的 `outputRoot` 必须是另一个还不存在的目录。它可以和旧目录不在同一块盘上。

`storageInterruptionRecovery` 只在存储中断这一次填写。语言缺口和末字越界那两次不要写它。上面示例里的哈希都是占位，要换成快照和那一条状态文件的真实哈希。

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

存储中断是另一条显式恢复，不能拿“正好有一处对齐失败”来套。配置里要有完整的 `storageInterruptionRecovery`：模式版本、片号、那一条音画候选、它的输入哈希、原来的状态文件哈希，以及这份快照文件自己的哈希。少任何一项，或者只写了候选编号，都不会放行。没写这个对象时，未知状态仍然整批拒绝。

这条模式只接受对齐已经结束的目录。它不制造一处对齐失败，也不重新跑末字复核。已经写在转写协议里的 `recovery_origin` 按原字节复制，继续指向更早的那棵树。成功的音画在创建新目录之前逐条核对：切片哈希、输入哈希、响应哈希，以及模型、768 维、归一化和处理参数。文字和共享文档缓存里已经成功的记录也同样核对。对不上就拒绝，这时还没有新目录。

未知请求只能有一条，而且必须是白名单上的那条：状态是 `inflight`，没有响应文件，切片和输入哈希一致。第二条未知、失败、`terminal-response`、孤立响应、半截切片或签名不对，都整批拒绝。明确没派发、也没有成功记录的 `.partial.mp4` 可以不复制进新目录，并写进回执；拿不准的残片就停下，不扩大重试。

新目录里不放这一条 `inflight` 状态文件。原来的状态字节和哈希留在旧目录，并记在回执里。成功记录按字节复制。之后的普通嵌入入口会重新发送这一条，其余还没开始的窗口照常处理。如果新的一次又变成未知，原来的停止规则仍然生效，不会循环重试。准备阶段的模型请求数是 0。回执写明快照、白名单、复制和改绑、省略项、这条未知的原状态，以及复用了多少条成功音画。它不是整批成功退出，也不是转写准确性验收。

```bash
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-recovery.py prepare --config "$RECOVERY_CONFIG"
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py run --config "$RECOVERY_CONFIG" --film F011
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py run --config "$RECOVERY_CONFIG"
```

画面比音轨早结束，是另一条显式规则，不能靠放宽 150 毫秒来过。配置里的 `videoTailPolicy` 必须正好是 `video-tail-hold-last-frame-200ms-v1`。没写，或写成空字符串，切片方式和以前一样。写成别的值，准备阶段就拒绝。

这条规则只看最后一扇窗口。冻结探针要能指出唯一的真实画面，封面和附图不算。音轨仍是配置里记下的绝对序号。一条流的终点是它在容器时间轴上的起点加上时长，不是只看时长。音轨终点要和整片时长一致，而且画面终点更早。两者的差要大于 0、并且不超过 200 毫秒。真正切的时候再读这一路画面的帧率，把差补到整帧，补完仍不得超过 200 毫秒。满足时，用最后一帧定格填满这扇窗口，音轨整段保留。窗口寻址仍用容器里的窗口起点，不加画面流自己的起点。取样仍是第 0、3、6、9 秒。切完仍用原来的 12 秒正负 150 毫秒校验。

画面本来就在第 0 路时，切片身份仍写成 `0:v:0`，已经成功的缓存可以继续用。画面在别的序号时，地图、滤镜、帧率和身份都写这个绝对序号。

差为 0，不补。画面终点已经到达或超过整片时长时，端点是已知的，不补画面，也不因此停下。切完的音轨仍用原来的 150 毫秒覆盖检查。差超过 200 毫秒，准备阶段就拒绝，并且不创建输出。中间的窗口不补。起点或时长缺失、空、非有限或为负，帧率读不到，或者画面还没盖住整片而音轨终点又和整片对不上，切到这一部时停下，不猜测时间。别的片子如果探针超过 200 毫秒，整批准备也会拒绝。探针算不清只记下来，不因此拒绝还没切到的片子。多路真实画面、只有封面、或指定音轨不在，准备阶段就拒绝，不拖到开切。已经成功、身份里没有这条补帧记录的切片继续复用，不重切。

新目录要另写 `videoTailRecovery`。它和存储中断不能同时写。对象里要有模式版本 `video-tail-recovery-v1`、片号、那一条最后一窗的候选、残片文件哈希和快照哈希。`videoTailPolicy` 也必须是上面那条定格策略。准备阶段先核对快照，再核对每一部已成功的音画、文字和共享文档。只省略这一条从未派发的残片：没有成片、没有切片记录、没有派发状态、没有响应，而且它就是该片的最后一扇。其它残片、未完成状态、孤立响应或哈希不符，都整批拒绝，这时还没有新目录。

新目录里的嵌入协议带上这条定格策略。转写协议按原字节复制，已经记下的 `recovery_origin` 仍指向更早的那棵树，也不重新做末字复核。回执写明模式、白名单、省略的残片、复用了多少条成功音画，以及模型请求数是 0、要重切的残片数是 1。它不是整批成功，也不是转写准确性验收。

```bash
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-recovery.py prepare --config "$RECOVERY_CONFIG"
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py run --config "$RECOVERY_CONFIG" --film FILM
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py run --config "$RECOVERY_CONFIG"
```

音轨内部的时间戳空档是另一条显式规则，不能靠把静音接到结尾、拉长或压短后面的声音来过。配置里的 `audioGapPolicy` 必须正好是 `audio-gap-silence-at-source-pts-200ms-v1`。没写，或写成空字符串，切片方式和以前一样。写成别的值，准备阶段就拒绝。

这条规则只在新切一扇窗口、并且测到至少一处大于 1 毫秒的正向内部空档时启用。空档要落在窗口里面，按切片时间轴的原位置补静音。音轨包上的时间会先减去这条音轨自己的起点，才是和画面同一次寻址后的位置。开头或结尾本来就短一截，仍走原来的时长检查，不算内部空档。一扇 12 秒窗口里，这些内部空档加起来不得超过 200 毫秒；超过就停下，不写出切片，也不用补静音掩盖任意丢失。时间戳逆行、重叠、缺失、不是有限数，或者空档位置对不上，都停下。小于等于 1 毫秒的抖动当成连续音轨，命令和身份都保持原样。

补过的切片把策略、空档条数、总长和每一处相对窗口起点的起止写进身份。输入哈希也带上这些字段。没有空档的切片不写这些字段，已经成功的缓存哈希保持不变，也不重切。上传前先把音轨解码成采样来数，容器时长单独显示 12 秒也不够。解码结果仍要落在 12 秒正负 150 毫秒里，画面取样仍是第 0、3、6、9 秒。

新目录要另写 `audioGapRecovery`。它不能和存储中断或片尾恢复同时写。对象里要有模式版本 `audio-gap-recovery-v1`、片号、候选、输入哈希、状态哈希、响应哈希、切片哈希、切片记录哈希、快照哈希，以及错误码 `invalid_media`。`audioGapPolicy` 也必须是上面那条补静音策略。画面是否需要定格，不由这条恢复来判断；协议里已经有的定格策略会保留。

准备阶段先核对快照。已经成功的音画、文字和共享文档仍逐条核对。只省略白名单上那一条失败窗口的四份文件：响应、状态、切片和切片记录。它必须是唯一的未成功音画，状态是 `terminal-invalid`，HTTP 是 422，响应错误码是 `invalid_media`，而且每一份哈希都对得上。第二条失败、未知状态、哈希不符或错误码不对，都整批拒绝，这时还没有新目录。还没开始的窗口没有状态文件，不算失败。

新目录里的嵌入协议带上这条空档策略。之后的普通运行会把这扇缺了的窗口切一次。白名单本身不会让一次普通运行重发旧目录里仍在的失败。回执写明模式、白名单、省略的四份文件、复用了多少条成功音画，以及模型请求数是 0、要重切的窗口数是 1。它不是整批成功，也不是转写准确性验收。

```bash
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-recovery.py prepare --config "$RECOVERY_CONFIG"
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py run --config "$RECOVERY_CONFIG" --film FILM
$PY -B plugins/EmbeddingVideoSearch/scripts/yuanying-batch.py run --config "$RECOVERY_CONFIG"
```

再跑一次 `prepare` 时，新目录的字节如果和回执一致，就不再重写，也不再复核。`--film` 只进入点名的那一部。它可以重复。写了不存在的编号会拒绝。它不放宽失败即停。

屏幕上的进度只显示 `F001` 这类编号、覆盖状态和数量，不显示路径、原文、向量或令牌。带路径的清单留在输出目录的私有文件里。

`run` 和 `resume` 的退出码：0 表示每一部都是 full。1 表示准备未就绪，例如模型目录不完整。2 表示普通阶段失败、还有未完成的片，或已完成的片里仍有文字缺口。3 表示停在未知、超时或被打断的推理上。普通失败、未知和超时都会停住整批，不会继续下一部，也不会把未完成的结果写成 full。已经按规则跳过语言、并且音画已经完整的片，不会挡住下一部；这种批次退出 2，进度里有缺口，但没有 `stopped`。只跑 `--film` 时，没被点名的片保持原状态，整批因此也不是 0。`--film F011` 时如果这一部没有硬失败，退出码也是 2，进度里没有 `stopped`。其余片都跑完以后，只要还留着文字缺口，退出码仍是 2，覆盖是 gapped，没有 `stopped`。退出码 2 并且进度里有 `stopped`，才是真的失败把整批停住。有文字缺口的片，默认导入会整批拒绝。只有导入配置显式写成 `approved-language-gap-import-v1` 时，才按上文规则写入新索引，整批仍记为 gapped。每一阶段的日志写在该片已有的 `logs` 目录里，后一阶段继续使用这个目录。

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
