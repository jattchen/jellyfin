# Embedding Video Search

独立 Jellyfin 插件，只针对未改过的官方 **12.2.0**。索引放在宿主 `DataPath/embedding-video-search/8f4c2a91-6d3e-4b7a-9c15-2e8d0f6a4b17/`，不放进插件 DLL 目录，也不写 Jellyfin 核心数据库。

当前这版插件测试是 80 项，都在本目录里跑，另有离线原片重绑定脚本回归。构建时会用到这里的 `global.json`（SDK 10.0.402）：

```bash
export DOTNET_CLI_TELEMETRY_OPTOUT=1
./build.sh
```

`./build.sh` 会跑测试，并打出两份加载目录：`artifacts/plugin-with-sqlite`（自带 SQLite 程序集）和 `artifacts/plugin-host-sqlite`（只有插件 DLL）。哪一份能在官方宿主里打开索引，以临时宿主实测为准。

## 单片离线试点

用户已授权一部完整原片的独立试点，范围见 [单片计划](../../docs/full-film-pilot-plan.md)。这是显式运行的离线脚本，不是插件的自动转录或整库入库功能。模型、原片、302个联合窗及资源设置在私有 `protocol.json` 中固定；私有原文、音频、向量和检查点不进入Git。

当前隔离实例已导入329条joint和164条text，第一部完整、另两部仍为partial；新曲线绑定新候选池，评分公式未改。处理、接口及2026-10-17主Agent真实浏览器搜索、完整原片播放和位置预览证据见 [单片实际记录](../../docs/full-film-pilot-acceptance-20261016.md)。

- `full-film-transcribe.py --run RUN --stage prepare`：用协议中已安装的worker Python依次运行 `prepare`、`asr`、`align`、`documents`。保留65块的原始转写、对齐、采样绝对位置、耗时和内存记录，分段不改字；截断、失败及未知状态会阻止续做。标点/空白单独保留但不送文字编码，越界单元保留并拒绝，定位资格按最终整数ticks判断。
- `full-film-embed.py --run RUN --stage joint`：先运行joint，再将stage改为text。复核输入/响应哈希和模型签名后复用成功检查点；新请求逐条发送，不自动重试。joint可加 `--prefetch-clips`，最多提前一个CPU切片、2个编码线程，仍只有一个模型请求；text不能加此选项。本机HTTP显式绕过系统代理，避免loopback被代理接管。
- `full-film-import.py --run RUN --references REFERENCE_CACHE --output NEW_DIRECTORY`：从备份创建全新离线索引，完整校验302个联合窗、文字响应及原16条参考缓存。只替换试点原片的启用候选，保留另两片及旧版本；沿原算法对新候选池生成新曲线，不新增参考query输入。不会写运行中的索引，安装新副本仍需独立验证、备份和停止自有隔离宿主。

ASR/对齐与两路embedding不要同时推理。单片脚本合成检查分别为 `full-film-transcribe-test.py` 的20项（worker Python）和 `full-film-embed-test.py` 的11项（Python3）；不代替真实导入、网页、播放和权限验收。

## HTTP

### 2026-10-16 原片页内播放与位置预览

原14段阶段的确认范围见服务端 `docs/full-source-web-plan.md`；该阶段已验收并本地保存。它取消固定查询白名单和累计95条的旧试验停止线，复用67条向量绑定完整原片播放。后续单片索引试点见上节，不改这里的播放协议。

