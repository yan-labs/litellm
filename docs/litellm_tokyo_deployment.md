# LiteLLM Tokyo 部署方式手册

## 2026-10-10 H1 生产迁移

生产运行位置调整为 H1 `i-07da62c91771f115c`，内部地址
`http://172.31.93.31:4000`，容器 `litellm`，配置目录 `/opt/litellm`。
旧机 `i-04ba3dd7ee265426f` 保留 TEST `litellm-test`（4001）及截图服务/Caddy；
确认这些用途仍在前不得停机、删除数据卷或释放 EIP。

`llm.kollab.im` 的 DNS、TLS 和 Caddy 仍位于旧机，只将生产 upstream 改为
`172.31.93.31:4000`。TEST upstream 保持原值。H1 4000 只供内部调用；
不得为本次迁移修改 ALB 或监听器规则。

## 发布入口与配置

发布分支为 `openrouter-fix-20260326`，workflow 为
`.github/workflows/deploy-tokyo.yml`。生产始终通过显式
`workflow_dispatch` 选择 `environment=prod`；普通 push 只面向 TEST。
纯发布控制文件与本文档变更由 `scripts/classify_multimedia_push.py` 跳过自动部署，
不会因这次适配触发 TEST 重启。

仓库变量：

- `AWS_REGION`：`ap-northeast-1`。
- `TOKYO_LITELLM_EC2_ID`：TEST 仍使用旧机 `i-04ba3dd7ee265426f`。
- `TOKYO_LITELLM_PROD_EC2_ID`：生产 H1 `i-07da62c91771f115c`，未设置时也使用此默认值。

`INSTANCE_ID` 统一按环境解析，所有 SSM 动作（包括 `health-check` 与
`dispatch_multimedia_config.py`）都读取同一个值。专用 GHA IAM user 的
`ssm:SendCommand` 权限须精确覆盖这两台实例；H1 instance role 须有现有
`kollab-litellm-build-20260415174047-13269` 的 `artifacts/` 前缀读取/清理权限。

生产沿用 `config/litellm.prod.yaml`，TEST 沿用 `config/litellm.test.yaml`；
构建替换继续从现有容器复用 env、端口、volume、network 和启动参数，
不在仓库保存或输出秘密。初次迁移须与旧生产使用同镜像、同配置、既有秘密；
数据库连接不变，受管 `REDIS_URL` 不变，生产 Redis DB15、TEST DB14 与业务 DB0
隔离不变。

生产 `build-and-restart` 明确指定 `--memory 2g --memory-swap 2g`
和 `--restart unless-stopped`；TEST 保留原有内存行为及重启策略。
现有发布模式及其事务/失败处理保持原样，本次只适配目标实例与资源上限。

## 迁移验收与回切

按容量闸门通过 → 新副本内部验证 → 本机后台访问 → 双活切流 →
30 分钟观察 → 旧机用途核查执行。新副本先验证 readiness、模型列表与
Kollab Server→LiteLLM Auto 最小真实路由；后台须通过
`scripts/debug/litellm-admin.sh` SSM 转发到本机 14000，返回 200 并能登录 UI。

本次迁移不自动回退镜像/配置；故障按已保存原值和现场证据前进修复。
切流前保存生产调用地址/Caddy upstream 原值并准备一条命令回切旧机；
切流后每 5 分钟读取一次 server 错误与延迟、`/proc/meminfo` 和各容器内存，
观察共 30 分钟。如 H1 `MemAvailable` 低于 6GiB，立即回切旧机并记录。
CPU、内存和服务告警须覆盖 H1 新位置；旧机仍有 TEST/截图/Caddy 用途，
其原有告警保留。实际迁移、回切命令与生产读回以
`~/.agent-reports/2026-10-10/litellm-h1-1011.md` 为准。

---

以下既有部署记录保留供历史参考；主机位置及访问路径以本节为准。

这份文档记录当前 Tokyo 环境下 LiteLLM 的实际部署方式，给后续 agent / 运维同学使用。

当前维护约定：

- Tokyo 这套私有部署当前继续在分支 `openrouter-fix-20260326` 上提交和部署
- `main` 不再承载这套 Tokyo 私有部署说明和 OpenRouter 定制修复
- 后续如果要改线上镜像、部署方式、VPN / DNS / Caddy / 告警，默认都从 `openrouter-fix-20260326` 分支继续演进

## 受限多媒体配置发布

`sync-multimedia-only` 只给当前容器补齐 `gpt-image-2`、`openrouter-tts/*`、
`openrouter-stt/*`、`gemini-omni` 四个已批准托管模型。Omni 完整配置对齐 TEST，
使用现有 Vertex 命名凭据和 `global` 区域，其余模型复用 `openrouter-toyo`。不读取、创建、
替换 provider 凭据，不查询或改写 virtual key，不执行任何模型请求或路由收敛脚本。
配置存在不代表当前调用身份已获授权，更不能证明一次 401 已修复。
受限模式不运行 key 收敛；后续 Kollab 发布须执行已有 allowlist 同步并回读 `gemini-omni`
与 `seedance-2.5` 精确授权。常规网关 key 收敛清单在 TEST/PROD 均包含 Omni。
生产共用单个网关容器，任何配置重启都会影响其它模型的在途请求。

