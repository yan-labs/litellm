# 集中模型路由

`routes.json` 管理平台连接、分环境凭据名称、公共别名、真实模型 ID、价格 metadata 和有序备用。
模型请求仍由受管网关执行；CLI 只修改本地表和指定环境 YAML，不连接管理 API、不部署、不保存密钥。
现有 PyYAML 与 Python 标准库即可运行。

## 看配置和往返核对

```sh
python3 scripts/route_cli.py --env test status
python3 scripts/route_cli.py --env test check
python3 scripts/route_cli.py import > /tmp/inferred-routes.json
python3 scripts/route_cli.py --table /tmp/inferred-routes.json --env test render
python3 -m unittest discover -s scripts/provider_routing -v
```

`import` 保留完整 model_list 与备用语义，识别已生成的内部备用别名；再 render 的结构化 diff 必须为 0。
`check` 验证名称、连接、凭据声明、组、备用目标和环路；不证明线上凭据或供应商能力。
`apply` 用 YAML 节点定位只替换受管条目和受管 fallbacks，保留其它设置、条目及注释。
未改变配置时不写文件。`additions` 显式登记从 DB 转入 YAML 的公共别名。

## 一条命令切换与回滚

```sh
python3 scripts/route_cli.py --env test set wokey-switch --primary wokey --fallback kie,openrouter --apply
python3 scripts/route_cli.py --env test rollback --apply
```

加 `--dry-run` 不写文件；不加 `--apply` 只改表。也可单独执行：

```sh
python3 scripts/route_cli.py --env test apply --dry-run
python3 scripts/route_cli.py --env test apply
```