- `POST playback`：当前登录的标准Authorization，正文 `{segmentId, expectedBindingVersion}`；返回 `playbackId/manifestUrl/itemId/mediaSourceId/bindingVersion/startTicks/durationTicks/timelineOriginTicks`。缺失或不一致的绑定版本返回409。完整原片HLS清单从0计时，`timelineOriginTicks=0`，起播/跳转使用原片绝对时间。
- `GET playback/{id}/resources/{resourceId}`：资源编号不授予权限。每个请求由宿主重新鉴权，再检查创建者、原登录头指纹、播放权限、候选绑定、媒体源和当前内容版本。固定loopback宿主协商并转发官方HLS，仅复制既有视频/音频编码。清单内后续地址全部改写为插件入口，移除官方生成地址中的token参数；前端HLS请求必须带Authorization，不使用cookie或裸静态原片地址。
- `DELETE playback/{id}`：先撤销媒体访问，再请求宿主释放编码资源；上游失败返回503 `cleanupPending`并保留可重试清理记录，不谎报停止成功。访问记录最多16个、闲置30分钟；每会话资源最多4096项。已缓冲字节无法收回。
- `GET segments/{id}/preview?expectedBindingVersion=...`：当前用户鉴权的小幅位置JPEG。只提取候选起点一帧，单并发、15秒预算、内存最多128图，不生成全片trickplay。缓存及生成后重新比较完整绑定；版本改变409，无图404，正忙429。前端通过鉴权fetch获取Blob，切换身份/查询清理。
- 管理员 `GET admin/source-facts/{itemId}?mediaSourceId=...`：输出当前启用库中的媒体身份、内容版本、时长、原始容器名和音轨编号；不输出路径或向量。普通用户403。原片导入须采用这些原始事实，网页DTO的容器名可能已经规范化，例如`mp4`与`mov,mp4,m4a,3gp,3g2,mj2`不能混用计算内容版本。文件为符号链接时，事实检查跟随最终视频文件，避免把链接创建时间当作媒体修改时间。

新增入口的权限验证不代表宿主所有原有接口均安全；隔离宿主原有静态流未登录206已单独留证，当前新增播放器不使用该地址。未修改正式实例或官方宿主核心。

### 2026-10-16 两路缓存候选契约（短片阶段）

导入现有14条样片的42条joint和25条text后，启用独立区间合榜。`candidate_representations` 以 `(channel, version)` 保存两套独立表示、输入规则、原文、float64向量和参考曲线；原 `signatures/media/windows` 代次保持原样。text只接受有原始 `task=document` 响应的缓存。搜索文字仍以 `task=query` 编码一次，共用于两路；本阶段不实现document编码、自动ASR或长片调度。

新网页以 `capabilities.qualityPolicy.scoreMeaning="referenceTailR"` 识别新契约。`rankingVersion="reference16-logistic-tail-f64-v1"`，`relatedMinimum=null`，`calibrated=false`；`representations` 列出两路独立版本，`coverage.candidatesSearchable` 为当前用户可见且内容有效的候选数。没有导入时仍保留以下旧joint契约，新网页在片段区域提示接口不符，原标题查询照常。

搜索结果 `quality="candidate"`、`score` 为R，另有 `rawScore`（余弦）、`representation`（`jointAudioVideo` 或 `transcriptText`）、`candidateId`、`anchorId`、`canonical`、`representationVersion` 和 `referenceVersion`。R不是概率或百分比，不使用0.35门槛。合榜严格按每条候选自己的R降序；严格并列按原 `anchorId`，再按固定text/visual通道别名顺序。不得把同锚点的低分区间借高分组提到前面。只有样片来源和局部时间范围完全相同时取较强的一条；时间相交或只共享锚点都不合并。

`itemId/mediaSourceId/startTicks/endTicks` 是宿主样片的真实身份与局部时间；`canonical.sourceItemId/startTicks/endTicks` 保留原片身份与绝对时间，仅用于追踪。片段详情返回同一候选身份、表示和canonical，`score=0`。每次搜索发布及点击都重新检查当前用户、启用库、媒体源、文件事实、表示版本和当前模型。更换文字表示不重建joint。

为避免播放返回搜索页后重复编码，同一用户的已完成query向量按服务地址、模型完整签名和query任务规则在进程内最多保留32条。失败、取消、未知响应不缓存，不自动重试，不共享未完成请求；结果、权限和片段有效性从不缓存，每次搜索及点击照常复核。当前模型不可用或签名变化时不能借缓存绕过。

管理员可在搜索正文传 `diagnostics=true`，通过 `rawResults` 导出全部获准候选的原始排序（不含向量或文件路径）。普通用户请求诊断会被拒绝；普通 `results` 仍遵守 `limit`。这是内部回归入口，不新增查询推理。