### 发布前和自动 push 安全门

1. 在独立工作区运行 `python3 -m unittest scripts/test_multimedia_config_release.py`，
   并完成独立 review。该测试完全离线，覆盖配置漂移拒绝、回滚和 push 事件分类。
2. 核对待推送范围。只有本节相关发布控制、生产配置、文档、离线测试改动时，
   `guard-branch` 会将整个自动 TEST deploy job 跳过，且不获取 AWS 发布身份。
   必须回读 job 为 skipped 后显式 dispatch；不要先触发旧收敛，再取消补救。
   同批包含普通 runtime / TEST 配置等其他文件时，原来的自动发布合同仍然有效，
   因此这批受限补丁不得与无关发布混推。无法证明 push 基准时直接拒绝。
3. 确认目标容器已经具备这四个模型的实现及正式托管 binding。这个模式不更新镜像，
   不读取 credential 记录，不修复权限。已有同名动态 DB deployment 若产生重复，
   发布后只读 `/model/info` 检查会失败并回滚，不会删除 DB 行。
4. 每次 dispatch 前回读同环境最近 workflow / SSM，确保没有遗留运行。
   新模式使用现有 workflow 的环境串行锁，同时持有远端专用非阻塞文件锁。
   手工/其他发布入口必须避免同时写目标配置；检测到其他写入则拒绝覆盖。
5. 受限模式必须填写 `expected_sha`，值为已审查的完整 40 位 commit SHA。
   workflow 在获取 AWS 身份之前比较 checkout HEAD，dispatcher 在 SSM 前再次比较；
   缺失、短 SHA 或不匹配都拒绝。其它发布模式不要求此字段，原合同不变。

### 正式顺序

从 `openrouter-fix-20260326` 先显式 TEST dispatch：

```bash
gh workflow run deploy-tokyo.yml --repo KollabTeam/litellm \
  --ref openrouter-fix-20260326 -f environment=test -f action_mode=sync-multimedia-only \
  -f expected_sha=<已审查的完整40位SHA>
```

TEST 已有四项正确配置时是只读 no-op，不重启。若线上配置与仓库存在其他语义差异，
严格拒绝，不把 TEST 当成额外配置同步机会。测试完成且获准生产后，使用同一命令把
`environment` 改为 `prod`，`expected_sha` 必须保持 TEST 已验收的同一 SHA。
如果分支已经前进导致 HEAD 不匹配，不能删掉/改写 SHA 绕过；应重新审查并从 TEST 开始。
不得为通关改选普通 `sync-and-restart` 或 `auto`。

### 原子更新、回滚与验收边界

- 先比较当前 `config.yaml` 与候选 YAML；只允许新增缺失的四个精确配置。
  其他路由、顺序或全局设置变化、重复 YAML key、目标已有漂移、额外凭据字段均拒绝。
- 首次改写前，独占创建 `config.yaml.bak.multimedia-<run>` 及对应
  `config.yaml.receipt.multimedia-<run>.json`，权限 `0600`；receipt 只记录发布 run、
  完整 SHA、目标、备份/候选摘要和恢复阶段，不含配置或凭据。保留原配置 owner/mode，
  用同目录原子替换并重启现有容器，不重建环境变量。
- receipt 在配置 rename 前已持久化。即使进程被 SIGKILL，回读原 SSM 已终止后，
  **重跑原 workflow run**（不要新 dispatch）即可恢复相同发布身份；attempt 增长不会换身份。
  只接受同 SHA/目标及精确备份/候选；当前配置必须仍是其中之一，否则拒绝覆盖。
  发布中断继续重启和验收；回滚中断只继续恢复旧配置。恢复无需先在可能已停止的容器中
  再次校验 YAML，靠改写前已验证并持久化的精确摘要锚定范围。
  缺失/损坏 receipt 或备份、摘要不一致返回失败，不能猜测归属；备份与 receipt 始终保留。
- 环境内另持久化 `.multimedia-config.owner.json`，只记录当前发布 run/SHA。
  未结束的发布必须由原 run 恢复，新 run 即使候选内容完全相同也不能接管。
  新发布只能接替明确终态的 owner；旧 run 看到 owner 已变化即拒绝，不得覆盖后来发布。
  `ready` / `rolled_back` 终态重跑严格只读，不能再次重启或回滚；若当前不健康，
  返回 `terminal_release_not_ready`，交由独立故障恢复操作处理，不把旧发布复活。
- 仅在容器内部使用已有管理身份读 `/health/readiness` 和 `/model/info`，不输出身份值。
  四个目标别名必须各出现一次；不读 `/key/*`、`/credentials`，不发模型探测。
- readiness 或 alias 检查失败时，仅当配置仍是本次候选才恢复备份并重启。
  `candidate_failed_rollback_ready` 表示发布失败但旧配置已恢复可就绪；
  `rollback_readiness_failed` 需运维恢复；`rollback_refused_foreign_config` 表示检测到并发写入，
  保留备份且禁止覆盖他人配置。不要盲目重跑。
