# 对白文字重排：下一步有限试验提案

2026-10-15。用户询问“所以是模型的局限，不是机制问题。下一步？” 当前可确认的是EmbeddingGemma在D1的文字语义排序出错，未发现传参、向量计算或排序方向错误；max机制自身不会纠正错误高分。模型局限和检索流程能否改进是两个层面，不能据此认定现有机制已经最优。

本文件只形成具体提案。**尚未安装、下载权重或执行重排推理。** 它超出此前16参考query的试排范围，执行需用户批准新的模型与50对预算。现有三路、索引和服务不变。

## 建议只验证文字重排

现有EmbeddingGemma先找出前10条候选；新增重排模型将“查询＋每条转写原文”一起输入，评估是否表达查询所述意思，再排序。它有机会纠正“天资较差”和“资质愚钝”的误排，但不能承诺这条必过，更不保证全语言准确。

首轮仅五条旧D查询，每条复用已保存的文字向量前10条，共50对。不重算embedding、不改转写、不追加目标到候选列表、不根据成绩换提示词或模型。五条既有合格目标均已在固定Top10内。查询、原文、候选ID和原时间已固定为私有候选清单；评估标签另存，不能送入模型。

这是文字路线的可行性验证。不会把缺少字幕的画面、叫声变成文字来假装重排，也不会将文字重排分直接混入现有joint/audio参考分。未来如何接三路总榜是另一项设计，首轮不做产品改造。

## 候选模型与本机布局

建议固定官方[Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B)。模型卡提供查询/文档联合输入的重排方法，标为0.6B、100+语言、Apache-2.0；这些是官方说明，不是本机质量或速度验收。官方表中的成绩也不代表这五条对白的结果。

本轮读取[官方仓库元数据](https://huggingface.co/api/models/Qwen/Qwen3-Reranker-0.6B?blobs=true)所得revision为`e61197ed45024b0ed8a2d74b80b4d909f1255473`。BF16权重`model.safetensors`为1,191,588,280字节，约1.19GB/1.11GiB，SHA256为`27cd75a405b9c1b46b59abfd88aaa209e6fed2a1972cde9b70e7659537c5e65b`。这不是峰值内存，依赖环境还需要额外空间。本机现有embedding和transcription模型目录未发现该重排器；现有HTTP服务也没有重排接口。

若批准，独立安装到`/Volumes/Turbo/Library/reranking-worker/`，按`models/qwen3-reranker-0.6b/<revision>`、`releases/<versioned pilot>/.venv`、`runs/jellyfin-dialogue-rerank-20261015`分开。复用已有Python解释器创建独立环境，不改现有两个worker的环境；不添加常驻服务、自启或公网接口。优先验证MPS加载，无法运行则记录技术失败，不自动换模型、量化或用另一设备冒充已测MPS结果。

## 固定计算与判定

按官方yes/no打分方法使用末token的yes减no原始logit排序，不生成解释，也不把分数当作已校准的正确率。指令固定为`Given a description of spoken dialogue, judge whether the transcript expresses the described meaning.`，所有50对相同，不出现案例ID、已知目标、原名次或期望答案。输入不截断；超出限制停止，原时间区间保持不变。

继续探索的条件是D1已确认完整目标进入前三，且D2–D5的原已确认目标仍在前三，即这五条旧题达到5/5。它是已见回归题上的路线诊断，不是独立验收。未达到就保存失败，不为D1单独改词救成绩；技术异常也不自动追加请求。无答案拒绝不在本轮内，旧负例不用于挑门槛。

记录首次加载、每查询10对重排耗时、后续查询耗时、进程及可用GPU内存观察，冷启动与稳态分开。该阶段发生在用户查询时，会增加在线开销，具体增量要实测，不能套用前一轮14毫秒的向量查找时间。

私有提案：`/Volumes/Turbo/Library/transcription-worker/runs/jellyfin-trimodal-pilot-20261015/text-rerank-proposal/`，包含`candidate-pairs.json`、`evaluation-only-labels.json`、`proposal.json`。50对清单SHA256为`4daa8696322c80dea33e781aeca048bb40fdfc1b7c54d6505288e8b01ae9f583`，提案SHA256为`bae961ac5643b14d19e45db5f68c9f56c577adaf06b6f663bcb0f4eeab9ea51f`。原embedding预算保持63/95；拟议50个重排对是需要另行批准的新预算，不能静默算入或绕过旧预算。