离线导入工具是 `scripts/import-candidates.py`：显式传入 `--acceptance-root`、`--pilot-root`、`--text-root`、`--mapping`、`--index`、`--output`。默认预检查，`--execute` 才写入。只允许停止的测试副本，拒绝历史原索引；校验25份document响应、42条原成功窗口、曲线、模型及双重时间映射，保存导入前数据库备份并断言原六张业务表哈希不变。输出包含私有向量和原文，只能放私有验收目录，不能提交Git。

### 按原片分组

能力接口始终带 `features: ["grouped-video-v1"]`。新网页先看这个标识，再使用下面的字段；旧插件没有该标识。

`groupByVideo` 为 true 时，`limit` 表示最多返回几部影片。省略时是 3，只接受 1 到 3。每部影片再取自己分数最高的 5 条，顺序沿用原来的 R、并列和部分相交规则，不因条数加分。`results` 按影片顺序把这些命中接成一维数组。`diagnostics` 的 `rawResults` 仍是未分组的完整排序。没有导入两路候选时，带 `groupByVideo` 会直接拒绝，不会去编码查询。

搜索命中和片段详情带 `bindingVersion`。它由片段、影片、媒体源、内容版本、起止时间、来源，以及候选编号和该路表示版本拼成。旧窗口没有候选时，后两项留空。同一 segmentId 换了候选或表示版本，这个值也会变。点击播放时把当时看到的值原样放进 `expectedBindingVersion`。

搜索命中和片段详情可以多一个 `transcript`。它只包含这条候选自己已经保存的原文，而且只有当前用户仍有权看到该片段时才会出现。没有原文就不写这个字段。仅仅时间相交的另一条候选，不会把对白借过来；播放范围完全相同的两条才会留下较强那条，并带上这一范围自己的原文。

原14段接回完整原片时，67条只覆盖每部片子里的一小段，对应媒体行为 `partial`，每条命中的 `indexComplete` 为false。后续单片导入只将完成全部窗口的该片改为 `complete`；另两片继续为partial，整体库仍非完整覆盖。

### 只使用已导入候选

索引设置 `imported_candidates_only` 默认是关闭。原片重绑定脚本在写入时把它设为 `1`。打开之后，后台不扫描片库、不补窗口、也不派发视频；手动暂停挡不住扫描，所以不能只靠暂停。搜索，以及点击时核对当前文件，照常进行。管理员状态的暂停原因里会有 `importedCandidatesOnly`，索引状态显示为 paused。这个模式下，重试失败和重建都会拒绝，避免生成全片窗口或新的视频请求。

查询向量缓存和模型派发保持原样。同一用户已经成功的 query，按服务地址、模型签名和 query 任务最多留 32 条。失败、取消和未知响应不缓存，也不自动重试。查询和视频共用一个派发器，最多一个还没收到完整响应的请求，本地排队最多 8 个。

原片重绑定脚本是 `scripts/rebind-full-source.py`。参数是 `--mapping`、`--index`、`--output`。默认只预检查；`--execute` 才写入。映射文件是 3 个元素的数组，每项含 `sourceItemId`、`itemId`、`mediaSourceId`、`libraryId`、`contentVersion`、`runtimeTicks`、`sourcePath`、`sizeBytes`、`modifiedTicks`、`container`、`audioStreamIndex`。`sourceItemId` 对应候选里的 canonical 原片编号，UUID 写法可以带或不带连字符。

脚本从目标索引当前启用的两路表示读取这 67 条，保留 canonical、向量、分数曲线、anchor、candidateId，以及旧行里的样片内容。播放起止改成 canonical 的绝对时间。新版本变为启用，旧版本改为 inactive，旧内容不改，并使用新的 segmentId。媒体表只新增这 3 行，状态为 partial；signatures、windows 和原来的样片行不动。执行前要求索引没有被其他进程打开、`manual_pause` 为 1，并且不是历史原索引。备份写到输出目录的 `pre-rebind-index.db`，不覆盖已有备份。已经绑过的索引再执行不会再造一版。输出说明不含向量和原文，仍应放在私有目录。实例还在运行时不要加 `--execute`。