- workflow summary 保留 SSM command ID。状态不明时不取消、不重发，先回读该命令终态；
  远程事务包含自己的恢复逻辑，取消可能中断回滚。输出只保留固定状态码，不倾倒原始日志。
- 部署成功后仍必须由 Kollab 正式服务链路做真实图片、TTS/STT 和 Omni 视频验收，核对可打开的产物与账单。
  若仍 401/403，只能进一步做脱敏身份边界诊断；若确需修改正式 service virtual-key grants，
  交给平台授权流程，任务侧不得获取/替换 key、运行权限 reconciler 或绕到 provider 直连。

## 分支与部署规则

为了避免后续 agent 把分支和环境搞混，当前固定采用下面这条规则：

| 分支 | 是否允许部署到 test | 是否允许部署到 prod | 备注 |
|---|---|---|---|
| `openrouter-fix-20260326` | 是 | 是 | 当前发布分支。测试环境先验证，验证通过后再发生产。 |
| `main` | 否 | 否 | 当前不作为 Tokyo 私有环境的部署来源。不要直接从 `main` 发 test 或 prod。 |

当前实际运行状态：

- test 当前运行镜像：`litellm-openrouter-fix-test:db815b27e4`
- prod 当前运行镜像：`litellm-openrouter-fix:20260326`

当前建议的发布顺序：

1. 所有新 patch 先合到 `openrouter-fix-20260326`
2. 先部署到 test：`litellm-test` / `llm-test.kollab.im`
3. 验证通过后，再部署到 prod：`litellm` / `llm.kollab.im`

如果后续 agent 看到 `main` 上有更新，不要默认把它发到 Tokyo 环境，除非维护约定被明确改掉。

适用范围：

- AWS 区域：`ap-northeast-1`
- AWS 账号：`766691179306`
- 目标服务：`llm.kollab.im`

这不是 LiteLLM 官方推荐的通用 Helm / Kubernetes 部署文档，而是**我们当前线上实际在用的 EC2 + Docker + RDS + Redis + Caddy + VPN** 方案。

## 架构概览

当前部署架构如下：

```text
员工机器 / Tokyo 内网服务
  -> OpenVPN（堡垒机，可选）
  -> DNS: llm.kollab.im -> 172.31.44.132
  -> Caddy on EC2 :443
  -> LiteLLM container on EC2 :4000
  -> RDS PostgreSQL
  -> ElastiCache Redis
```

当前测试环境采用低成本方案 A：

```text
同一台 LiteLLM EC2
  -> production: litellm -> :4000 -> llm.kollab.im
  -> test: litellm-test -> :4001 -> llm-test.kollab.im
  -> shared RDS instance, separate databases
  -> local redis-test container for test
```

核心原则：

- LiteLLM 跑在单台 EC2 上，不在 K8s 里
- 数据库和 Redis 用 AWS 托管服务
- 对外统一入口是 `https://llm.kollab.im`
- LiteLLM 本体监听 `4000`，由同机 `Caddy` 反代到 `443`
- 当前访问模型是“**内网访问 + VPN 访问**”，不是公网直接暴露

## 当前资源清单

### 计算与入口

- LiteLLM EC2：`i-04ba3dd7ee265426f`
- 实例名：`litellm-tokyo-ec2`
- 实例规格：`t3.xlarge`
- 私网 IP：`172.31.44.132`
- 所在 VPC：`vpc-04fa7231cc5c88f9b`
- LiteLLM 安全组：`sg-02b1f2f596a7c8450`

### 数据层

- LiteLLM PostgreSQL：`litellm-tokyo-pg`
- RDS endpoint：`litellm-tokyo-pg.cfsa2m2uagaa.ap-northeast-1.rds.amazonaws.com:5432`
- 生产数据库：`litellm`
- 测试数据库：`litellm_test`
- LiteLLM Redis：`litellm-tokyo-redis`
- Redis endpoint：`litellm-tokyo-redis.e4yzov.0001.apne1.cache.amazonaws.com:6379`
- LiteLLM backend 安全组：`sg-00b7e931c21fbead8`
- 测试 Redis：同机本地容器 `redis-test`

### 域名与访问

- 公有 hosted zone：`kollab.im.` (`Z0670501PRO3U4KXNDG6`)
- 私有 hosted zone：`llm.kollab.im.` (`Z0862216BMU2LPHPD7VG`)
- 生产域名：`llm.kollab.im`
- 测试域名：`llm-test.kollab.im`

### VPN / 堡垒机

- Bastion：`i-0ca129f97bdbfe9f2`
- 名称：`agentcore-tokyo-bastion`
- OpenVPN 客户端网段：`10.88.0.0/24`

### 监控与告警

- 监控与告警 runbook：`docs/litellm_tokyo_alarm_management.md`

## 当前运行方式

### LiteLLM 容器

当前线上 LiteLLM 是以 Docker 容器运行：

- 容器名：`litellm`
- 当前镜像：`litellm-openrouter-fix:20260326`
- 重启策略：`unless-stopped`
- 对外端口：`4000:4000`
- 配置挂载：`/opt/litellm/config.yaml -> /app/config.yaml`

