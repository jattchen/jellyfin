# 本地多语种转写：下一轮候选

状态：2026-10-15。只读比较，没有安装，没有下载权重，没有新的转写或推理。本文件作者没有读取或解码视频。

阶段 A 已经在本机做完，不是待批准、未安装或语种未知。用户已确认三条音轨主要是普通话。whisper.cpp `v1.9.5` 与非量化 `large-v3-turbo` 已装在 `/Volumes/Turbo/Library/transcription-worker/`。14 个约 12 秒短片、合计约 168 秒，显式 `zh` 各转写一次。五条对白的文字与粗粒度区间确认 **2/5**，低于 4/5，没有进入阶段 B。细节在 [对白文字计划](embeddinggemma2-dialogue-text-plan.md)。不要再推荐跑 turbo。

更早的向量试验仍分开保留：联合表示是画面前三 5/5、对白前三 3/5、负例 0/2；纯音频对白确认 2/5；固定 RRF 对白确认 2/5。

用户补充的目标是：主要普通话之外，最好还能覆盖任何语言。下面只比较能下载、能在本机考虑的路线。宣传数字不是这台 Mac 的实测。

## 建议主 Agent 裁决

先做一次有限的 Qwen3-ASR-1.7B 文字试验，作为 turbo 失败后的新候选。Whisper 完整 `large-v3` 留作同一套已安装 Metal 程序上的广语种备选，本轮不自动开跑。不做自动路由，不把多个模型串成产品。

1600 种以上语言的 Omnilingual ASR 单独算覆盖问题。它的 Apple 预编译包是 CPU，不是已经证明的 Metal。7B 对这台 16 GiB 机器不合适，小号模型不能套用 7B 的分数。

## 三条路线

### 1. 首要新候选：Qwen3-ASR-1.7B

