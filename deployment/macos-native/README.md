# 本机 Jellyfin 12.2（macOS 原生）

替换 Docker 里的 Jellyfin 10.11.11，装上 `plugins/EmbeddingVideoSearch` 和 jattchen/jellyfin-web 同名分支的网页构建。跟踪：jattchen/jellyfin#2。

## 为什么要 `/media` 和 `/config`

Docker 版数据库里存的是容器内路径：视频在 `/media/...`，媒体库根目录和海报在 `/config/...`。Jellyfin 的条目编号由路径算出，路径一变就会被当成新条目，观看记录对不上。所以本机版保持这两个路径不变：

- `/etc/synthetic.d/jellyfin-native` 在系统根目录建 `/media`、`/config` 两个链接，分别指向 `~/.local/share/jellyfin-native/paths/{media,config}`。
- 这两个是普通链接，分别指向 `/Volumes/Nas/影视库` 和 `/Volumes/Turbo/Applications/JellyfinNative/data`。以后换位置只改普通链接，不用管理员密码，也不用重启。

`/cache` 只出现在配置文件里，迁移时直接改成本机路径。

## 目录

`/Volumes/Turbo/Applications/JellyfinNative/`：

| 目录 | 内容 |
| --- | --- |
| `server/12.2` | 官方 macOS arm64 安装包解压，SHA256 已核对 |
| `ffmpeg/8.1.3-1` | jellyfin-ffmpeg portable macarm64，来自 GitHub release，SHA256 已核对 |
| `web/web-ae9762d97701` | 网页分支构建快照（与 jattchen/jellyfin-web `1908fda` 代码一致） |
| `data` | 本机版数据（即 `/config`），由 Docker 配置复制并升级 |
| `cache` | 缓存和转码临时目录 |
| `query-service` | 三集索引匹配的查询模型代码快照、独立配置和查询证据；只听 18783 |
| `plugin-releases/<DLL哈希>` | 已验证插件产物；迁移从这里复制，不改历史验收 DLL |
| `backups/docker-config-<时间>` | 每次迁移前 Docker 配置的完整备份（原件不动） |
| `replaced/data-<时间>` | 重新迁移时被换下的旧本机数据，不自动删除 |

开机自启：`~/Applications/Jellyfin Server — Video Search.app` + `~/Library/LaunchAgents/com.jattora.jellyfin-native.plist`。启动外壳等 Turbo、`/config`、`/media` 都可用后才启动 Jellyfin，避免 NAS 未挂载时扫描把视频当成删除。等待原因写在 `~/Library/Logs/jellyfin-native/launcher.log`。

## 命令

```bash
cd deployment/macos-native
/usr/bin/python3 jellyfin_native.py stage            # 准备程序文件（可重复）
/usr/bin/python3 jellyfin_native.py migrate --port 8097   # 备份 Docker 配置并生成本机数据，须先 stop
/usr/bin/python3 jellyfin_native.py links            # 建 /media、/config（会弹管理员密码框）
/usr/bin/python3 jellyfin_native.py install          # 装开机自启并启动
/usr/bin/python3 jellyfin_native.py status|start|stop|restart|uninstall
/usr/bin/python3 query_model.py install|status|stop   # 独立查询模型，保留共享 18780
```

`migrate` 不停 Docker：数据库按只读不可变方式复制，复制期间文件或 WAL 有变化就重试，并做完整性检查。迁移后服务只听 `127.0.0.1`，关闭局域网自动发现，`KnownProxies` 为 `127.0.0.1`（给 Caddy）。

首次启动会执行 10.11 → 12.x 的数据库迁移；按官方要求之后要全库扫描一次。

## 三集索引复用

全库后台索引仍留待 issue #3。本次只复用已验收的 510 个联合窗、481 条文字候选，不重新编码，不启用自动建索引。