当前启动形态等价于：

```bash
sudo docker run -d \
  --name litellm \
  --restart unless-stopped \
  -p 4000:4000 \
  --env-file /opt/litellm/.env \
  -v /opt/litellm/config.yaml:/app/config.yaml \
  litellm-openrouter-fix:20260326 \
  --config /app/config.yaml \
  --port 4000
```

注意：

- `/opt/litellm/.env` 存放数据库、Redis、master key 等敏感配置
- 不要把 `.env` 内容写进仓库
- 配置文件路径是 `/opt/litellm/config.yaml`
- 当前线上自定义镜像维护语义上对应 `openrouter-fix-20260326` 分支，而不是 `main`

### 运行时行为开关（代码内置，env 可调）

- `KOLLAB_LITELLM_ANTHROPIC_HEADERS_TIMEOUT_SECONDS`（默认 `240`，单位秒）：
  Anthropic `/v1/messages` 流式上游 POST 的"等响应头"预算。Bedrock
  `invoke-with-response-stream` 偶发接受请求后不回响应头，默认 httpx read
  timeout (600s) 会让每次尝试干等 10 分钟；这个预算让挂死的尝试快速失败并在
  handler 内重试一次（重试前重新 SigV4 签名）。只约束响应头到达前的等待，
  不影响流中间的 chunk 间隔，也不作用于非流式请求。
  排查日志 stage：`upstream_headers_timeout`（带 `attempt` / `will_retry`）。
  设为 `0` 或负数 = 关闭，恢复旧行为（运维级回滚，无需回滚代码）。
  改动容器 env 后必须重建容器才生效（`docker restart` 不会读取新 env，做法见 Tripo 一节）。

### 测试 LiteLLM 容器

当前测试环境与生产共机，但完全分容器、分配置、分数据库：

- 容器名：`litellm-test`
- 当前镜像：`litellm-openrouter-fix:20260326`
- 重启策略：`unless-stopped`
- 仅本机绑定端口：`127.0.0.1:4001->4000`
- 重启策略由 `deploy-tokyo.yml` 的 build 路径在 `docker run` 时显式设置为 `unless-stopped`；手工重建容器时也必须带 `--restart unless-stopped`，否则 EC2 重启后网关不会自启
- 配置目录：`/opt/litellm-test/`
- 环境文件：`/opt/litellm-test/.env`
- 配置文件：`/opt/litellm-test/config.yaml`

### Tripo 3D 自定义端点（V3）

`litellm/proxy/tripo_3d_endpoints/endpoints.py` 由 `proxy_server.py` 挂载，把 Tripo
官网 V3（`https://openapi.tripo3d.ai/v3`）的最小子集暴露给持有虚拟 key 的调用方，
Tripo 凭据只存在于网关进程：

- `POST /v1/3d/tasks`：JSON，`model` 固定 `tripo-3d`，`type` 为 `text_to_model`
  （`prompt`）或 `image_to_model`（`file_token` 与 `image_url` 二选一）；可选
  `model_version`（带日期的 V3 build，默认 `v3.0-20250812`）、`texture_quality` /
  `geometry_quality`（`standard` | `detailed`）、`texture` / `pbr`（布尔，默认 true）。
  其它字段一律 422，body 里带 `api_key` 之类凭据字段也 422。
- `POST /v1/3d/files`：multipart 字段 `file`，仅 JPG/PNG、≤20 MB，返回 `data.file_token`。
- `GET /v1/3d/tasks/{task_id}`：原样透传 Tripo 信封，终态 `data.credits_consumed`、
  `data.output.model_url` 等计费/交付字段不做改写。
- 鉴权：LiteLLM 虚拟 key，且该 key 必须被授权模型 `tripo-3d`（或不限模型）。
- `TRIPO_API_KEY` 缺失时 503 fail closed，不接受调用方自带 key。

`TRIPO_API_KEY` 的来源：

- TEST：SSM SecureString `/kollab/test/litellm/tripo_api_key`，同时写入
  `/opt/litellm-test/.env`。注意容器 env 在 `docker run` 时固化，workflow 的
  build-and-restart 是从**当前容器** `docker inspect` 复制 env，所以轮换 key 必须
  重建容器（`docker restart` 不会读新 `.env`）。
- PROD：尚未配置；`config/litellm.prod.yaml` 未声明该变量，生产上线需单独决策。

### MiniMax Music 自定义端点

`litellm/proxy/minimax_music_endpoints/endpoints.py`（共用
`litellm/proxy/kollab_media_common.py` 的鉴权 / 请求 id / 凭据拒绝 / SSRF 守卫）：

- `POST /v1/music/generations`：JSON，`model` 固定 `minimax-music`（上游固定
  `music-3.0`、`stream=false`）；必填 `prompt`（≤2000）、`lyrics`（≤3500）；可选
  `is_instrumental` / `lyrics_optimizer`（布尔）、`output_format`（`hex` 默认 | `url`）、
  `audio_setting`（`sample_rate` 16000/24000/32000/44100、`bitrate`
  32000/64000/128000/256000、`format` mp3/wav/pcm，默认 44100/256000/mp3）。