[Qwen3-ASR README](https://github.com/QwenLM/Qwen3-ASR/blob/main/README.md) 写明 1.7B 与 0.6B 支持语言识别和语音识别。标题里的 “52 languages and dialects” 是 **30 种语言加 22 种汉语方言**，不是 52 个独立语种。表内 30 种语言含普通话、英语、粤语、日语、韩语等；22 种方言含吴语、闽南语和多种官话口音。权重可从 `Qwen/Qwen3-ASR-1.7B` 与 `Qwen/Qwen3-ASR-1.7B-hf` 下载。时间戳要另加 `Qwen3-ForcedAligner-0.6B`，对齐语言是 11 种：中文、英语、粤语、法语、德语、意大利语、日语、韩语、葡萄牙语、俄语、西班牙语。对齐器不改正写错的字。字幕上的时间与转写文字是两件事：有可读字幕时时间可以来自字幕；没有字幕时，对齐器只给已有文字估时间。

官方多语表的粗标题是 WER。[论文](https://arxiv.org/html/2601.21337v2) §4.1 写明：普通话、粤语、韩语用 CER，英语等按词分界的语言用 WER；跨语种是不加权宏平均，也就是不同单位混在一个平均数里。README 标题和论文单位要分开记。多语表的平均数不要叫成统一的 WER。

同一 README 里，Qwen3-ASR-1.7B 对 Whisper-large-v3：

| 分组 | Qwen 1.7B | Whisper-large-v3 |
| --- | ---: | ---: |
| Fleurs 首 12 语 | 4.90 | 5.27 |
| 额外 8 语（Fleurs †） | 6.62 | 6.85 |
| 最后 10 语（Fleurs ††：cs, da, el, fa, fi, fil, hu, mk, ro, sv） | 12.60 | 8.16 |

最后 10 语 Qwen 明显更差，必须保留。不能写成跨全部语言普遍更好。中文 AISHELL-2 是 2.71 对 5.06。按论文，普通话用 CER，所以这一对比可以标成 CER；README 那张表的标题仍写着 WER。

本机若要采用本次找到的 Apple GPU 加速路径，可用社区 MLX；官方 PyTorch/Transformers 也有公开实现，但本次未确认其 MPS 完整可用，不能说它没有任何 Mac 路径。官方表不保证 MLX 等效。[mlx-community/Qwen3-ASR-1.7B-bf16](https://huggingface.co/mlx-community/Qwen3-ASR-1.7B-bf16) 写明由 `Qwen/Qwen3-ASR-1.7B` 用 mlx-audio 0.3.1 转换，并叫读者去看原模型卡。社区运行时 [moona3k/mlx-qwen3-asr](https://github.com/moona3k/mlx-qwen3-asr) 自己的对照写：多语 100 条里 67% 文本完全一致、64% token 完全一致；其余多是用词、数字写法或标点不同。这不是完全等效，也不是这台机器上的成绩。同一 README 把 1.7B 的内存写成约 3.4 GB。主 Agent 只读 Hugging Face 元数据、没有下载文件：

| 仓库与修订 | 权重大小 |
| --- | ---: |
| `Qwen/Qwen3-ASR-1.7B` `7278e1e70fe206f11671096ffdd38061171dd6e5` | 两个 safetensors 合计 4,698,521,512 字节 |
| `Qwen/Qwen3-ASR-1.7B-hf` `bcd2b5b7f32b480ab5790554cfa8347f246a14f3` | 单文件 4,076,193,080 字节 |
| `mlx-community/Qwen3-ASR-1.7B-bf16` `e1f6c266914abc5a46e8756e02580f834a6cf8a7` | 4,076,186,653 字节 |
| `mlx-community/Qwen3-ForcedAligner-0.6B-bf16` `53c8c0e46733eec430e4b53dd6471d0e5dee45f8` | 1,835,539,240 字节 |

后两个未量化 bf16 文件相加约 **5.51 GiB**。不能用 “1.7B” 乘 2 推成大约 3.4 GB 的文件，也不能把文件大小当成峰值内存。moona 的约 3.4 GB 是作者内存估计，文件大小与运行内存属于不同口径，不能仅凭文件大小判断内存估计是否正确；这台 M4 上的峰值内存仍未知。

### 2. 低成本广语种备选：Whisper 完整 large-v3

同一个已经装好的 whisper.cpp Metal 程序，切换为 ggml 的完整 `large-v3`。[固定 v1.9.5 模型表](https://github.com/ggml-org/whisper.cpp/blob/v1.9.5/models/README.md)给出磁盘 **2.9 GiB**；[运行器内存表](https://github.com/ggml-org/whisper.cpp/blob/v1.9.5/README.md#memory-usage)里对应 “large” 的一行约 3.9 GB，那一行不是在这台 M4 上测的。[OpenAI 模型卡](https://github.com/openai/whisper/blob/main/model-card.md) 的训练数据包含英语和另外 98 种语言，准确度不均匀。命令行可以显式指定语言；未知时用 `auto` 并保留检测结果。用户已确认这三条主要是普通话，那是这三条的语言依据，不是 “任何语言” 已经覆盖。

完整 large-v3 **还没有在这台机器上跑过**。turbo 已经失败，不能把 turbo 的 2/5 当成 large-v3 的成绩，也不能反过来假设 large-v3 一定会过 4/5。它的优点只是接入成本低：不必新开 Python 栈。段级时间仍只是候选，要实测是否包含完整意思以及大约 5–10 秒的定位。

### 3. 1600 种以上的长尾：Omnilingual ASR

[Omnilingual ASR](https://github.com/facebookresearch/omnilingual-asr) 公开 CTC 与 LLM，规模从 300M 到 7B，并有 `_v2` 和可处理长音频的 Unlimited 变体。权重在 `dl.fbaipublicfiles.com`。普通 CTC 与 LLM 只接受短于 40 秒的音频。公开 `transcribe` 示例返回文字，快速示例里没有 5–10 秒的段级时间，不能把它当成已有时间的字幕。

Apple 落地和 1600 语覆盖要分开。[fairseq2 说明](https://github.com/facebookresearch/fairseq2/blob/main/README.md) 有 Apple arm64 预编译包，0.8 对应 PyTorch 2.9.1、Python 3.10 到 3.12。这是 **CPU** 轮子，所以不能写成这台 Mac 不能安装。公开 [pipeline.py](https://github.com/facebookresearch/omnilingual-asr/blob/main/src/omnilingual_asr/models/inference/pipeline.py) 在 `device` 为空时，有 CUDA 就用 `cuda`，否则用 `cpu`，没有自动改走 MPS。CPU 轮子不等于 Metal 加速。源码是否还能手动放到 MPS，这次没有核完。

官方速度和显存表的条件是 A100、BF16。7B LLM 的推理显存约 17 GiB、下载约 30 GiB，对这台 16 GiB 且已有 EmbeddingGemma 常驻的机器不合适。7B 的分数不能套到 300M 或 1B。公开逐语种表只属于 7B LLM：[CSV](https://github.com/facebookresearch/omnilingual-asr/blob/main/per_language_results_table_7B_llm_asr.csv) 里 `cmn_Hans` 的 CER 是 11.5、训练时长 9941.5 小时。这个 11.5 不能和 Qwen 的 AISHELL-2 或 Fleurs 混比。

## 本机运行与维护边界

- [MLX 官方安装说明](https://ml-explore.github.io/mlx/build/html/install.html)要求 Apple Silicon、原生 Python 3.10 及以上和 macOS 14 及以上。本机前轮曾遇到 Intel 工具链默认目标，因此未来必须核对 Python 进程为原生 arm64，不能直接在系统 Python 3.9 或现有 embedding 环境上安装。
- 社区运行器 [moona3k/mlx-qwen3-asr 的 pyproject.toml](https://github.com/moona3k/mlx-qwen3-asr/blob/main/pyproject.toml)当前版本为 0.4.4，Apache-2.0；它提供离线 CLI、JSON/SRT 等输出以及独立的 MLX 对齐器。作者基准机器是 M4 Pro 48 GB，不能外推本机 M4 16 GiB。对齐支持语言与 ASR 支持语言分别判断；不支持对齐的语种不能凭分句标点生成声音起止。
- 新运行环境、模型和运行结果继续分开保存到已批准的 Turbo 根目录，采用新 release 和独立模型校验目录；不覆盖 whisper.cpp，也不动 embedding-service。此处只是后续布局建议，本轮没有创建新环境或模型目录。
- Qwen 模型及对齐器为 [Apache-2.0](https://github.com/QwenLM/Qwen3-ASR/blob/main/LICENSE)；Whisper 上游代码/权重按 [MIT](https://github.com/openai/whisper/blob/main/LICENSE)，具体转换包另核验其声明；Omnilingual 的 [Apache-2.0](https://github.com/facebookresearch/omnilingual-asr/blob/main/LICENSE) 与 fairseq2 的 MIT 属不同组件。
- ASR 和对齐可分进程顺序执行，避免无必要地同时驻留两个模型；同机 embedding 的 GPU 竞争仍需测量。权重下载量不是峰值内存，可加载不代表能与日常应用同时稳定运行。本轮没有安装、测峰值或做并行推理。

## 不作为本轮候选

- [nvidia/canary-1b-v2](https://huggingface.co/nvidia/canary-1b-v2) 是 25 种欧洲语言的转写，语言表里没有中文。NVIDIA 的另一模型 [Parakeet-TDT-0.6B-v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)也列出这 25 种欧洲语言。官方速度比较是别的硬件，不是这台 Mac。
- [FunAudioLLM/SenseVoiceSmall](https://huggingface.co/FunAudioLLM/SenseVoiceSmall) 开放的小模型写明普通话、粤语、英语、日语、韩语。说明里另有 “50 种以上语言” 的更大训练描述，但 Small 的可指定语言是这五种。它不是 1600 语或 “任何语言” 的替代。
- [Qwen-Audio-3.0-ASR](https://arxiv.org/abs/2609.07549) 新于 Qwen3-ASR 的报告。[官方项目页的 Model experience](https://qwenaudio.github.io/qwen-audio-3.0-asr/)目前链接到阿里云 Model Studio 的短音频、文件与流式 API；本轮未核到可下载本地权重，因此不进入当前本地候选。

## 不能保证的事

“任何语言” 不能保证。Qwen 的 30 语在最后 10 个 Fleurs 语言上差于 Whisper；Whisper 的约 100 语准确度不均匀；Omnilingual 模型家族的语种覆盖是 1600 多种，不能把这种覆盖等同于每种语言都准确；公开逐语种成绩表属于 7B，不能当作小号模型或这台机器的成绩。

任意两种语言混在同一句里，没有充分证据。社区页面把个别 MLX 路径写成中英混合可用，那是该转换的说明，不是官方全组合测试，也不是本机结果。

对齐器不纠正错字。turbo 已把 “西线” 写成 “西县”、把意思改掉；换一个会打时间戳的模型，不会自动把错字改对。字幕时间独立存在：将来若有可读字幕轨，时间可以来自 Jellyfin 的字幕接口，不必等 ASR。

## 若以后做，最小验证只建议到这里

不在本轮执行。

1. 仍用原来的 14 个短片，只换 Qwen3-ASR-1.7B 做文字门槛。显式使用已确认的普通话，保留检测或指定语言的记录。不重跑 turbo，不对 42 个重叠窗口做转写，不扩到约 68 分钟原片。
2. 文字与完整意思先过事先的门槛（沿用至少 4/5，且没有把没说过的话写成原话）。没过就停，保留成功和失败，不自动改跑 Whisper large-v3 或 Omnilingual。
3. 只有文字门槛通过后，才对这 14 段做有限对齐。对齐失败或时间盖不住完整意思，记成 “文字可用、定位未通过”，不能改短标签。
4. 其他语言用另行准备的小样本，不拿这 14 段普通话结果代表 “任何语言”。新的文字向量若要做，必须是 `task=document` 和独立签名，不沿用 0.35。原 7 条查询向量已有则可复用。语义文本不保证无答案被拒绝。

是否执行新一轮试验由用户确认；主 Agent 负责具体边界与验收。推荐只批准一个固定候选的有限转写，再根据结果决定对齐或其它模型；不自动把 Whisper large-v3 接在失败之后运行，也不安排自动路由。

本轮零安装、零新推理。唯一新文件：`/Users/mac/Documents/projects/.worktrees/paseo/1yk5nvz3/gemini-video-search/docs/local-multilingual-transcription-research.md`。

## 主 Agent 独立核验

主 Agent 直接读取官方模型卡、论文 §4.1、OpenAI tokenizer、固定 whisper.cpp v1.9.5 模型表、Omnilingual/fairseq2 文档与 pipeline 源码，并读取 Hugging Face 文件大小及 revision 元数据；未下载权重。OpenAI [tokenizer 的 LANGUAGES](https://github.com/openai/whisper/blob/main/whisper/tokenizer.py)当前列 100 个语言 token（含新增粤语），这与旧模型卡的训练语种描述不是同一种统计。本报告用“约 100 语”，不将语言 token 数作为准确度承诺。

Qwen 多语组里的最后十语均在公开支持范围内却仍差于 Whisper，证明支持列表不能代替质量测试。两组成绩来自 Qwen 作者自己的统一对照，不是独立第三方或本机结论；中文 CER 2.71/5.06 不外推成三个关键错词必定改善。

本机现有阶段 A 的 14 条成功任务与失败质量结论保留。本轮没有新增 ASR、对齐、向量、媒体读取或服务调用，没有改变索引和网页；唯一新交付是本研究文档。此前调研文档保持历史快照，新决策以本文和已执行阶段 A 为准。