以下余弦/related/weak说明仅适用于尚未导入两路数据的旧joint模式。

前缀 `/EmbeddingVideoSearch/v1/`。响应由插件自己写成 camelCase，不跟宿主默认的 PascalCase。

认证用宿主现有策略。请求必须带标准头：

`Authorization: MediaBrowser ... Token="..."`

没有有效的 `Jellyfin-UserId` 就拒绝。调用者不能在参数里指定用户。1 秒 = `10000000` ticks。请求里的 Guid 可以是 32 位无连字符，或带连字符的 8-4-4-4-12。两种写法是同一个 ID。响应仍只输出带连字符的写法。

`POST /EmbeddingVideoSearch/v1/search` 可以多带可选的 `parentId`。它表示当前界面的库、系列或季。若是已启用且可见的库，结果只留在这个库里；若是系列或季，只留其中的影片。它不是 `libraryIds` 的另一种写法。

`GET /EmbeddingVideoSearch/v1/segments/{segmentId}` 在点击前再核对当前用户、启用库、当前内容版本、代次和媒体源。路径里的 `segmentId` 同样接受两种 Guid 写法。有效时返回和搜索命中相同的字段，并多一个 `representation`，值为 `jointAudioVideo`。这次调用不重新计算相似度，`score` 为 0。当前用户仍能看见影片但片段已失效时返回 409，`error.code` 为 `stale` 或 `incompatible`。找不到或无权返回 404。

### GET `/EmbeddingVideoSearch/v1/capabilities`

登录用户。不含文件路径、向量或未授权库。

```json
{
  "apiVersion": "1",
  "ticksPerSecond": 10000000,
  "availability": {
    "search": "available",
    "searchReason": "none",
    "indexing": "idle",
    "indexingReasons": []
  },
  "compatibility": {
    "hostVersion": "12.2.0",
    "supportedHostVersions": ["12.2.0"],
    "hostCompatible": true,
    "model": "google/embeddinggemma-2",
    "revision": "914f7f89142e33e77833254d9c9b90c3cef7303b",
    "processingId": null,
    "dimension": 768,
    "normalized": true,
    "inputVersion": "joint-x264-veryfast-crf28-yuv420p-aac-16000-mono-v1",
    "windowVersion": "6s-4s-tail-v1",
    "audioVersion": "source-default-audio-index-v1",
    "indexSignatureMatches": true
  },
  "coverage": {
    "libraries": [{ "libraryId": "guid", "name": "display name" }],
    "videosTotal": 0,
    "videosComplete": 0,
    "videosPartial": 0,
    "videosFailed": 0,
    "videosWaiting": 0,
    "windowsSearchable": 0,
    "indexComplete": false
  },
  "window": { "lengthTicks": 60000000, "stepTicks": 40000000, "timeSource": "recordedWindow" },
  "qualityPolicy": { "relatedMinimum": 0.35, "calibrated": false, "scoreMeaning": "cosine" }
}
```

`availability.search`：`available`、`empty`、`incompatible`、`modelOffline`、`timeout`、`cancelled`、`queueFull`、`schemaNewer`、`hostIncompatible`、`writerLocked`。

`availability.indexing` 在没有更严重的阻塞时，手动暂停或正在播放视频都是 `paused`。`indexingReasons` 里手动暂停是 `manual`，播放暂歇是 `playback`。两条可以同时出现。播放结束只去掉 `playback`，不会清掉已经写下的手动暂停。搜索本身仍可以是 `available`。

管理员状态里的 `pause.manual` 和 `pause.playback` 分开。`pause.playback` 为 true 表示宿主当前有视频播放会话，包括播放器停在暂停。`pause.reasons` 使用同样的 `manual` 和 `playback`。

`searchReason` 用同一组词，另加 `none`、`noLibraries`。

模型签名对不上时保留旧窗口和向量，搜索改为 `incompatible`，不返回旧向量。`POST .../admin/resume` 只解除手动暂停。

### POST `/EmbeddingVideoSearch/v1/search`

```json
{ "query": "文字", "libraryIds": ["guid"], "parentId": "guid", "limit": 24, "clientQueryId": "可选，最多 80 字" }
```