- 同步返回：MiniMax JSON 原样透传（`base_resp`、`data.audio`、`extra_info`、`trace_id`）。
  **MiniMax 业务失败也是 HTTP 200**，调用方必须检查 `base_resp.status_code == 0`。
- 计费：MiniMax 官方按首计价（$0.15/首，≤5 分钟），不按秒。
- 鉴权与 fail closed 同 Tripo：虚拟 key 需授权 `minimax-music`；`MINIMAX_API_KEY` 缺失 503。
- 密钥来源：TEST 为 SSM SecureString `/kollab/test/litellm/minimax_api_key`（取自 TEST
  K8s `seeds-runtime-env` 中 skills-server 正在用的 `MINIMAX_VIDEO_API_KEY`），同样写入
  `/opt/litellm-test/.env` 并重建容器生效；PROD 尚未配置。

### Kling V3（OpenRouter）自定义端点

`litellm/proxy/kling_video_endpoints/endpoints.py`。为什么不复用 seedance-2 走的原生
`/v1/videos`：原生 OpenRouter 视频适配器会丢掉 `aspect_ratio` / `resolution` /
`generate_audio` / `seed`，并且重建状态对象时丢掉 OpenRouter 的 `usage`（Kollab 计费要用）；
把 `kling-v3` 放进 `model_list` 还会让任意 key 绕过这里的 15 秒上限，所以 `kling-v3`
**刻意不是路由模型**，只作为 key 授权别名。

- `POST /v1/kling/videos`：`model` 固定 `kling-v3`（上游固定 `kwaivgi/kling-v3.0-pro`）；
  `prompt`（≤2500）必填；`duration` 整数 4–15（默认 6，与 Kollab 视频策略一致；OpenRouter 按秒计费，上限即花费上限）；
  可选 `aspect_ratio`（21:9/16:9/4:3/1:1/3:4/9:16）、`resolution`（480p/540p/720p/1080p/1K–4K）、
  `size`（`WxH`）、`generate_audio`（默认 false）、`seed`、`watermark`（→ `provider.watermark`）、
  `first_frame_image_url` / `last_frame_image_url` / `reference_image_urls`（≤9，与 Kollab 媒体上限一致，均须公网 HTTPS，
  共用 SSRF 守卫）。返回 OpenRouter JSON（`polling_url`、`status` 等原样），其中 `id` 被替换为
  网关签名 id `<OpenRouter id>~<签名>`，后续查询/下载必须用这个 id。
- `GET /v1/kling/videos/{video_id}`：透传状态 JSON（`usage`、`unsigned_urls`、`error` 原样，`id`
  换成签名 id）。签名用网关已有的 `LITELLM_SALT_KEY`（缺失时退回 `LITELLM_MASTER_KEY`）做 HMAC，
  未签名或签名不符的 id（例如同一 OpenRouter 账号上的 seedance 任务）一律 404，因为
  OpenRouter 状态不保证带 `model` 字段，不能靠它判断归属。
- `GET /v1/kling/videos/{video_id}/content?index=0`：先验签并查一次状态，再流式转发成片
  字节。`unsigned_urls` 指向 openrouter.ai 且需要网关 key，调用方必须走这个路由下载。
- 凭据：复用网关 LiteLLM 凭据 `openrouter-toyo`（与 seedance-2 同一把，可用 env
  `KLING_OPENROUTER_CREDENTIAL` 改名），不需要新 secret；凭据缺失 503。
- 授权：虚拟 key 需授权 `kling-v3`；TEST 的 Kollab server key 已由
  `scripts/reconcile_gateway_key_models.py` 自动授权。

测试 Redis：

- 容器名：`redis-test`
- 网络：`litellm-test-net`
- 端口：不对宿主机公网暴露，只给 `litellm-test` 容器使用

### Caddy 反向代理

当前入口由同机 Caddy 提供：

- 容器名：`caddy`
- 镜像：`local/caddy-route53:2.11.2`
- 网络模式：`host`
- Caddyfile：`/opt/caddy/Caddyfile`

当前 Caddy 配置核心逻辑：

```caddy
llm.kollab.im {
    tls {
        resolvers 1.1.1.1 8.8.8.8
        dns route53 {
            hosted_zone_id Z0670501PRO3U4KXNDG6
        }
    }
    @missingAgentcoreGateway {
        path /v1/messages*
        not {
            header X-Kollab-Agentcore-Gateway <server-side-secret>
        }
    }
    respond @missingAgentcoreGateway "missing x-kollab-agentcore-gateway" 403

    @missingEndUser {
        path /v1/messages*
        not header X-Litellm-End-User-Id *
    }
    respond @missingEndUser "missing x-litellm-end-user-id" 403

    @missingTags {
        path /v1/messages*
        not header X-Litellm-Tags *
    }
    respond @missingTags "missing x-litellm-tags" 403

    reverse_proxy 127.0.0.1:4000
}

llm-test.kollab.im {
    tls {
        resolvers 1.1.1.1 8.8.8.8
        dns route53 {
            hosted_zone_id Z0670501PRO3U4KXNDG6
        }
    }
    @missingAgentcoreGateway {
        path /v1/messages*
        not {
            header X-Kollab-Agentcore-Gateway <test-server-side-secret>
        }
    }
    respond @missingAgentcoreGateway "missing x-kollab-agentcore-gateway" 403

    @missingEndUser {
        path /v1/messages*
        not header X-Litellm-End-User-Id *
    }
    respond @missingEndUser "missing x-litellm-end-user-id" 403

    @missingTags {
        path /v1/messages*
        not header X-Litellm-Tags *
    }
    respond @missingTags "missing x-litellm-tags" 403

    reverse_proxy 127.0.0.1:4001
}
```

