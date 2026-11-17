# QQ × DeepSeek 自托管机器人

在自己服务器上跑一个 QQ 机器人：群里 @ 它或私聊它，回答由 DeepSeek API 生成。

走 API 而不是官方 App，好处是**不受客户端免费额度限制**（API 按 token 计费，用多少付多少），
代价是要自己备一台机器和一份 API key。整个项目是一个 Python 文件 + 一份 systemd unit，
不需要数据库服务、不需要 Web 框架、不需要 Redis。

```
QQ 用户 ──@/私聊──> QQ 开放平台 ──WebSocket──> bot.py ──HTTPS──> DeepSeek API
                                                  │
                                          SQLite（上下文窗口 + 全量存档）
```

## 特性

- **群聊 @ 与私聊**：同一套逻辑，按用户 + 会话隔离记忆。
- **多轮记忆**：最近 N 轮进上下文，全部消息落 SQLite 存档；「记得多少」和「存了多少」解耦，
  上下文成本可预测。
- **联网搜索**：用 DeepSeek 服务端的 `web_search` 工具，模型自己判断什么时候查，普通闲聊不花这笔钱。
- **链接抓取**：用户直接甩链接，机器人自己取正文，不用对方贴全文。带 SSRF 防护：
  只允许 http/https、拦截回环/私网/link-local/云元数据地址（含 `127.1`、`2130706433` 这类整数写法）、
  **重定向逐跳复检**（httpx 默认会直接跟着 302 跳进内网）、DNS 解析结果也参与校验。
- **图片理解**：识别图片消息，走多模态模型。
- **引用上下文**：回复某条消息时能读到被引用的那条。
- **时间注入**：每轮告诉模型真实的北京时间，避免它把「今天」说错。
- **管理员指令**：`/stats` `/ping` `/recall` `/export` `/reset` `/wipe` 等，仅 `BOT_OWNER_IDS` 里的人可用。
- **健壮性**：消息去重、按会话加锁、失败后等 1.5 秒重试一次（重试时关掉 thinking 并放大 `max_tokens`，
  避免只拿到思考块）、长回复按 `|||` 拆成多条、按字符与字节双上限截断。

## 目录

```
bot.py                 主程序（单文件，约 1250 行）
prompt.example.md      人设模板，复制成 prompt.md 后自己改
.env.example           全部配置项，带注释
requirements.txt       qq-botpy + httpx
deploy/qq-bot.service  systemd unit 模板
```

## 前置条件