验收库使用的条目编号不同于日常库，不能直接把数据库复制过去就称搜索可用。`rebind_index.py` 接受三条私有映射，核对当前宿主 `admin/source-facts` 提供的条目、媒体源、库、内容版本、时长及音轨事实。映射另含 `sourceItemId`、`sourcePath`、`sizeBytes`、`modifiedTicks`。只有目标与验收来源是同一个文件、内容版本与时长一致时才生成新副本；向量、对白、原始来源和排序曲线均保留。来源索引必须已停机、没有待处理 WAL，且完全不写来源文件。

```bash
/usr/bin/python3 rebind_index.py --source /绝对路径/验收索引/index.db \
  --mapping /私有目录/mapping.json --output /私有目录/prepared-index
/usr/bin/python3 jellyfin_native.py stop
/usr/bin/python3 jellyfin_native.py install-index --prepared-index /私有目录/prepared-index
/usr/bin/python3 jellyfin_native.py start
```

输出含 `index.db`、映射及哈希证据。`install-index` 必须停机执行，并再次核对本机条目编号和时长；旧索引整目录保留在 `replaced/index-<时间>`。查询只启用动漫库，`manual_pause=1`、`imported_candidates_only=1` 保持不变。

查询模型使用独立命名 App `Jellyfin Search Model.app` 和 LaunchAgent `com.jattora.jellyfin-query-model`。`query_model.py install` 校验冻结源代码及查询记录外壳后复制到本机安装目录，再接管**身份已核对且空闲的原手动验收进程**。未知占用者拒绝接管。新的进程须报告 `processing_id=eg2-0b01d27c8c20b7d920e5`；不能改接身份不同的共享 18780。依赖现有 embedding-service 的 Python 环境和固定模型文件；不修改共享服务配置。模型日志在 `~/Library/Logs/jellyfin-native/query-model.log`。

本机使用的插件另修复了可信反向代理后的播放回连：外部 HTTPS 会改写 `Request.Scheme`，但 8097 实际只听 HTTP。内部回连从当前连接的 TLS feature 判断实际协议，地址仍固定为 `127.0.0.1` 和当前监听端口，不依赖外部 Host。`stage` 将匹配 `PLUGIN_SHA256` 的构建产物复制到 `plugin-releases/`；历史验收目录不变。

macOS 可能要求首次允许新 App 访问外接磁盘；解锁电脑并在真实系统提示中允许后才可继续验证。LaunchAgent 的 `running` 状态不代表 HTTP 已就绪。分别核对 8097 的 `/System/Info/Public`、18783 的 `/readyz` 和 `/v1/models`，再用浏览器验收；整机重启后的顺序仍须另行观察。

## 切换与回退

切换（第 4 步，待用户试用确认）：

1. 用户先通过与日常入口分开的临时 HTTPS 网址试用 8097，明确验收通过后才能切换。三集搜索、缩略图和定位播放须可用；其他视频仍按名称搜索。
2. `stop` 本机版，并 `docker stop jellyfin`，让 Docker 数据库静止且释放 8096。用 `migrate --port 8096` 重新迁移最新数据；准备和检查在临时目录完成后才替换 `data`。试用期间本机版的观看记录保留在 `replaced/`，不自动与 Docker 合并。
3. 用上面已验证的 `prepared-index` 再执行 `install-index`。若最新数据与映射不一致，会拒绝安装，必须先核对，不能跳过校验。
4. `start` 本机版，等待数据库升级、全库扫描，再核对账号、条目和观看记录以及三集搜索、普通播放、转码播放。不要用 `install` 代替无变更的正常启动，以免无故重编译 App。
5. Caddy 的 `jellyfin.home.jattora.com` 后端本来就是 `127.0.0.1:8096`，无须改动。通过日常 HTTPS 入口再次验证后，撤下临时入口；Docker 配置和旧验收数据均保留。

回退：`stop` 本机版，`docker start jellyfin`，核对 8096 和日常入口恢复旧版。若本轮不再试用三集搜索，可另行停止独立查询模型；共享 18780 不动。迁移脚本只读 Docker 配置，不写入源目录。