这意味着：

- TLS 证书由 Caddy + Route53 DNS challenge 管理
- Route53 DNS challenge 必须显式使用公共 resolver 验证 TXT 传播，避免本机或 VPC resolver 受私有 zone、缓存或 stale TXT 影响导致续签卡住
- 用户入口永远走 `443`
- LiteLLM 容器只负责本机 `4000`
- `/v1/messages*` 不能只靠 LiteLLM key 直连；生产入口必须同时带 Gateway 注入的 `X-Kollab-Agentcore-Gateway`、`X-Litellm-End-User-Id`、`X-Litellm-Tags`
- `llm-test.kollab.im` 也使用同一类 Gateway secret header 和 billing header 门禁，用于验证 AgentCore Gateway 到 LiteLLM 的服务端门禁链路
- `X-Kollab-Agentcore-Gateway` 的值只存在于 Caddy 和 Kollab K8s `agentcore-llm-gateway-env` secret 中，不能写入代码、文档、客户端环境变量或用户 runtime

修改 Caddyfile 时必须先备份 `/opt/caddy/Caddyfile`，再执行：

```bash
sudo docker exec caddy caddy validate --config /etc/caddy/Caddyfile
sudo docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

如果 Caddy 提示 ACME / DNS challenge 失败，先确认 public hosted zone 里是否有遗留 `_acme-challenge` TXT，再确认上面的 `resolvers` 仍然存在。健康检查不要只用 `curl -k`，还要用不跳过校验的请求或 `openssl s_client -servername llm.kollab.im -connect llm.kollab.im:443` 查看证书有效期。

门禁验证的最小证据：

- 从 K8s pod 直接请求 `https://llm.kollab.im/v1/messages` 或 `https://llm-test.kollab.im/v1/messages`，即使带 LiteLLM key 和 billing headers，只要缺少 `X-Kollab-Agentcore-Gateway` 就必须返回 `403`
- 通过 Kollab AgentCore LLM Gateway 请求同一路径必须继续成功，说明 secret header 只由服务端 Gateway 注入

## 当前 LiteLLM 配置文件

当前 `/opt/litellm/config.yaml` 的管理方式：

- 存在 EC2 本地磁盘上
- 通过 bind mount 挂到容器里
- 配置修改后需要重启 LiteLLM 容器

### 模型路由与凭证的真相源

Tokyo 环境同时存在两类模型路由，运维时必须区分：

- `config/litellm.test.yaml` 和 `config/litellm.prod.yaml` 中的路由与设置是 Git
  可重放真相。部署 workflow 会把对应文件同步到 EC2，实例重建或配置恢复时也以它们为准。
- 通过 LiteLLM Admin 创建的路由只保存在数据库中。它们可以立即服务运行时流量，但不会自动导出到
  Git，也不能替代需要随部署重放的 YAML 配置。

YAML 路由可以通过 `litellm_credential_name` 引用 LiteLLM 数据库中已有的加密凭证。测试环境
使用 `openrouter-gpt`，生产环境使用 `openrouter-toyo`。仓库只保存凭证名称，不保存
`api_key` 或任何密钥值；对应凭证必须在目标环境的 Admin 中预先存在且有效。

`claude-fable-5` 和 `claude-opus-4-8` 是 YAML 独占管理的 Bedrock 路由。部署 workflow
在代理健康检查后运行 `scripts/reconcile_managed_model_routes.py`：只有先证明预期 YAML route
存在，并通过 `model_id` 定向真实模型健康调用，才删除这两个 alias 的数据库 deployment，
随后再次确认每个 alias 只剩一条 Bedrock route。Sonnet 4.6、Sonnet 5 和 Opus 5 不在白名单内，继续
保留 OpenRouter。`restart` 和 `health-check` 辅助模式不执行这一有状态收敛。

`kimi-k3` 刻意只由 TEST / PROD 数据库中的阿里云路由
`dashscope/kimi/kimi-k3` 提供。Git YAML 不得再声明同名 deployment：`simple-shuffle`
会把 YAML 与 DB route 合并成同一个 model group，同名的 OpenRouter deployment 会让既有
conversation、Bot 和 timer 保存的 `kimi-k3` 选择随机回到海外 provider。移除 YAML route
不会迁移或废弃公开 alias；`store_model_in_db: true` 会继续加载原有阿里云 deployment。
部署前后都要用 `/model/info` 确认该 alias 在目标环境只收敛到这一条 DB route。
workflow 在取得 AWS 凭证和执行任何 SSM 改动前，会先运行 reconciler 的本地单元测试。

