# EmbeddingGemma 2：Google 官方视频采样资料核查

核查日期：2026-10-17。问题是把现有6秒联合窗改成15/30秒、或每隔几秒取一帧时，Google 官方有哪些明确建议。此记录区分默认值、可配置能力、示例做法和项目实验判断，不修改已经验收的索引。

## 官方文档已明确的内容

| 项目 | 官方内容 | 应如何理解 |
| --- | --- | --- |
| 采样频率 | 默认1 FPS，允许配置 | 每秒一帧是默认起点，不是所有任务的最优值或强制条件。 |
| 帧数上限 | 教程注明默认`max_frames=32`；示例设为16 | 这是处理器可调整的帧数预算，不是模型只能处理32秒视频。 |
| 超出帧数预算 | 支持`uniform`和`truncate` | 前者均匀选取，后者保留开头；并非整段内容都会保留。 |
| 时间戳 | 可用`add_timestamps`配置 | 教程展示把时间戳加入输入；没有承诺一个整段向量能输出精确事件时刻。 |
| 输入方式 | 可传MP4路径/URL，也可传预抽取帧 | 官方允许外部抽帧，但外部抽帧的时间映射仍需应用保存。 |

以上来自[Google多模态教程的Working with Video部分](https://ai.google.dev/gemma/docs/embeddinggemma/multimodal-embeddinggemma-with-sentence-transformers#working_with_video)。教程中的16帧示例是一次调用的演示，不能视为通用最优配置，也不能用示例的单次相似度变化证明本项目收益。

Google模型卡还列出：所有模态共享8192 token；默认视频每帧约140 token，音频每秒约25 token；视觉预算可在70–1120间选择，预算增加会增加延迟和输入长度，以换取视觉细节表达。模型卡中的约58视频帧、约327秒音频是各自单模态的预算估算，不能同时当作音画联合输入的上限。[Google EmbeddingGemma 2模型卡](https://ai.google.dev/gemma/docs/embeddinggemma/model_card_2#context_limits)

按上述默认预算粗算，30秒视频以1 FPS取30帧并加入30秒音频，约为`30×140 + 30×25 = 4950` token，尚未计特殊标记等开销；这不构成30秒不可用的理论限制。它只是输入预算估算，不能据此断言本机吞吐、峰值内存、不会截断或检索质量已经通过。

## 目前未查到的统一规定

在上述教程、模型卡及[Google开发者指南](https://developers.googleblog.com/en/embeddinggemma-2-the-developer-guide/)中，未查到视频检索必须使用6秒、15秒或30秒窗口，也未查到每2/3/5秒一帧对所有视频最优的规定。片长、步长、采样率是不同选择，不能由默认1 FPS反推统一片长。

这些结论针对本项目使用的`google/embeddinggemma-2`。不借用`gemini-embedding-2`云接口或Gemma 4生成模型的视频限制。

## Google官方Android定位示例的实际配置

另核查[Google AI Edge Gallery的Video Moments Finder](https://developers.googleblog.com/google-ai-edge-with-embeddinggemma-2/)源码，固定到提交`8f5f2a0bdb7c81693384dbdecbabec9b1e8d7f4e`。这是一套移动端应用初始配置，不能等同于通用最优值，也不能把其运行时间移用于本机PyTorch/MPS服务。

| 示例预设 | 片段长度 | 每段帧数 | 相邻段重叠 | 带音轨 |
| --- | --- | --- | --- | --- |
| Object／物体 | 2秒 | 2帧 | 0秒 | 否 |
| Action／动作 | 4秒 | 4帧 | 0秒 | 否 |
| Speech／语音 | 6秒 | 6帧 | 0秒 | 是 |

预设和可调范围由[ProcessingConfigDialog.kt](https://github.com/google-ai-edge/gallery/blob/8f5f2a0bdb7c81693384dbdecbabec9b1e8d7f4e/Android/src/app/src/main/java/com/google/ai/edge/gallery/customtasks/videomomentfinder/ProcessingConfigDialog.kt#L49-L90)直接定义，段长滑块为1–10秒，且预设只是可继续调整的起点。[ViewModel的默认值](https://github.com/google-ai-edge/gallery/blob/8f5f2a0bdb7c81693384dbdecbabec9b1e8d7f4e/Android/src/app/src/main/java/com/google/ai/edge/gallery/customtasks/videomomentfinder/VideoMomentFinderViewModel.kt#L73-L77)为2秒、2帧、0重叠、启用音轨。

这里的“6秒6帧”不等于处理器的精确1 FPS：示例把多帧均匀安排在片段首尾（含两端），单帧取中点，并从Android检索最接近的关键帧。2秒2帧的计划时刻是片段两端；动作4秒4帧约间隔1.33秒；语音6秒6帧约间隔1.2秒。不能把这些预设写成Google要求每隔固定几秒取一帧。[采样实现](https://github.com/google-ai-edge/gallery/blob/8f5f2a0bdb7c81693384dbdecbabec9b1e8d7f4e/Android/src/app/src/main/java/com/google/ai/edge/gallery/customtasks/videomomentfinder/VideoMomentFinderViewModel.kt#L1246-L1267)、[取关键帧实现](https://github.com/google-ai-edge/gallery/blob/8f5f2a0bdb7c81693384dbdecbabec9b1e8d7f4e/Android/src/app/src/main/java/com/google/ai/edge/gallery/customtasks/videomomentfinder/VideoMomentFinderViewModel.kt#L1347)

示例把有声段的时间文字、音频小段和图片交错编码为一条窗口记录；起止毫秒保存在应用元数据中，定位不是由向量直接输出时间。[组合实现](https://github.com/google-ai-edge/gallery/blob/8f5f2a0bdb7c81693384dbdecbabec9b1e8d7f4e/Android/src/app/src/main/java/com/google/ai/edge/gallery/customtasks/videomomentfinder/AudioInterleaving.kt#L63-L91)、[窗口记录实现](https://github.com/google-ai-edge/gallery/blob/8f5f2a0bdb7c81693384dbdecbabec9b1e8d7f4e/Android/src/app/src/main/java/com/google/ai/edge/gallery/customtasks/videomomentfinder/VideoMomentFinderViewModel.kt#L1373-L1386)

它还把视觉预算设为70、运行输入上限设为1024。音频预算估算常数为每秒6.25 token，不能拿来覆盖模型卡的25 token/秒；样例对超预算或音频后端失败存在退回纯画面的逻辑，本项目要求保持音画联合身份及失败停住，不能直接照搬。[运行预算](https://github.com/google-ai-edge/gallery/blob/8f5f2a0bdb7c81693384dbdecbabec9b1e8d7f4e/Android/src/app/src/main/java/com/google/ai/edge/gallery/customtasks/videomomentfinder/VideoMomentFinderTask.kt#L129-L132)、[音频估算](https://github.com/google-ai-edge/gallery/blob/8f5f2a0bdb7c81693384dbdecbabec9b1e8d7f4e/Android/src/app/src/main/java/com/google/ai/edge/gallery/customtasks/videomomentfinder/AudioInterleaving.kt#L23-L33)

对本项目有价值的参考是：官方定位应用提供短段预设、按搜索目标选择是否带音轨、并减少每帧视觉预算。这不证明15/30秒必然更差，也不证明稀疏采样必然足够；其速度及短暂目标召回仍需本机固定样本实验。

## 对现有实现的映射

本机服务代码目前在`MediaDecoder.video`中按每秒一帧预抽取、最多32帧并均匀取样，再以`do_sample_frames=False`交给处理器；`Engine.PROCESSING.video`固定每帧140 token并关闭时间戳。因此在本项目里减少采样频率，需要调整实际抽帧链，不能只给后面的处理器传一个新FPS参数。此项为本机代码核查，不是Google官方推荐。

当前服务源码位置：`/Users/mac/Documents/projects/embedding-service/src/embedding_service/media.py`和同目录`engine.py`。本轮未编辑服务，也未运行新的媒体编码、模型推理或全库任务。

后续实验可分别比较片长、采样频率和每帧视觉预算，按固定问题检查速度、资源和短暂目标的命中；更少的帧可能漏过短暂画面，这是需要本项目验证的推断。15秒每2秒一帧只是候选实验配置，不能称为Google推荐。新的预处理及表示需要独立版本和校准，不能覆盖现有成功缓存或把旧验收结论直接搬过去。