`libraryIds` 可省略，表示当前用户能看见且已启用的库。`limit` 默认 24，最大 50。

还没有导入两路候选时，`quality` 为 `related` 或 `weak`，`score` 是余弦。导入画面和文字两路候选之后，路由和 `apiVersion` 仍是 1。`qualityPolicy.scoreMeaning` 变为 `referenceTailR`，并带上 `rankingVersion` 和各表示自己的版本。每条结果 `quality` 为 `candidate`，`score` 是 R，`rawScore` 是余弦，`representation` 为 `jointAudioVideo` 或 `transcriptText`，另有 `candidateId` 和 `canonical`。`itemId`、`mediaSourceId`、`startTicks`、`endTicks` 是实际播放媒体的身份和该媒体内时间：样片阶段是局部时间，原片模式是片头起算的时间。播放直接用这些字段，不再叠加canonical偏移。`canonical` 用于原片追踪。片段详情同样带上表示、候选编号和锚点，`score` 为 0，不再当作名次。合榜按固定锚点把两路排在一起；时间只是相交时不折叠。只有可播放媒体源和时间范围完全相同，才留 R 更高的一条。

```json
{
  "apiVersion": "1",
  "queryId": "guid",
  "clientQueryId": null,
  "availability": { "search": "available", "searchReason": "none", "indexing": "idle", "indexingReasons": [] },
  "indexComplete": false,
  "qualityPolicy": { "relatedMinimum": 0.35, "calibrated": false, "scoreMeaning": "cosine" },
  "results": [{
    "itemId": "guid",
    "mediaSourceId": "id",
    "segmentId": "guid",
    "startTicks": 0,
    "endTicks": 60000000,
    "score": 1.0,
    "quality": "related",
    "indexComplete": true
  }]
}
```

### 管理员

需要宿主的 `RequiresElevation`，并且用户记录本身是管理员。

- `GET /EmbeddingVideoSearch/v1/admin/status`
- `PUT /EmbeddingVideoSearch/v1/admin/libraries`，正文 `{ "libraryIds": ["guid"] }`
- `PUT /EmbeddingVideoSearch/v1/admin/settings`，正文可含 `relatedMinimum`、`calibrated`、`embeddingServiceBaseUrl`
- `POST /EmbeddingVideoSearch/v1/admin/pause`
- `POST /EmbeddingVideoSearch/v1/admin/resume`
- `POST /EmbeddingVideoSearch/v1/admin/retryFailed`
- `POST /EmbeddingVideoSearch/v1/admin/rebuild`

`embeddingServiceBaseUrl` 初始为空，不会去连真实模型。地址必须是不带账号的 http(s) 根地址。本机服务预期为 `http://127.0.0.1:18780`，由管理员写入。

`retryFailed` 只把当前有效代次、已启用库、而且文件事实仍对得上的失败窗口放回待处理，并把这些窗口的尝试次数清零。已经成功的窗口和向量留着。手动暂停保持原样。不换代次，也不做整库重建。文件读不到就保持未知，不把失败窗口重新激活。文件已经换过，就按补查规则让旧窗口失效。宿主不受支持、索引只读或结构更新时，这个接口拒绝。

`rebuild` 成功时 `oldGenerationRetained` 为 true，`reindexStarted` 为 true。旧行保留，后台会按新签名重新切窗。

错误正文：`{ "apiVersion": "1", "error": { "code": "...", "message": "..." } }`。未登录 401，权限不够 403，参数错误 400，模型或签名状态 409。

## 模型超时

状态查询 `GET /v1/models` 限 5 秒。文字查询限 30 秒。视频推理限 3 分钟，只发一次。已测到的 6 秒窗口推理约 20.65 秒，所以不能再用 20 秒总超时。

取消返回 `cancelled`。到点返回 `timeout`。服务 429 或 `queue_full` 返回 `queueFull`，并标成以后可以有限次再试。客户端自己不循环重试。