配置发布方式保持环境隔离：

- TEST：向 `openrouter-fix-20260326` 推送 `config/litellm.test.yaml` 后自动部署。
- PROD：只允许在 GitHub Actions 手动选择 `prod` 部署，不随 push 自动发布。

应用选择器开放新模型之前，必须从 AWS 内部执行一次真实模型调用。验证应从 LiteLLM EC2、
K8s pod 或 AgentCore 链路请求部署后的公开别名，并确认模型响应成功；只看到 Admin 路由、
`/models` 列表或健康检查成功不算模型可用。真实调用还要使用应用预期的参数（包括中等思考程度）
验证兼容性，确认后才能把同名选项暴露给用户。本地机器不直接调用模型提供商。

当前配置里至少包含：

- `model_list`
- `router_settings.redis_host`
- `router_settings.redis_port`
- `general_settings.master_key`
- `general_settings.store_model_in_db`

不要把完整线上配置和密钥直接复制进文档或仓库。

## 当前访问模型

### 安全组层

LiteLLM 当前只允许：

- `172.31.0.0/16 -> 443`
- `172.31.0.0/16 -> 4000`

这意味着：

- 同一 Tokyo VPC 内部服务可以直接访问
- 员工连上 OpenVPN 后也可以访问
- 外网不能直接访问 LiteLLM

### DNS 层

当前为了兼顾 VPN 使用体验，DNS 策略是：

- 公有 DNS `llm.kollab.im` 返回私网 IP `172.31.44.132`
- 私有 hosted zone 里也有 `llm.kollab.im -> 172.31.44.132`
- 公有 DNS `llm-test.kollab.im` 也返回私网 IP `172.31.44.132`

这样做的目的：

- Tokyo VPC 内部服务可以直接解析到私网地址
- 员工开 VPN 后，因为 `172.31.0.0/16` 会走隧道，也能访问这个私网地址
- 不需要再把客户端 DNS 强制切到 AWS resolver

### VPN 层

当前 OpenVPN 配置已经调整为：

- 保留 `push "route 172.31.0.0 255.255.0.0"`
- 移除 `push "dhcp-option DNS 172.31.0.2"`

效果：

- VPN 只接管 Tokyo 内网路由
- 不强制改用户本机 DNS
- 不容易再出现“开 VPN 后其他网站解析异常”

## 后续修改的推荐方式

### 1. 只改 LiteLLM 配置

如果只是改模型、路由、Redis、开关类配置：

1. 编辑 `/opt/litellm/config.yaml`
2. 重启 LiteLLM 容器
3. 做健康检查

示例：

```bash
sudo docker restart litellm
curl -skS https://llm.kollab.im/health/liveliness
```

测试环境则对应：

```bash
curl -s http://127.0.0.1:4001/health/liveliness
curl -skS https://llm-test.kollab.im/health/liveliness
```

### 2. 改 LiteLLM 镜像

如果要改 LiteLLM 代码：

1. 在仓库里完成代码变更
2. 构建新的自定义镜像
3. 在 EC2 上替换容器
4. 保留旧镜像标签，方便回滚

当前这台机器的实际做法允许两种路线：

- 从源码完整构建
- 基于稳定镜像做 overlay / patch 型构建

对这套环境来说，推荐：

- 小补丁：优先 overlay / patch 方式
- 大变更：走完整镜像构建

### 3. 改 DNS / 访问方式

如果调整访问模式，优先遵守：

- 不直接把 `4000` 暴露到公网
- 如需继续支持 VPN，确保 `llm.kollab.im` 最终能解析到私网 IP
- 如改 OpenVPN，记得提醒用户“需要断开重连一次 VPN”

## 部署 / 更新最小步骤

### 配置型更新（推荐：走 GitHub Actions）

`KollabTeam/litellm` 仓库的 `.github/workflows/deploy-tokyo.yml` 已经把 `aws ssm send-command`
的那套手动流程自动化，作为日常首选路径。

真相源：

- 仓库：`config/litellm.test.yaml`、`config/litellm.prod.yaml`
- 受控路由收敛：`scripts/reconcile_managed_model_routes.py`
- EC2：`/opt/litellm[-test]/config.yaml`（由 workflow 从仓库 sync 下来）

触发方式：

- **test**：在 `openrouter-fix-20260326` 分支改 `config/litellm.test.yaml` 后 push → 自动部署
- **prod**：GitHub Actions 页面手动 Run workflow，environment 选 `prod`（禁止 push 自动走 prod）

workflow 三挡 `action_mode`：

- `sync-and-restart`（默认）：把仓库里的 yaml 同步到 EC2、备份旧文件、`docker restart`
- `restart`：只 `docker restart`。注意它**不会**让 `.env` 改动生效：容器 env 在 `docker run` 时固化，改 env 必须重建容器（见上文 Tripo 一节的做法）
- `health-check`：只做远端 health 探针，不改任何状态

