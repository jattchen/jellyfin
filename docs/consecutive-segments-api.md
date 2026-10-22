# 连续片段归组

搜索响应保持原来的候选形状。`collapseRuns` 缺省为 `false`，旧请求的结果、分数和 `referenceTailR` 不变，也不多出 `run` 或 `similarAvailable`。

`capabilities.features` 在实现本能力后包含 `collapsed-runs-v1`。空的契约对象仍只带原来的 `grouped-video-v1`，避免未走搜索运行时的旧检查被提前改写。

`collapseRuns: true` 时，先按原规则得到即将返回的那一页：按片分组时最多 3 部、每部 5 条；不分组时仍是原来的条数上限。只在这一页里归组，不用页外的低分候选把一段接长，也不为了补回被折叠掉的名额再去取后面的候选。

同一部影片、同一个媒体源、同一内容版本，时间重叠或端点相接的候选连成一组，并沿这页里的接触关系传递。代表条是这一页里搜索排名最高的那条，自己的起止时间和 transcript 不改。`run.startTicks` 与 `run.endTicks` 是组内区间的并，`run.memberCount` 是这一页里进入该组的条数。代表条之间仍按原来的相对排名排列。

`similarAvailable` 只在折叠后的代表条上出现。已有声画联合候选，或同一影片、媒体源和内容版本上确有重叠的声画窗时为 `true`。没有导入候选的旧窗口搜索为 `false`。它不表示文字向量可以当作声画参照。