视频请求是 multipart：字段 `request` 为 JSON，文件字段名 `clip`，`video_audio` 为 true。响应里的 `processing_id` 必须和已保存的签名一致。一次实测样本是 `eg2-8108e8f38c5460ac0e33`，代码不把这个值写死。

## 后台索引

管理员选定库之后，后台每次只派发一个窗口。6 秒窗、4 秒步长，尾窗盖住片尾。ffmpeg 路径在每次派发窗口时向宿主现用的编码器路径再读一次。启动时如果这条路径还是空的，这次窗口失败并记下 `ffmpeg_unavailable`，不会改去调用系统里另外一个 ffmpeg。宿主稍后填上路径后，下一次派发用新路径。ffprobe 只用这个 ffmpeg 旁边的同名程序。

ffmpeg 在打开文件之前按输入位置寻址，并显式使用精确寻址，再转码，不做流复制。画面是 libx264、veryfast、crf 28、yuv420p。有声音时转成 16000 Hz 单声道 AAC。这套参数写在 `inputVersion` 里。音轨用的是媒体源上 Jellyfin 记录的默认音轨序号，不是“文件里第一条音轨”的别名；序号记在租约里，规则名是 `source-default-audio-index-v1`。旧的 `default-track-v1` 行留着，但和新签名对不上，搜索会停在不兼容，直到管理员重建。

切片要能读出视频和音频各自的起点和时长。缺字段算未知，拒绝这个窗口，不把缺的数当成 0。允许的误差是一帧画面，再加上一个 AAC 帧（16000 Hz 下 1024 个采样）。模型返回的音视频时长也用同一把尺子。对不上就不发布。

模型结果回来之后、写入可搜索窗口之前，先处理已经排队的删除和替换，再按这条租约的媒体源编号核对当前文件事实和代次。只改标题、文件事实没变，这次结果可以留下。文件换了或删了，旧租约不能发布。读文件失败按未知处理：不发布，也不当成删除。搜索和点击片段时同样核对这个媒体源的当前内容版本，不能只看库里的旧状态。

宿主不是受支持的 12.2.0、索引结构比这版插件新，或这个进程没有写锁时，后台不启动、不写队列、也不派发。原因记在日志和能力接口里。Jellyfin 的标题和播放器照常可用。

成功的窗口可以马上搜，一条视频要全部窗口成功才算完成。

查询和视频共用一个派发器，最多只有一个还没收到完整响应的模型请求。调用方取消或等到软超时，只会停止等待，不会取消已经发出的请求，也不会把位子让出来。还排在本地的请求可以马上取消，取消后不会发出。发出之前会再检查一次权限、代次和暂停。文字查询的等待上限是 30 秒，传输硬超时是 2 分钟；视频硬超时是 3 分钟。切片流一直留到这次 HTTP 真正结束。

正在做的窗口不会被新查询抢走。它结束后，还没取消的查询按先来后到逐个发送，都排在下一段视频前面。不同用户的查询不会互相顶掉。本地排队最多 8 个，再多就返回 `queueFull`。调用方取消，或文字查询的 30 秒等待到了，只拿掉那一条还没发出的请求。完整响应和完整的 429 会让出位子；429 之后后台有限退避。连接中断或硬超时没有完整回执时，先当作完成情况未知，不再发新请求。之后探测 `/readyz`，至少连续两次且间隔 1 秒都是 `ready=true` 且 `admitted_requests=0`，并再核对模型签名，才恢复。缺字段或不是 0 就继续等。这是整个共享服务的空闲观察，不是某一条请求的完成证明。旧的那次结果丢弃。

搜索进行时不开始新窗口。429 或可重试的失败会退避，最多约 30 秒，同一窗口最多试 5 次。进程重启会收回租约，旧租约的结果不能写入。媒体变更只进队列。启动时补查，之后大约每分钟再补查一次。内容替换、删除或移出库会隐藏旧窗口。读不到文件不会当成删除。

Jellyfin 正在播放视频时不派发新窗口，播放结束会自己继续。手动暂停写在索引里，播放结束不会把它清掉。卸载只停止派发并解开媒体事件，索引留在宿主 DataPath。代次变了之后，迟到的旧结果不会成为可搜索窗口。