底层路径：GHA runner → AWS SSM → `i-04ba3dd7ee265426f` → `docker restart`。不依赖 SSH，因此不需要动 LiteLLM EC2 安全组。

涉及 `claude-fable-5` 或 `claude-opus-4-8` 时不得只执行下面的手动 restart；它会跳过
同名数据库 route 的白名单收敛。此类变更必须走 workflow，或完整重放 workflow 中的
reconciler 并验证 `/model/info` 唯一性。

### 配置型更新（手动备用）

workflow 不可用时可以退回手动：

```bash
env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY aws ssm send-command \
  --region ap-northeast-1 \
  --instance-ids i-04ba3dd7ee265426f \
  --document-name AWS-RunShellScript \
  --parameters commands='[
    "sudo sed -n '\''1,220p'\'' /opt/litellm/config.yaml",
    "sudo docker restart litellm",
    "curl -s http://127.0.0.1:4000/health/liveliness"
  ]'
```

注意：手动改完线上后，必须把最新 yaml 回写到仓库 `config/litellm.{test,prod}.yaml`，
保证仓库真相源和 EC2 一致，下次 workflow 才不会用旧 yaml 覆盖回去。

### GitHub Actions 部署配置真相

需要在 `KollabTeam/litellm` 仓库级别配好（当前已落地，不要删）：

- Secrets：
  - `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`：IAM user `litellm-gha-tokyo` 的 access key，
    权限只限对 `i-04ba3dd7ee265426f` 调 `ssm:SendCommand`
- Variables：
  - `AWS_REGION=ap-northeast-1`
  - `TOKYO_LITELLM_EC2_ID=i-04ba3dd7ee265426f`
- Runner：
  - 自托管 runner `litellm-runner-bastion`，label `litellm-tokyo`，
    跑在 Tokyo bastion (`13.113.90.234`, `i-0ca129f97bdbfe9f2`) 上
  - systemd unit `actions.runner.KollabTeam-litellm.litellm-runner-bastion.service`，
    `Restart=always`，开机自启
  - 安装目录：`/opt/actions-runner-litellm`，以专用用户 `litellm-runner` 运行

如果 runner 要重装或迁移，registration token 用：

```bash
gh api -X POST repos/KollabTeam/litellm/actions/runners/registration-token --jq '.token'
```

### 镜像型更新

推荐顺序：

1. 上传或构建新镜像
2. `docker stop litellm`
3. `docker rm litellm`
4. 用同样的 `--env-file`、挂载和端口重新起
5. 做本机和域名健康检查

## 回滚方式

### LiteLLM 容器回滚

如果新镜像启动失败：

1. 停掉新容器
2. 用上一版镜像标签重新起
3. 验证 `127.0.0.1:4000/health/liveliness`
4. 验证 `https://llm.kollab.im/health/liveliness`

### Caddy 回滚

如果入口配置改坏：

- 优先用 `/opt/caddy/Caddyfile.bak.*` 回滚
- 然后重启 `caddy` 容器

## 健康检查与验证

### 从 LiteLLM 实例本机验证

```bash
curl -s http://127.0.0.1:4000/health/liveliness
```

### 从 Tokyo 内网节点验证

```bash
curl -skS https://llm.kollab.im/health/liveliness
```

### 从 Tokyo 内网节点验证测试环境

```bash
curl -skS https://llm-test.kollab.im/health/liveliness
```

### 查看容器状态

```bash
sudo docker ps
sudo docker logs --tail 200 litellm
sudo docker logs --tail 200 caddy
```

## 常见变更点

后续 agent 最常改的通常是：

- `/opt/litellm/config.yaml`
- LiteLLM 容器镜像 tag
- `llm.kollab.im` 的 DNS 解析方式
- LiteLLM 安全组入口规则
- OpenVPN 是否推 DNS
- 告警阈值与通知链路

## 不要做的事

这套环境里尽量避免：

- 不要直接把 LiteLLM `4000` 端口暴露到公网
- 不要把 `.env`、数据库密码、master key 写进仓库
- 不要在不确认 VPN 路由影响的情况下重新加 `dhcp-option DNS 172.31.0.2`
- 不要只改公有 DNS 不改内网验证
- 不要只改容器不做健康检查

## 配套文档

- 告警与监控：`docs/litellm_tokyo_alarm_management.md`

如果后续这套环境迁移到新的实例、RDS、Redis 或新的域名，必须同步更新这份文档里的资源清单和访问模型。

## Gemini Omni TEST generate-only route

`gemini-omni` is configured only in `config/litellm.test.yaml`, backed by the
existing named Vertex credential and Cloud preview id `gemini-omni-1.1-flash-preview`.
The proxy exposes submission at `/v1beta/interactions` and polling at
`/v1beta/interactions/{id}?model=gemini-omni`; the provider adapter translates
to the global Cloud `v1beta1` Interactions endpoint. No production route is enabled.

Kollab admission is separately gated by `GEMINI_OMNI_ENABLED`; all requests go
through Kollab server and this managed gateway. Editing/extension are not connected.
Deployment success alone does not prove Omni project access, generated media or billing.
