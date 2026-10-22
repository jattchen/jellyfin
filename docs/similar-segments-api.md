# 声画相似片段

`POST EmbeddingVideoSearch/v1/similar` 从一条已经返回的候选出发，只比较当前索引里的声画联合向量。不调用嵌入模型，也不把文字向量当作查询。分数是原始余弦，`qualityPolicy.scoreMeaning` 为 `documentCosine`，不是 `referenceTailR`。

实现之后，`capabilities.features` 才包含 `similar-segments-v1`。文字搜索的排序和相关性门槛不变。

请求字段是 `segmentId`、`expectedBindingVersion`、`parentId`、`limit`、`groupByVideo`、`collapseRuns`、`clientQueryId`。`collapseRuns` 缺省为 `false`。条数上限与原来的搜索相同。

点击文字候选时，只在同一影片、同一媒体源、同一内容版本里选择重叠最大的声画窗。重叠相同则优先包含对白中点的窗，再取更早的起点，最后取稳定的 segmentId。没有重叠时返回 409 `referenceUnavailable`。

排除范围是参照区间和实际声画窗的并集。同一来源里与这个并集重叠或端点相接的窗都排除，包括参照自身。同一部影片里不相接的远段可以返回。其他影片不因为时间数字相同而被排除。

`reference` 带回点击的那条：`segmentId`、`itemId`、`mediaSourceId`、起止、`representation`、`bindingVersion`。`reference.source` 是实际使用的声画窗，`representation` 固定为 `jointAudioVideo`。`reference.excluded` 是被排除的并集。绑定戳是 64 位小写十六进制。

每条结果沿用原来的候选形状：`quality` 为 `candidate`，`representation` 为 `jointAudioVideo`，并带 `candidateId`、`bindingVersion`。`score` 和 `rawScore` 都是原始余弦。余弦小于 0 的窗不进入这一页，否则现有网页会把整页当成读失败；其余窗仍按余弦从高到低排列。

网页在看到 `collapsed-runs-v1` 和 `similar-segments-v1` 后，相似请求固定发送 `groupByVideo: true`、`limit: 3`、`collapseRuns: true`。这时先取最多 3 部、每部 5 条，再只折叠这一页，每条结果都有 `run` 和 `similarAvailable`。能力接口的 `scoreMeaning` 仍是 `referenceTailR`。向量长度不对的快照整份返回 409 `incompatible`，不会调用模型。

源和每个候选都要能通过当前权限、源事实、内容版本和签名。排序前查一遍，排序后再读一份新的用户、资料库范围和快照，另查一遍。每一遍里，同一影片、媒体源、资料库和内容版本只读一次；这两遍不共用结果，下一次请求也不沿用。两遍都仍读取整份快照。查完失效目标后才按原来的条数取页，因此刚被收回的目标会空出位置，由仍合法的下一条补上。索引是否完整只对这一页里的来源各查一次，然后再折叠。取消之后不会开始下一个来源，也不会开始第二份快照。已经开始的快照解码和数据库锁会把这一次做完，不能从半路掐断。看不到或不存在的片段都是 404 `notFound`，正文不说明它是否存在。绑定不一致是 409 `bindingMismatch`，内容已变是 409 `stale`，签名或索引不能用是 409 `incompatible`。缺字段或条数不合法是 400 `invalidRequest`。

`collapseRuns: true` 时，先按原来的页取出结果，再只折叠这一页。页外的低分窗不能把一组接长。
