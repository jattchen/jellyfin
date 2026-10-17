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
```

`migrate` 不停 Docker：数据库按只读不可变方式复制，复制期间文件或 WAL 有变化就重试，并做完整性检查。迁移后服务只听 `127.0.0.1`，关闭局域网自动发现，`KnownProxies` 为 `127.0.0.1`（给 Caddy）。

首次启动会执行 10.11 → 12.x 的数据库迁移；按官方要求之后要全库扫描一次。

## 切换与回退

切换（第 4 步，待用户试用确认）：

1. `stop` 本机版；用 `migrate --port 8096` 从 Docker 最新数据重新迁移（试用期间本机版上的观看记录会被换下到 `replaced/`）。
2. 停 Docker 容器 `jellyfin`，`install` 启动本机版并全库扫描。
3. 核对后，Caddy 的 `jellyfin.home.jattora.com` 后端本来就是 `127.0.0.1:8096`，无须改动。

回退：`stop` 本机版，`docker start jellyfin`。Docker 配置目录在整个过程中只读。