单别名可替代组名。备用严格按参数顺序；只有没有 Kie 映射的别名跳过 Kie。
待发布配置：Fable 与两个 Haiku 别名走 Wokey，其它已登记聊天路线保留 Wokey → Kie 或 Kie 主用，均无 OpenRouter 备用。
GLM 5.3 Flash 与 DeepSeek V4.1 Flash 复用各环境既有 Aliyun 私有连接，不再声明 OpenRouter 备用。
Astra Pro 与聊天 openrouter/* 通配已从待发布配置移除；历史 OpenRouter backend 映射仅供离线历史导入，不在运行路由链中。
语音 openrouter-tts/*、openrouter-stt/* 及 TEST grok-stt 尚无已验证的受管等价路线，保留现有入口；这些配置不能宣称全模型已去 OpenRouter。
回滚保留最近一版全表，绑定修改环境；回滚后历史消耗。Git 可恢复更早版本。
每次只写选定环境 YAML；并发编辑请用隔离 Git 分支。

## 新增平台

1. 在 `platforms.<平台>.profiles` 声明已支持的适配器、api_base、custom_llm_provider 和凭据名称。
2. 先通过供应商模型目录核对真实 ID，再在目标别名 `backends` 声明 profile、model 与必要参数。
3. 在目标环境 `credentials` 登记名称，通过该环境 Admin API 建命名凭据；不把值写入表。
4. 检查、切换、apply、提交，再按既有发布契约部署并验实际出站、工具、缓存、备用和积分。

Wokey 地址为 `https://api.wokey.ai`；Claude 用 anthropic，GPT 用 openai/responses 与 `/v1` base。
Wokey Responses 拒绝 `user` 和 `safety_identifier`；共享 OpenAI Responses 出站转换仅在精确 `api.wokey.ai` 主机把 `user` 存入 `metadata.user_id`，已有 metadata 优先，保留工具/推理语义。Chat 原生支持 `user`；其它上游不变。该 metadata 仅用于归因，不代表安全标识能力。
离线合同：`tests/test_litellm/llms/openai/responses/test_wokey_user_metadata.py`。真实 TEST 用例：process-scoped 登录，具体 GPT 别名、自然旅游/写作问题；回读回答、最终 Wokey 出站与逐笔积分，禁止把 Kie 兜底算通过。
不删除 tool_choice。内部备用使用 `__route__<公共别名>__<平台>`，避免公共别名互相绕回。
收敛脚本读取应用后的 YAML；key 授权增量包含私有备用；多媒体校验保持媒体固定定义并核对集中表。
Kimi K3 当前只有含私有凭据的 DB 路由，未导入 YAML，也未切换。

## TEST 部署

含工具选择转换代码，必须 **build-and-restart**，不能仅 sync-and-restart。
推送到既有发布分支（先 pull --rebase，禁止强推），再执行：

```sh
gh workflow run deploy-tokyo.yml --repo KollabTeam/litellm --ref openrouter-fix-20260326 \
  -f environment=test -f action_mode=build-and-restart -f expected_sha="$(git rev-parse HEAD)"
```

核对运行镜像对应提交、配置、同名 DB 路由清理、key 授权；按实际出站确认 Wokey，不能把备用成功算主用成功。
已知旧 Kie 路由健康检查偶发超时，最多重跑失败 workflow 三次，保留失败证据，不能删旧别名凑绿。

## PROD 后续清单（本轮不执行）

- 先在 PROD 建立命名凭据 `wokey`，检查真实 ID、余额、价格、协议、备用可用性。
- 明确生产授权后，先检查再切 PROD 表并 apply；本轮 PROD YAML 不变。
- 含网关转换代码时使用 environment=prod、build-and-restart、expected_sha 固定头。
- 回读运行提交、清理同名 DB 路由、核对授权，完成工具/流式/缓存/备用和真实积分链路。
- 失败时按该环境历史 rollback --apply，提交后再次部署；不在 Admin API 上临时改 YAML 路由。

## 第二批：私有 Aliyun deployment 与健康探测

`aliyun.private-deepseek` 只保存 `db_source_alias=deepseek-v4-flash`，不声明新凭据。
`apply` 从公共 YAML 删除 V4.1 Kie deployment，不声明跨平台 fallback，并写
`db_managed_routes` 的可重放 opt-in。部署在 YAML 收敛前运行
`reconcile_aliyun_v41_route.py`：原样复用既有私有连接，在进程内创建新 V4.1 DB deployment，
只清除 V4.1 的旧 Kie/OpenRouter DB 同名项；既有四条 Aliyun deployment 完全不写。
新 route 不属于 YAML 删除集合；未带 opt-in 的环境不会创建它。
`import`/`render`/`apply`/`rollback` 保留这项 DB 所有权，不导出秘密。
Wokey 聊天与新 Aliyun 路由设置 `disable_health_check`，自动健康集合与收敛步骤跳过推理，
不把跳过报告成健康；真实可用性由本批次自然提问验收。媒体健康检查只读免费目录。
其它部署聊天探测使用自然旅游问题，不能使用 test/ping 或精确复读。

## 第二批生产切换清单（只准备，不执行）

1. 生产发布时先在 PROD 网关建立 `wokey` 命名凭据；
   `~/kollab-secrets/wokey.env` 只由受控进程加载并直传，不打印、不写 Git。
2. 从最新生产配置重新核对三媒体、13 聊天和 V4.1 的现状，保存各路由/备用/价格与回滚基线。
   将 TEST 已验收的三媒体 Wokey backend 与 Aliyun DB-source profile 登记到 PROD 表，
   对既有 Aliyun flash/pro/kimi/glm 只作白名单只读对比；不要复制 TEST 私有地址或秘密到 PROD。
3. 13 聊天用 `set wokey-switch --primary wokey --fallback kie,openrouter`；
   Fable 保持 Wokey → OpenRouter。三媒体逐别名 set 为 Wokey → Kie；
   V4.1 set 为 Aliyun → OpenRouter。PROD 表必须先拥有对应 backend，不能凭空切换。
   为 Wokey 聊天与 V4.1 标明自动探测禁用；Fable/Opus 仅一次自然小样本。
4. 先做生产 V4.1 私有连接单次自然权限样本；无权即停止该迁移，另行评估 OpenRouter 主路线，
   不改 Aliyun 现有连接/凭据。检查后 apply PROD、提交并非强推。
5. 固定 expected_sha，environment=prod、build-and-restart；包含网关媒体/GPT代码，
   sync-and-restart 不能作为首次上线。部署读回镜像、配置、唯一 deployment 和备用顺序。
6. 生产重跑自然聊天矩阵、三媒体真实最小样本、提交失败备用、视频终态下载、积分和余额账单。
   已提交视频的轮询失败绝不触发重新生成；保留 Wokey deployment 直到旧视频任务排空。
7. 回滚时用切换前 Git 快照恢复整批表，或逐次 route rollback（仅保留最近一次历史），
   apply PROD、提交并 sync-and-restart。媒体恢复 Kie，聊天恢复既定旧主路线，
   V4.1 恢复 OpenRouter（不得新启用 Kie）；移除 opt-in 后确认 YAML 收敛清除 V4.1 同名 DB deployment。
   视频排空前将视频路线显式设置为 Kie → Wokey，保留同原生 model 的 Wokey 私有备用 deployment 和命名凭据，GET 不重新提交；排空后才移除备用。

### Kollab 仓库侧待同步清单（本轮只读）

`common/billing/kie-route.ts` 当前把目标媒体/聊天/V4.1 别名识别为 Kie 计费身份，
这个集合也控制 1.0× 和免 OpenRouter 采购费；直接删除别名会改变用户口径。
生产切换应把「用户计费合同」与「真实上游身份」的识别同步，保持用户积分口径，
并核对媒体预估、最终 Wokey authoritative cost、视频只终态一次结算与 OpenRouter 备用费用。
若预估代码因 alias 重写 provider=kie，需同步新路线的估算来源和价格回执，
不能把 Wokey 账单当 Kie 成本，也不能靠只改显示 provider 修正计费。
Kollab 产品目录、权限与价表必须同步；真实 TEST 积分检查会记录是否仍存在这项生产前置差距。

## OpenRouter 退役发布前置

本地配置与生产运行状态分开核验。先通过 SSM 容器内 `/model/info` 和 `/v1/models` 读取别名、provider、脱敏 api_base；还需读取运行 YAML 的 fallbacks。
生产只读基线仍有 OpenRouter 语音、旧 GPT Mini、GLM 5V 和历史 DB 路由；修改 YAML 不会自动移除这些 DB-owned 行。
只清理已核实零流量且已下架的精确 DB id；Astra Pro 在 TEST 同时有 YAML 和 DB deployment，发布后必须清理其旧 DB 行。
`bedrock-claude-haiku` 保留为 Wokey Haiku 5.5 兼容别名，价格 metadata 与公开 Haiku 5.5 相同。
生产 `gpt-5-mini` 使用 TEST 已登记的 Vertex 兼容路线，仍须在生产清理旧同名 DB deployment，防止混合负载均衡。
语音有真实流量且无等价已验证 route，GLM 5V 的 Aliyun 模型 ID/能力尚未确认；这两项未解决前，零 OpenRouter 的完整验收未通过。
推送发布分支会自动部署 TEST；若任务禁止部署，不推送该分支，也不 dispatch。生产发布始终由主线程按固定 SHA 统一执行。

修订后的退役发布：两个 Haiku 名均 Wokey→KIE；Luna 兼容名转现有 GPT-6.1 Sol，计费同步新型号官方价；GPT Mini 使用 Vertex Gemini 3.1 Flash Lite 兼容名。`reconcile_aliyun_v41_route.py` 在既有私有路线收敛后退役已下架、七日无调用的旧 GLM/Kimi/MiniMax/Astra Pro DB route。语音保留唯一 OpenRouter 能力类别，生产切换必须先网关后 Kollab。

## Gemini 4 Argon

`gemini-4-argon` uses the existing Google Vertex managed credential in `global` and remains explicit-selection only in Kollab. Standard PayGo input/output (including thinking)/cache read are $4/$20/$0.20 per million tokens; the 50% credits-back promotion through November 30 does not change these standard rates. Unified context is 1,048,576; synchronous output is capped at 32,768 and long-decode continuation is not enabled. Cyber Permissive is a separate allowlisted model and is not registered.
Gemini 4 uses thinking levels, including MEDIUM, rather than thinkingBudget, so deploy with `build-and-restart`. TEST route and key grant must be live before the product catalog. Production files are prepared here; publish the production gateway and verify its key grant first, then publish the Kollab main catalog.

退役现在由部署末步 `--retire-only` 执行，路由收敛与产品 key 授权在前。
删除前 `--export-only --export <file>` 备份全部 DB 路由字段（不含凭据值），`--dry-run --export <file>` 预演七日无调用判据；失败保留路由并让 workflow 非零退出。
生产 JEV 的现行 `enabled: true` 是既有设置，不能记成关闭；授权回读必须与这项开关一致。
生产切换的完整范围、备份与逐模型读回见 Kollab `apps/deploy/litellm-config/DEPLOYMENT.md`。
