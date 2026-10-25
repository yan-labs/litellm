# Wokey 媒体离线适配与下一步 TEST 验收

本变更只本地准备，不推送、不部署。本地 TEST YAML 将三个既有别名准备为
Wokey 主用、Kie 提交阶段备用；PROD、聊天、Seedance 路由保持原样。
官方依据：[Wokey 完整图片、视频参数与计费文档](https://wokey.ai/docs)。
本文的接口细节来自文档和禁网 mock，**不是上游实测记录**。

## 实现选择与契约

复用 LiteLLM OpenAI 图片 JSON/编辑 multipart 和视频同步/异步 HTTP 传输，
新增 Wokey 配置只负责不同的字段、端点、任务状态和实际费用，不新增依赖。
OpenAI 原实现不能直接绑定 api_base 即完成迁移：Wokey 图片编辑用
`/v1/images/generations` + `image` 文件；视频用 `duration_seconds`/`ratio`/
`video_resolution`，而 Kollab 使用 `seconds` 和 `extra_body`。

| 网关别名 | Wokey 上游 | Kie 备用 |
| --- | --- | --- |
| gpt-image-2 | gpt-image-2.5 | gpt-image-2-5-flare |
| grok-imagine-image | grok-imagine-image-2.0 | grok-imagine-image-2-0 |
| grok-imagine-video | grok-imagine-video-1.5 | grok-imagine-video-1-5-preview |

所有部署仅引用已有命名凭据 `wokey` 或 `kie-ai`。不读取、复制或新建上游密钥。

## Kollab 入参映射

只读 Kollab `origin/test` 的 `gpt_image_images_endpoint.py`、`xai_image.py`、
`grok_video.py`、`managed_openrouter_video.py`、`kie_video_contract.py` 和
`llm_gateway.py`，以及 server `common/billing/kie-route.ts` 与
`common/constants/model-price-config.ts`。

| Kollab → 网关 | Wokey 出站 | 兼容边界 |
| --- | --- | --- |
| GPT size=WIDTHxHEIGHT / auto | size=WIDTHxHEIGHT / 1024x1024 | 每边 128–4096；保留画幅 |
| GPT quality=low/medium/high | 最长边 1024/2048/4096 | 与既有 Kie quality→1K/2K/4K 对齐 |
| GPT background=auto/opaque | 不发送特殊背景控制 | transparent 未核验，提交前走 Kie |
| Grok image 顶层 aspect_ratio（也兼容 extra_body） | 1K 长边的 size | 16:9 → 1024x576；9:16 → 576x1024 |
| 图片 n=1 | n=1 | n>1 在提交前转 Kie，绝不少生成 |
| 图片编辑 multipart 图片 bytes | multipart image 文件 | 当前保守仅 1 张；多张提交前走 Kie |
| 视频 seconds（由 duration 转来） | duration_seconds 整数 | 当前 Grok 合同 1–15 秒；真实 SKU 待核验 |
| 视频 extra_body.aspect_ratio | ratio | 文档列 1:1/3:4/4:3/9:16/16:9/21:9 |
| 视频 extra_body.resolution | video_resolution | 480p/720p/1080p；真实 Grok SKU 待核验 |
| 视频 size | 反解 ratio/resolution | 已有显式 resolution 优先，避免审计尺寸误映射 |
| input_references:image_url / first_frame | 无上游认证下载参考图 → multipart image | 只 1 张，mode=image_to_video |
| 无参考图 | JSON，mode=text_to_video | 提交立即返回任务 ID |
| auto / 3:2 / 2:3、尾帧、多参考图、音视频参考、generate_audio | 提交前拒绝 Wokey | 完整请求进入 Kie 备用，不截断或改画幅 |

Grok 默认 `auto` 画幅将继续用 Kie，直到上游真实证明有等价语义；需要使用
Wokey 的测试应明确选择文档支持画幅。多图与批量图片同理保留 Kie。

## 返回与结算

- 图片：返回原 `data[].b64_json`。Kollab 解码并写 S3，随后注册会话 artifact。
  不返回上游临时 URL。
- 图片实际费用：只读取上游 `price_usd`、`usage.cost_usd`、`usage.cost` 或
  实际费用响应头。缺少真实费用时 `cost_authoritative=false`、
  `billing_pending=true`，不把文档价格或 token 当已结算费用。
- 视频：提交 `202`/`in_progress` → 网关 `processing`，返回 provider/model 编码 ID。
  `queued` → queued；completed → completed；failed/cancelled → failed。
- 视频完成状态将 `price_usd` 映射为 `usage.cost_usd`；提交只预留，GET 只读取，
  网关费用头都是 0，避免每次轮询扣费。Kollab 原任务 ledger 完成时结算一次。
- 视频下载：直接认证 GET `/v1/videos/{id}/content`，即文档的 canonical
  `content_url` 路由，返回 MP4 字节。Kollab 完成轮询后立即下载并写 S3/artifact。
- 文件保存期：文档仅要求尽快下载，没有具体 TTL。不能宣称已验证有效期。
- 当前 Kollab 会将三个媒体的 provider 身份改写为 kie；实际费用字段仍保留，
  以原 KIE 1.0× 结算。无真实费用时仍有旧价格预估/上限，不能宣称降价已生效。
- Router 注册自定义价格时可能覆盖 `kie_config`，备用入口现在会从原已打包
  Kie 目录恢复缺失能力，避免 Wokey 提交失败却无法提交备用。

## 重复生成与兜底

1. 参数不支持/明确 HTTP 提交拒绝可以使用 Kie 备用。
2. Wokey 提交成功后以 provider 编码 ID 读取任务；状态或成品读取失败不切 Kie。
3. POST 超时/连接异常、成功响应缺 ID、成功响应状态异常，结果可能已被接受；
   标记 `media_no_resubmit`，禁止包装层、Router 首轮/后续重试、备用链重提。
4. `Idempotency-Key` 大小写不敏感地保留调用方值；无值时在第一次 POST 前生成。
   HTTP 层自身连接重试继续使用同一个头。需要可恢复的真实验收必须由调用方
   提供固定 key；不可在超时后换 key 再发。
5. 健康检查用内部 context 调用免费 GET `/v1/models?output_modalities=...`；
   只证明目录连接可用，不提交收费内容。
6. 后续切回 Kie 时须保留 Wokey deployment 直到旧任务排空；否则代理无法按
   已编码原生 model 找到原命名凭据。本单没有实现跨发布任务迁移。

## 下一单在 TEST 部署后验收

- 等聊天任务结束，再通过既有流程发布包含本地提交的 TEST 镜像。
- 确认三条 TEST 主用/私有备用条目、费用读回及命名凭据可解析；不动 PROD。
- 先核对受管上游目录：`/v1/models/pricing`、`/v1/images/models`、
  `/v1/videos/models` 的真实模型、模式、时长、画幅、分辨率和参考图限制。
- 所有普通实测经 Kollab Server → LiteLLM；显式 Auto，process-scoped TEST session，
  `KOLLAB_API_URL=https://test.flowus.work`。不使用 stored production profile。
- 先单图片文生图，再以该产物做单图片编辑；明确 1K、n=1，核对响应 b64、
  真实尺寸/格式、S3 artifact、provider 原始费用与产品单次结算。
- 分别提交 480p/1 秒/16:9 Grok 文生视频与单图生视频。接受 202 后保存
  ID/固定幂等 key，再轮询 pending→terminal；下载 MP4，验证 artifact 能播放。
- 对照同一任务 reserved→settled、失败释放预留；重复 GET 不增加费用。
- 单独以确定不支持的参数验证提交前 Kie 备用；mock 故障注入验证任务创建后
  GET 失败不会发生另一笔 POST。不要为制造真实失败反复付费。
- 核对 content 认证、过期码及实际保留时间；能拿到 expires_at 时记录，不为
  等待未公开 TTL 占用本轮或将“还可下载”当无限期。
- 临时别名如需创建只用 `wokey-media-` 前缀，报告列出并保留。本单未创建任何别名。
- 四个基础样本预估 $0.01 × 2 + $0.0056 × 1s × 2 = **$0.0312**；另加
  一张 Grok 图片预估总 **$0.0412**。金额来自 brief/文档，必须在下一单目录
  核对后再执行；总上限 $1，禁止充值/新增套餐或无证据重复失败调用。

### 每个别名的网关请求体与期望

GPT 文生图，预计 $0.01，返回一张可解码/转存图片：

```json
{"model":"gpt-image-2","prompt":"A small blue paper boat on a white table","n":1,"size":"1024x1024","quality":"low","background":"opaque","response_format":"b64_json"}
```

GPT 图生图，同一 `model/prompt/n/size/quality/background/response_format`
转换为网关 `/v1/images/edits` multipart，上传上一张图的 bytes；Wokey 出站
仍为 `/v1/images/generations` multipart `image`，预计 $0.01。

Grok 图片，预计 $0.01，尺寸按画幅返回 inline bytes：

```json
{"model":"grok-imagine-image","prompt":"A small blue paper boat on a white table","n":1,"response_format":"b64_json","aspect_ratio":"16:9"}
```

Grok 文生视频，预计 $0.0056：

```json
{"model":"grok-imagine-video","prompt":"A blue paper boat gently moves on calm water","seconds":1,"extra_body":{"aspect_ratio":"16:9","resolution":"480p"}}
```

设置请求头 `Idempotency-Key: wokey-media-video-text-0001`。预期一次 POST、
provider 编码 ID、完成 usage.cost_usd、认证 MP4、产品只结算一次。

Grok 图生视频，预计 $0.0056：

```json
{"model":"grok-imagine-video","prompt":"The paper boat gently moves","seconds":1,"extra_body":{"aspect_ratio":"16:9","resolution":"480p","input_references":[{"type":"image_url","image_url":{"url":"<上一张产物的受控可下载URL>"}}]}}
```

设置独立固定头 `Idempotency-Key: wokey-media-video-image-0001`；Wokey 实际
出站为 multipart image，data 为 model/prompt/duration_seconds/ratio/
video_resolution/mode=image_to_video。

## 离线重跑

在本 worktree 用 uv 缓存和 pytest-socket 运行。报告的 `run_offline.py` 在
导入 LiteLLM 前封禁 TCP/UDP，设置本地成本表，只允许 asyncio 内部 UNIX socket。
无运行依赖或锁文件变更。

```sh
uv run --offline --no-project --with litellm --with pytest --with pytest-asyncio \
  --with pytest-socket --with pyyaml --with fastapi --with orjson \
  --with backoff --with cryptography python /absolute/path/run_offline.py \
  tests/test_litellm/llms/wokey/test_media.py \
  scripts/provider_routing/test_route_cli.py \
  scripts/test_multimedia_config_release.py scripts/test_openrouter_video_routes.py -q
```