| 项 | 要求 |
|---|---|
| 服务器 | 任意 Linux，1 核 1G 足够；常驻，能主动出网（连接 QQ 与 DeepSeek 都是**出站**连接，不需要公网 IP、不需要开端口） |
| Python | 3.10+（用到 `str \| None` 与 `zoneinfo`） |
| QQ 机器人 | 在 [q.qq.com](https://q.qq.com) 创建，拿到 AppID / AppSecret |
| DeepSeek | [platform.deepseek.com](https://platform.deepseek.com) 充值并创建 API key |

> 出站即可这一点很关键：机器人用的是 WebSocket 长连接，**不需要域名、不需要证书、不需要公网 IP**，
> 家里的小主机、NAT 后面的机器都能跑。

## 一、拿到 QQ 侧凭据

官方文档在 [bot.q.qq.com/wiki](https://bot.q.qq.com/wiki/)，管理端在 [q.qq.com](https://q.qq.com)。

1. 用要当管理员的 QQ 登录，按引导创建机器人应用。**个人认证主体即可，不需要营业执照**，单主体上限 5 个。
2. **先认清一条硬限制**：个人主体**不能把机器人设为「公开使用」**，只能加到自己当群主/管理员的群
   （个人认证进群上限 2000 人）。想让任何人都能把它拉进自己的群，必须企业主体 + 发布审核。
   自己玩、给自己群用，个人认证完全够。
3. 进应用 → **开发设置**，记下 `AppID` 与 `AppSecret`（只显示一次，丢了要重置）。
   `Token` 字段**官方已弃用**，鉴权是 AppID + AppSecret 换 AccessToken，SDK 内部处理，你不用管。
4. **「沙箱应用」这个概念已经没有了**：新管理端里管理员账号直接可用，没有单独的沙箱环境。
   需要给非管理员内测，用「内部体验号码」（上限 20 个）。
5. **事件订阅方式选 WebSocket**（`wss://api.bot.qq.com/websocket/`），本项目就是这条路。
   Webhook 要求 HTTPS 且回调端口只能是 80/443/8080/8443，还要域名和证书，没必要。
6. 权限：群 @ 消息与单聊消息同属一个事件位 `GROUP_AND_C2C_EVENT`（`1<<25`），
   botpy 里写作 `botpy.Intents(public_messages=True)`，**两者无法分开订阅**。
   官方文档一边说这一位「需申请」，一边又说个人主体建号即可在自己群里 @ 使用，口径不一致；
   实际情况是个人主体加自己管理的群、群内 @ 就能用，若 WS 连上又被断开，再去管理端确认权限位。

## 二、拿到 DeepSeek key

`platform.deepseek.com` → API keys → 创建，形如 `sk-...`。注意 key 只在创建时显示一次。

本项目默认用 **Anthropic 兼容端点** `https://api.deepseek.com/anthropic/v1/messages`，
因为只有这个端点支持服务端 `web_search` 工具（OpenAI 兼容端点会拒绝非 `function` 类型的 tool）。

## 三、部署

> 不想手动敲这些命令？把文末「附录：让 AI 替你部署」里的提示词整段复制给 AI 编程助手，
> 它会按同样的步骤执行，遇到密钥会停下来问你。

```bash
# 1. 代码
sudo mkdir -p /opt/hub && sudo chown "$USER" /opt/hub
git clone https://github.com/WangYr-Peppa/qq-deepseek-bot.git /opt/hub
cd /opt/hub

# 2. 依赖
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt

# 3. 配置（.env 必须 chmod 600）
cp .env.example .env
chmod 600 .env
$EDITOR .env          # 至少填 QQ_APPID / QQ_APP_SECRET / DEEPSEEK_API_KEY / BOT_OWNER_IDS

# 4. 人设（可选）
cp prompt.example.md prompt.md && $EDITOR prompt.md

# 5. 数据目录
mkdir -p exports

# 6. 先手动跑一次，确认能连上
#    bot.py 自己不读 .env（只有 systemd 的 EnvironmentFile 会注入），
#    所以手动跑要先把 .env 导进环境，否则 import 阶段就 KeyError。
set -a; . ./.env; set +a
.venv/bin/python bot.py
```

> `BOT_OWNER_IDS` 别留空：默认 `BOT_PRIVATE_MODE=1` 时它是**白名单**，空着等于谁都不理。
> 先启动一次、私聊机器人发 `/id`（这个指令对所有人开放），把返回的 openid 填进去再重启。

看到 `connected` / 收到心跳就算通了。此时在自己的群里 @ 它一句试试，再 `Ctrl-C` 退出。

## 四、注册为系统服务

```bash
sudo cp deploy/qq-bot.service /etc/systemd/system/qq-bot.service
sudo sed -i "s/YOUR_USER/$USER/g" /etc/systemd/system/qq-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now qq-bot
journalctl -u qq-bot -f          # 看日志
```

`MemoryMax=400M` 是保守值，够 10 轮上下文的正常负载；如果 `/stats` 显示内存持续贴着上限，
先降 `BOT_MEMORY_TURNS`，再考虑把上限调到 600M。

## 五、常用配置

完整清单见 `.env.example`，这里只列最容易搞错的几个。

| 变量 | 默认 | 说明 |
|---|---|---|
| `BOT_OWNER_IDS` | 空 | 允许使用机器人的 QQ openid（**不是 QQ 号**，用 `/id` 查），逗号分隔 |
| `BOT_PRIVATE_MODE` | `1` | `1` = **白名单模式**：只有 `BOT_OWNER_IDS` 里的人会被回答，其他人直接丢弃；<br>`0` = 群里任何人的 @、任何人私聊都会被回答（受平台频控与内容审核约束） |
| `BOT_MAX_CHARS` | `1500` | 单条消息可见字数上限，超出按 `|||` 拆分或截断 |
| `BOT_MAX_TOKENS` | `4096` | 模型侧上限，**同时也覆盖 thinking token**，别调太小否则只回思考块 |
| `BOT_MEMORY_TURNS` | `10` | 进上下文的问答轮数；调大 = 更连贯但更贵 |
| `BOT_MEMORY_TTL` | `1800` | 闲置多少秒后该会话的记忆失效 |
| `BOT_WEB_SEARCH` | `1` | 服务端联网搜索开关 |
| `BOT_FETCH_PAGES` | `1` | 抓取用户粘贴的链接正文 |
| `BOT_DB_PATH` | `/opt/hub/memory.db` | SQLite 存档路径 |
| `BOT_PROMPT_FILE` | `/opt/hub/prompt.md` | 人设文件，改完**无需重启**（按 mtime 热加载） |

## 六、指令

除 `/id` 外，全部要求发送者在 `BOT_OWNER_IDS` 里（`/id` 故意对所有人开放，否则你没法查自己的 openid）。

| 指令 | 作用 |
|---|---|
| `/id` | 查自己的 openid 与会话键（**所有人可用**，填 `BOT_OWNER_IDS` 用） |
| `/help` | 列出全部指令 |
| `/stats` | 上下文轮数、存档行数、进程内存、运行时长 |
| `/ping` | 测一次 DeepSeek 往返延迟 |
| `/time` | 服务器时间与时区 |
| `/model` | 当前模型、人设字数、各项限额 |
| `/recall 关键词 [all]` | 检索存档；`all` = 跨会话 |
| `/export` | 导出本会话全量记录到服务器文件 |
| `/reset` | 清空上下文窗口（存档保留） |
| `/wipe yes` | 永久删除本会话记录 |
| `/split N` | 分条发送测试 |

中文触发词「清空对话 / 重置对话 / 重新开始」等价于 `/reset`。

## 七、踩过的坑

1. **API 域名漂移**：QQ 在 2026-08-10 把所有接口域名统一到 `api.bot.qq.com`（连鉴权也迁了），
   独立沙箱域名 `sandbox.api.sgroup.qq.com` 同时废弃；而 PyPI 上最新的 `qq-botpy` 仍是 1.2.1（2024-03 发布），
   硬编码旧域名 `api.sgroup.qq.com`。`bot.py` 在导入时改写 `Route.DOMAIN`，
   **不要删掉那段 patch**，否则会连旧域名。
2. **被动回复是真的短**：群聊被动消息 **5 分钟内、每条最多回 5 次**；单聊被动 **60 分钟、最多 4 次**；
   同一个 `msg_id + msg_seq` 重复发送必失败，`msg_seq` 要递增（SDK 内部处理，自己拼 HTTP 时要注意）。
   频控也比想象紧：认证 Bot 60 qpm、未认证 30 qpm、单用户关系 20 qpm、**每群每天最多接收 1000 条**。
   程序里的 `BOT_MAX_PARTS` 就是为「一条消息最多拆几条」留的余量，别调到 5 以上。
3. **`max_tokens` 是包含思考的**：把它设成和可见回复等长，会得到一条只有思考块的回复。
   正确做法是给足 `BOT_MAX_TOKENS`，在发送前按 `BOT_MAX_CHARS` / `BOT_MAX_BYTES` 截断。
4. **单条长度没有官方数字**：文档只给了错误码 `40054007 消息长度超限`，没写字符数；
   社区普遍按 2000 字符切分（未证实）。另有一个 `40054010 不允许发送URL`，
   所以别让模型把裸链接当正文发出去。本项目按「字符 + 字节」双上限截断，中文字符占 3 字节，两个值要一起看。
5. **内容安全是硬要求**：运营规范第 8 条写「**应**设置过滤违法、违规信息的机制」。
   后果很直接——下发一次违规消息，会被取消「被动消息免语料审核」资格，之后每条都要人审（24 小时）。
   自己群自用风险低，但仍建议在人设/代码里加一层关键词过滤。
6. **私聊要单独权限**：没开通私聊能力时，私聊消息根本不会推过来，不是代码问题。
7. **`.env` 权限**：`chmod 600`，并且永远不要提交。仓库里的 `.env.example` 只有变量名。
8. **SQLite 的 `-wal` / `-shm`**：备份时要一起拷，或先 `sqlite3 memory.db ".backup ..."`。
9. **SDK 版本偏旧**：1.2.1 早于 2026 年几批 v2 字段变更，遇到「字段缺失 / 事件解析不到」
   先怀疑 SDK，再用原始 payload（`BOT_DEBUG_PAYLOAD=1`）确认服务端到底发了什么。
10. **换目录部署要改三处**：代码里的默认路径是 `/opt/hub/*`，如果你装在别处，
   除了改 systemd unit 的 `WorkingDirectory`/`ExecStart`，还要在 `.env` 里改
   `BOT_DB_PATH` / `BOT_EXPORT_DIR` / `BOT_PROMPT_FILE`，否则 SQLite 会去打不开的旧路径。
11. **`Asia/Shanghai` 需要 tzdata**：精简镜像（`python:*-slim`、Alpine）没有时区库，
   `ZoneInfo("Asia/Shanghai")` 会在 import 阶段抛 `ZoneInfoNotFoundError`。
   Ubuntu/Debian 正常安装不受影响；容器里补 `apt install tzdata` 或 pip 装 `tzdata`。
12. **`MemoryMax` 撞墙会循环重启**：`Restart=always` + cgroup 内存超限 = 每 5 秒被杀一次。
   看 `journalctl -u qq-bot | grep -i kill` 确认，然后降 `BOT_MEMORY_TURNS` 或调高上限。

## 八、成本

按 token 计费，当前 `deepseek-flash` 大致单价（以官方定价页为准）：输入 $0.15–0.3 / 百万 token（闲/峰时），
输出 $0.6–1.2 / 百万 token，服务端联网搜索 $0.75 / 千次。

量级参考：一个人一天 50 轮、每轮 500 token 上下文 + 300 token 输出，一天约 4 万 token，
成本是**几分钱**级别；真正把账拉高的是三件事：超长上下文、图片、联网搜索。
想压成本就降 `BOT_MEMORY_TURNS`、关 `BOT_WEB_SEARCH`、关 `BOT_FETCH_PAGES`。

具体单价与模型名以 [DeepSeek 定价页](https://api-docs.deepseek.com/quick_start/pricing) 为准
（`deepseek-flash` / `deepseek-v4-pro`；旧名 `deepseek-v4-flash` 仍会被重定向到 flash）。

## 九、隐私与合规

- 消息**全量落库**在你自己机器上的 SQLite 里（`BOT_DB_PATH`），可用 `/wipe` 删除、`BOT_ARCHIVE_DAYS` 自动过期。
- 内容会发送给 DeepSeek API 用于生成回复，这与其他任何调用 API 的客户端一样。
- 放进别人的群之前，**先告知群成员**机器人在记录消息；不同地区对聊天记录留存的要求不同。
- 遵守 [QQ 开放平台开发者规范](https://q.qq.com/wiki/)：不要做主动群发、营销推送、绕过内容审核。

## 附录：让 AI 替你部署

适用 Claude Code、OpenCode、Cursor、Codex 这类能在这台服务器上执行 shell 的助手。
如果你的 AI 连不上服务器，它也照样有用——提示词里已经写明让它只输出命令清单，你自己贴。

把下面整段复制过去，**先把第 1 行的仓库地址和"哪台机器"说清楚**：

```text
我要在这台机器上部署这个 QQ 机器人：https://github.com/WangYr-Peppa/qq-deepseek-bot
如果你没有这台机器的 shell 权限，不要假装执行，把完整命令清单给我，我自己贴。

部署完成的效果：我在 QQ 群里 @ 机器人，它能回答我。

硬性规则：
1. 每一步先说要做什么、为什么，再执行；涉及删除、覆盖、重启的操作先问我确认。
2. 密钥只能由我提供，不许编造、不许猜、不许写进任何被 git 管理的文件。
   缺 QQ_APPID / QQ_APP_SECRET / DEEPSEEK_API_KEY 时停下来问我，我贴给你。
3. 不要修改仓库里的代码；临时脚本放 /tmp，结束后删掉。
4. 哪一步失败就停下，把完整报错贴给我，不要自己绕过（例如偷偷去掉 systemd 的 MemoryMax）。
5. 不要开放任何入站端口，也不要在输出里回显完整密钥（打印时打码）。

按这个顺序做：
A. 体检：python3 --version（必须 ≥3.10）、git --version、df -h /opt、sudo -n true（确认免密 sudo）、
   curl -sS -o /dev/null -w '%{http_code}' https://api.deepseek.com，
   以及 curl -sS -o /dev/null -w '%{http_code}' https://api.bot.qq.com（确认出网）。任一不满足就报告并停止。
B. 代码：mkdir -p /opt/hub 并确保当前用户拥有它；/opt/hub 非空时先问我能不能清空。
   git clone https://github.com/WangYr-Peppa/qq-deepseek-bot.git /opt/hub
C. 依赖：python3 -m venv /opt/hub/.venv && /opt/hub/.venv/bin/pip install -q -r /opt/hub/requirements.txt
D. 配置：cp /opt/hub/.env.example /opt/hub/.env && chmod 600 /opt/hub/.env
   逐个问我 QQ_APPID、QQ_APP_SECRET、DEEPSEEK_API_KEY。如果我还没有 AppID/AppSecret，
   先告诉我去 https://q.qq.com 创建机器人、在"开发设置"里取值，取完再继续。
   BOT_OWNER_IDS 先留空，部署完再用 /id 拿 openid 填。
E. 人设与数据目录：cp /opt/hub/prompt.example.md /opt/hub/prompt.md && mkdir -p /opt/hub/exports
F. 手动试跑一次，确认同时连得上 QQ 和 DeepSeek：
   cd /opt/hub && set -a && . ./.env && set +a && timeout 25 .venv/bin/python bot.py
   日志里出现"启动成功"和"心跳维持启动"就算通过；退出码 124（被 timeout 杀掉）是正常的。
   这一步报错就先修，不要继续装服务。
G. 装服务：cp /opt/hub/deploy/qq-bot.service /etc/systemd/system/ &&
   sed -i "s/YOUR_USER/$USER/" /etc/systemd/system/qq-bot.service &&
   systemctl daemon-reload && systemctl enable --now qq-bot
H. 验收：systemctl is-active qq-bot 返回 active；journalctl -u qq-bot -n 30 里能看到"启动成功"，
   且没有 Traceback / ERROR。然后把这句话原样告诉我：
   "去群里 @ 我一句，再私聊我发 /id（白名单模式下这个指令对所有人开放），
    把返回的 openid 填进 /opt/hub/.env 的 BOT_OWNER_IDS，然后 systemctl restart qq-bot，
    否则机器人会谁都不理。"
I. 收尾：删掉 /tmp 下的临时文件；把 /opt/hub/.env 里的配置项列给我看，密钥位置打码。

最后给我一份 5 行以内的总结：装在哪、服务名、日志怎么看、下次怎么更新、还有什么需要我人工做。
```

两个提醒：

- **它跑完不等于能用**。最后一步的"@ 一句 + 发 `/id` + 填 `BOT_OWNER_IDS`"必须你自己做，
  那是唯一没法自动化的环节。
- 别让 AI 顺手帮你「优化」代码或改成 Docker 部署——它改的东西你看不见，出问题也无从对账。
  真要改，让它先给 diff。

## License

MIT，见 `LICENSE`。
