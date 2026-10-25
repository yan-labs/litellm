# Provider routing 离线原型交付报告

## 规则回执

- 目标仅为集中路由表与本地命令行原型；续接已有提交 `d6000175fa6df66da00d55343066643319acf726`，不重写原型。
- 工作位置：`/Users/kcsx/litellm-wt/provider-routing`，分支 `provider-routing-proto`；基线 `origin/openrouter-fix-20260326` 为 `d9ddddd74e552c5d6e662c4d645c0cbfc7d7db1f`。
- 只涉及相对基线新增的原型文件。未修改 Kollab、占用 YAML、收敛/校验脚本及其测试；未推送、未触发 workflow、未连接网关或数据库，未读凭据值、未付费。
- 本次明确要求只提交本地，覆盖仓库普通任务推送/TEST 部署规则；离线原型不进入线上验收。采用 dev-kollab 本地理解与核验规则，不运行自动发布管线。

## 结论

离线原型已完成：一份表管理 TEST/PROD、平台连接、别名真实模型映射、主用与有序备用；同一个 CLI 提供 status、set、rollback、render、check、import。
24 项离线单元测试全部通过。现有 YAML 反推后再渲染，两环境全部 model_list 与 router fallbacks 的结构化 diff 均为 0 行，包含各 19 个 Kie 对话别名及图片/视频路线。
三个 GPT 别名模拟切到 Wokey、保留 Kie 备用，以及 dry-run 和本地回滚均已实际运行；演示配置不等于 Wokey 已真实接入。

## 实际执行

1. 检查前次提交和工作区：原型初版已有四个新增文件，工作区干净，merge-base 与 origin 头一致。
2. 阅读 CLI、单文件目录、全部原型测试及使用说明；执行初版 22 项测试，均通过。
3. 补充 PROD 组切换/回滚隔离测试及两别名循环测试，重跑 24 项，均通过；未改现有网关源代码。
4. 只读导入现有两份 YAML，对全部字段作结构化往返比对；在临时目录实际执行切换、dry-run、状态、回滚。
5. 核验占用文件内容与基线逐字节一致；两次只读查询远端头，均为 `d9ddddd74e552c5d6e662c4d645c0cbfc7d7db1f`；未 fetch、未 rebase、未改变远端。
6. 本报告另存于原型目录，作为本地提交的验收记录。最终提交后另回读工作区状态和远端头。

## 改动与产物

- `config/routing/routes.json`：集中表，含 Kie/OpenRouter/Google/DashScope/Wokey 平台目录、连接 profiles、环境凭据名称、路由及分组。源 YAML 未指定 api_base 的连接保留 adapter 默认地址，不凭空加配置。
- `scripts/route_cli.py`：269 行离线 CLI，复用已有 PyYAML 与 Python 标准库，无新依赖；不带 dry-run 的 set/rollback 也仅写本地表。
- `scripts/provider_routing/test_route_cli.py`：24 项离线测试，网络连接被禁止，副作用局限临时目录。
- `config/routing/README.md`：简短操作说明、平台新增方式、整组迁移示例、回滚、未来集成步骤。
- `config/routing/ACCEPTANCE.md`：本报告的提交内副本；外部报告为 `/Users/kcsx/.agent-reports/2026-10-05/provider-routing-design.md`。

### 使用说明

```sh
python3 scripts/route_cli.py --env test status
python3 scripts/route_cli.py --env prod check
python3 scripts/route_cli.py import > /tmp/inferred-routes.json
python3 scripts/route_cli.py --table /tmp/inferred-routes.json --env test render
cp config/routing/routes.json /tmp/routing-demo.json
python3 scripts/route_cli.py --table /tmp/routing-demo.json --env test set gpt-demo --primary wokey --fallback kie --dry-run
python3 scripts/route_cli.py --table /tmp/routing-demo.json --env test set gpt-demo --primary wokey --fallback kie
python3 scripts/route_cli.py --table /tmp/routing-demo.json --env test rollback
```

新增平台：仅在 platforms 加平台条目与连接 profiles，无需改脚本；对需迁移的别名 backends 登记平台实际模型 ID、必要参数，再将凭据名称登记到目标环境。没有模型映射的别名不能切换。
`gpt-demo` 是 gpt-6.1-sol、gpt-6-sol、gpt-6-luna；`kie-chat` 是全部 19 个对话别名，但不能把未核实的 Wokey 支持能力填成真。
回滚保存最近一版全表，在同一文件 previous 内记录，必须使用最近修改的环境；回滚消耗历史，不提供多版本栈。set 替换备用列表，`--fallback kie,openrouter` 保留指定顺序。

### 网关空出后如何并回

以下仅是部署步骤说明，本任务未执行：收到发版会话“网关已空出”通知后，fetch，再 `git rebase origin/openrouter-fix-20260326`，原型改动相对基线都是新增文件。重新 import 最新 YAML，逐项带回仍有效的平台/组定义，重跑单测和零差异比对。
随后需改 `config/litellm.test.yaml`、`config/litellm.prod.yaml`，只替换渲染的 model_list/fallbacks，保留其它配置；让 `scripts/reconcile_managed_model_routes.py`、`scripts/reconcile_gateway_key_models.py`、`scripts/validate_multimedia_config.py` 及相应测试统一读取路由表，避免旧脚本覆写新主用。部署前核实受管凭据、DB 路由优先级、备用内部别名的 virtual-key 授权、平台真实模型/响应协议以及价格与能力 metadata，之后由既有环境 workflow 发布并验真实主用/故障备用。

## 验证证据

### 离线单测

命令：`python3 -m unittest discover -s scripts/provider_routing -v`

```text
test_alias_cycle (test_route_cli.RoutingTests.test_alias_cycle)
Reject a fallback target which returns to its own public alias. ... ok
test_alias_set_mismatch (test_route_cli.RoutingTests.test_alias_set_mismatch)
Compare the catalog against authoritative local YAML names. ... ok
test_checks_both_environments (test_route_cli.RoutingTests.test_checks_both_environments)
Reconcile all public aliases and declared credential names offline. ... ok
test_cli_read_commands (test_route_cli.RoutingTests.test_cli_read_commands)
Status, render and import are local-only and leave the table untouched. ... ok
test_committed_table_roundtrip (test_route_cli.RoutingTests.test_committed_table_roundtrip)
Inactive demo mappings must not affect production or TEST rendering. ... ok
test_duplicate_alias (test_route_cli.RoutingTests.test_duplicate_alias)
Duplicate route entries are rejected before rendering. ... ok
test_duplicate_json_keys (test_route_cli.RoutingTests.test_duplicate_json_keys)
Reject duplicate keys instead of accepting JSON's last-value behavior. ... ok
test_group_switch_isolated (test_route_cli.RoutingTests.test_group_switch_isolated)
A TEST group switch keeps PROD and the input catalog unchanged. ... ok
test_import_rejects_inline_credentials (test_route_cli.RoutingTests.test_import_rejects_inline_credentials)
Import never emits literal credential fields from source snapshots. ... ok
test_inline_credential_rejected (test_route_cli.RoutingTests.test_inline_credential_rejected)
Catalog rendering accepts credential names only, never secret values. ... ok
test_invalid_set_does_not_write (test_route_cli.RoutingTests.test_invalid_set_does_not_write)
Failed mapping validation cannot change local catalog/history. ... ok
test_local_set_rollback_and_dryrun (test_route_cli.RoutingTests.test_local_set_rollback_and_dryrun)
CLI dry run is read-only, local set persists, rollback restores exactly. ... ok
test_missing_credential (test_route_cli.RoutingTests.test_missing_credential)
A connection must reference the environment's credential declaration. ... ok
test_nineteen_chat_aliases (test_route_cli.RoutingTests.test_nineteen_chat_aliases)
Assert the brief's 19 managed chat routes in each snapshot. ... ok
test_ordered_multiple_fallbacks (test_route_cli.RoutingTests.test_ordered_multiple_fallbacks)
Preserve requested Kie then OpenRouter fallback order. ... ok
test_platform_cycle (test_route_cli.RoutingTests.test_platform_cycle)
Repeated primary in the backup chain is a cycle. ... ok
test_prod_group_switch_and_rollback (test_route_cli.RoutingTests.test_prod_group_switch_and_rollback)
The same CLI switches PROD locally and restores both environment tables. ... ok
test_rollback_environment_fence (test_route_cli.RoutingTests.test_rollback_environment_fence)
Wrong environment cannot silently restore another environment's edit. ... ok
test_rollback_without_history (test_route_cli.RoutingTests.test_rollback_without_history)
One-step rollback reports absence of a previous version. ... ok
test_snapshot_roundtrip (test_route_cli.RoutingTests.test_snapshot_roundtrip)
Require exact structured YAML equality for both environment fragments. ... ok
test_two_alias_fallback_cycle (test_route_cli.RoutingTests.test_two_alias_fallback_cycle)
Check detects a loop through sibling aliases rather than only self-loops. ... ok
test_unknown_fallback_target (test_route_cli.RoutingTests.test_unknown_fallback_target)
External targets must resolve through a declared wildcard or alias. ... ok
test_unknown_model_mapping (test_route_cli.RoutingTests.test_unknown_model_mapping)
A platform declaration alone never invents model compatibility. ... ok
test_unknown_selector (test_route_cli.RoutingTests.test_unknown_selector)
Reject a typo rather than silently succeeding on an empty selection. ... ok

----------------------------------------------------------------------
Ran 24 tests in 1.869s

OK
```

### 从 YAML 反推并渲染回去

比较对象是完整 `model_list` 和 `router_settings.fallbacks`；JSON 字典键排序用于消除 YAML 排版/键顺序，列表顺序和所有字段值保留，不忽略任何受管参数或 metadata。

```text
test: 39 public aliases; Kie chat=19; Kie media=7; semantic diff=0 lines
Kie media aliases: seedance-2-mini, grok-imagine-image, seedance-2, seedance-2.5, hailuo-3, grok-imagine-video, gpt-image-2
prod: 38 public aliases; Kie chat=19; Kie media=7; semantic diff=0 lines
Kie media aliases: seedance-2-mini, grok-imagine-image, seedance-2, seedance-2.5, hailuo-3, grok-imagine-video, gpt-image-2
test unified diff: (empty)
prod unified diff: (empty)
```

结构化原样相等由 `test_snapshot_roundtrip` 与 `test_committed_table_roundtrip` 双重验证；后者确认加入未激活的 Wokey/DashScope 目录也不改变网关渲染。
其它 router/global/credential 内容不属于 render 片段，原型未写这些原文件。离线 check 输出：

```text
OK test: 39 aliases; credential names declared (values not checked)
OK prod: 38 aliases; credential names declared (values not checked)
```

### 模拟切换 dry-run 实际输出

命令：`set gpt-demo --primary wokey --fallback kie --dry-run`，在临时表运行；前后文件字节完全相同。

```diff
--- test/before
+++ test/proposed
@@ -224,28 +224,39 @@
     use_litellm_proxy: false
     merge_reasoning_content_in_choices: false
     num_retries: 1
-    api_base: https://api.kie.ai/codex/v1
-    custom_llm_provider: openai
-    litellm_credential_name: kie-ai
+    api_base: https://wokey.invalid/v1
+    custom_llm_provider: openai
+    litellm_credential_name: wokey-demo
     model: openai/responses/gpt-6-sol
-  model_info:
+  model_info: &id001
     health_check_max_tokens: 16
     litellm_provider: openai
     input_cost_per_token: 2.0e-06
     output_cost_per_token: 1.0e-05
     cache_read_input_token_cost: 2.0e-07
     cache_creation_input_token_cost: 2.5e-06
+- model_name: __route__gpt-6-sol__kie
+  litellm_params:
+    use_in_pass_through: false
+    use_litellm_proxy: false
+    merge_reasoning_content_in_choices: false
+    num_retries: 1
+    api_base: https://api.kie.ai/codex/v1
+    custom_llm_provider: openai
+    litellm_credential_name: kie-ai
+    model: openai/responses/gpt-6-sol
+  model_info: *id001
 - model_name: gpt-6.1-sol
   litellm_params:
     use_in_pass_through: false
     use_litellm_proxy: false
     merge_reasoning_content_in_choices: false
     num_retries: 1
-    api_base: https://api.kie.ai/codex/v1
-    custom_llm_provider: openai
-    litellm_credential_name: kie-ai
+    api_base: https://wokey.invalid/v1
+    custom_llm_provider: openai
+    litellm_credential_name: wokey-demo
     model: openai/responses/gpt-6-1-sol
-  model_info:
+  model_info: &id002
     health_check_max_tokens: 16
     litellm_provider: openai
     mode: chat
@@ -263,17 +274,28 @@
     supports_function_calling: true
     supports_prompt_caching: true
     max_tokens: 128000
+- model_name: __route__gpt-6.1-sol__kie
+  litellm_params:
+    use_in_pass_through: false
+    use_litellm_proxy: false
+    merge_reasoning_content_in_choices: false
+    num_retries: 1
+    api_base: https://api.kie.ai/codex/v1
+    custom_llm_provider: openai
+    litellm_credential_name: kie-ai
+    model: openai/responses/gpt-6-1-sol
+  model_info: *id002
 - model_name: gpt-6-luna
   litellm_params:
     use_in_pass_through: false
     use_litellm_proxy: false
     merge_reasoning_content_in_choices: false
     num_retries: 1
-    api_base: https://api.kie.ai/codex/v1
-    custom_llm_provider: openai
-    litellm_credential_name: kie-ai
+    api_base: https://wokey.invalid/v1
+    custom_llm_provider: openai
+    litellm_credential_name: wokey-demo
     model: openai/responses/gpt-6-luna
-  model_info:
+  model_info: &id003
     health_check_max_tokens: 16
     litellm_provider: openai
     mode: chat
@@ -289,6 +311,17 @@
     supports_reasoning: true
     supports_function_calling: true
     supports_prompt_caching: true
+- model_name: __route__gpt-6-luna__kie
+  litellm_params:
+    use_in_pass_through: false
+    use_litellm_proxy: false
+    merge_reasoning_content_in_choices: false
+    num_retries: 1
+    api_base: https://api.kie.ai/codex/v1
+    custom_llm_provider: openai
+    litellm_credential_name: kie-ai
+    model: openai/responses/gpt-6-luna
+  model_info: *id003
 - model_name: deepseek-v4.1-flash
   litellm_params:
     use_in_pass_through: false
@@ -602,9 +635,9 @@
   - gpt-6-astra:
     - openrouter/openai/gpt-6-astra
   - gpt-6.1-sol:
-    - openrouter/openai/gpt-6.1-sol
+    - __route__gpt-6.1-sol__kie
   - gpt-6-luna:
-    - openrouter/openai/gpt-6-luna
+    - __route__gpt-6-luna__kie
   - grok-4.7:
     - openrouter/x-ai/grok-4.7
   - gpt-5.6-terra:
@@ -624,7 +657,7 @@
   - gpt-5.6-luna:
     - openrouter/openai/gpt-5.6-luna
   - gpt-6-sol:
-    - openrouter/openai/gpt-6-sol
+    - __route__gpt-6-sol__kie
   - grok-4.3:
     - openrouter/x-ai/grok-4.3
   - grok-4.5:
OFFLINE ONLY: review model_list/router_settings patch; register named credentials; align reconcile/key/media validators; deploy via existing environment workflow after approval; verify DB route precedence and real fallback. No deployment executed.
```

随后在临时表执行不带 dry-run 的同一命令，status 实际输出：

```text
gpt-6-sol: wokey -> kie
gpt-6.1-sol: wokey -> kie
gpt-6-luna: wokey -> kie
```

### rollback 实际输出

先实际执行 `rollback --dry-run`，文件字节不变，补丁为上述切换补丁的反向；再执行 `rollback`，实际输出如下：

```diff
--- test/before
+++ test/proposed
@@ -224,39 +224,28 @@
     use_litellm_proxy: false
     merge_reasoning_content_in_choices: false
     num_retries: 1
-    api_base: https://wokey.invalid/v1
-    custom_llm_provider: openai
-    litellm_credential_name: wokey-demo
+    api_base: https://api.kie.ai/codex/v1
+    custom_llm_provider: openai
+    litellm_credential_name: kie-ai
     model: openai/responses/gpt-6-sol
-  model_info: &id001
+  model_info:
     health_check_max_tokens: 16
     litellm_provider: openai
     input_cost_per_token: 2.0e-06
     output_cost_per_token: 1.0e-05
     cache_read_input_token_cost: 2.0e-07
     cache_creation_input_token_cost: 2.5e-06
-- model_name: __route__gpt-6-sol__kie
-  litellm_params:
-    use_in_pass_through: false
-    use_litellm_proxy: false
-    merge_reasoning_content_in_choices: false
-    num_retries: 1
-    api_base: https://api.kie.ai/codex/v1
-    custom_llm_provider: openai
-    litellm_credential_name: kie-ai
-    model: openai/responses/gpt-6-sol
-  model_info: *id001
 - model_name: gpt-6.1-sol
   litellm_params:
     use_in_pass_through: false
     use_litellm_proxy: false
     merge_reasoning_content_in_choices: false
     num_retries: 1
-    api_base: https://wokey.invalid/v1
-    custom_llm_provider: openai
-    litellm_credential_name: wokey-demo
+    api_base: https://api.kie.ai/codex/v1
+    custom_llm_provider: openai
+    litellm_credential_name: kie-ai
     model: openai/responses/gpt-6-1-sol
-  model_info: &id002
+  model_info:
     health_check_max_tokens: 16
     litellm_provider: openai
     mode: chat
@@ -274,28 +263,17 @@
     supports_function_calling: true
     supports_prompt_caching: true
     max_tokens: 128000
-- model_name: __route__gpt-6.1-sol__kie
-  litellm_params:
-    use_in_pass_through: false
-    use_litellm_proxy: false
-    merge_reasoning_content_in_choices: false
-    num_retries: 1
-    api_base: https://api.kie.ai/codex/v1
-    custom_llm_provider: openai
-    litellm_credential_name: kie-ai
-    model: openai/responses/gpt-6-1-sol
-  model_info: *id002
 - model_name: gpt-6-luna
   litellm_params:
     use_in_pass_through: false
     use_litellm_proxy: false
     merge_reasoning_content_in_choices: false
     num_retries: 1
-    api_base: https://wokey.invalid/v1
-    custom_llm_provider: openai
-    litellm_credential_name: wokey-demo
+    api_base: https://api.kie.ai/codex/v1
+    custom_llm_provider: openai
+    litellm_credential_name: kie-ai
     model: openai/responses/gpt-6-luna
-  model_info: &id003
+  model_info:
     health_check_max_tokens: 16
     litellm_provider: openai
     mode: chat
@@ -311,17 +289,6 @@
     supports_reasoning: true
     supports_function_calling: true
     supports_prompt_caching: true
-- model_name: __route__gpt-6-luna__kie
-  litellm_params:
-    use_in_pass_through: false
-    use_litellm_proxy: false
-    merge_reasoning_content_in_choices: false
-    num_retries: 1
-    api_base: https://api.kie.ai/codex/v1
-    custom_llm_provider: openai
-    litellm_credential_name: kie-ai
-    model: openai/responses/gpt-6-luna
-  model_info: *id003
 - model_name: deepseek-v4.1-flash
   litellm_params:
     use_in_pass_through: false
@@ -635,9 +602,9 @@
   - gpt-6-astra:
     - openrouter/openai/gpt-6-astra
   - gpt-6.1-sol:
-    - __route__gpt-6.1-sol__kie
+    - openrouter/openai/gpt-6.1-sol
   - gpt-6-luna:
-    - __route__gpt-6-luna__kie
+    - openrouter/openai/gpt-6-luna
   - grok-4.7:
     - openrouter/x-ai/grok-4.7
   - gpt-5.6-terra:
@@ -657,7 +624,7 @@
   - gpt-5.6-luna:
     - openrouter/openai/gpt-5.6-luna
   - gpt-6-sol:
-    - __route__gpt-6-sol__kie
+    - openrouter/openai/gpt-6-sol
   - grok-4.3:
     - openrouter/x-ai/grok-4.3
   - grok-4.5:
OFFLINE ONLY: review model_list/router_settings patch; register named credentials; align reconcile/key/media validators; deploy via existing environment workflow after approval; verify DB route precedence and real fallback. No deployment executed.
Local catalog updated; gateway unchanged.
```

```text
rollback: catalog equals original (structured JSON); render equals original; history consumed
PROD switch + rollback: passed; TEST table unchanged (unit test)
```

### 占用文件及 origin 分支头未变

开始和报告生成时只读 `git ls-remote origin refs/heads/openrouter-fix-20260326`：

```text
d9ddddd74e552c5d6e662c4d645c0cbfc7d7db1f  refs/heads/openrouter-fix-20260326
local origin tracking ref: d9ddddd74e552c5d6e662c4d645c0cbfc7d7db1f
occupied paths vs origin baseline: empty diff; 13 files byte-identical
```

下面 SHA-256 为占用文件白名单内容摘要，不含任何文件正文或凭据；开始/结束一致，同时逐字节与基线 git blob 比较通过：

```text
config/litellm.prod.yaml  8f8befb8075bd85e350dab0fbfec3dcbae6b9bce42bda2e74559b8f70d056b75
config/litellm.test.yaml  74b142e2f7f07dfac735bc91259661778e6e024deacb78b48c0b94bff4acf178
scripts/reconcile_gateway_key_models.py  451854c127984eb7b7fbe5dcca87c60db58ee388e04930b936ec8cdae417fcd8
scripts/reconcile_managed_model_routes.py  9a5413f16ca669c073db003b88787c13470f8c1f13f8806714d264ea5fc662f2
scripts/test_classify_deploy_action_mode.py  9eacfe2f356004c3af0eda93505cb09645d25094a5f3ffb30282721d759ad756
scripts/test_kie_chat_routes.py  0a1837f87f728224e20407549568f98a4a820d5aaebc197ee24a4567fca1cba8
scripts/test_multimedia_config_release.py  1a603033fa91b74f99d791bb3b93cc03d60de078def70fda20873b63e8243675
scripts/test_openrouter_audio_gateway.py  ccc8887fa1a07d97b8562c8c4ff048ab7a0afd92dbdd63890a7f75a47544e528
scripts/test_openrouter_video_routes.py  6644a930feaf1031f2af3ebbca79fac6fccb3ca999c7940bffc1df1d7f37de98
scripts/test_reconcile_gateway_key_models.py  d29485639e20e7f103be243a58c752ae7e607e4f8cfbf4762ff5ef14c4db2371
scripts/test_reconcile_managed_model_routes.py  2e316f7fe3df246761b9fdf24d8628e6b472ee0407feadb18fa6f3a6e646ec27
scripts/test_tool_allowlist_script.py  1acc4114cc4cb30cbc80fb6f42d3d2b7ab5c5c9a93badb25d14255ee834ee54e
scripts/validate_multimedia_config.py  cc665dbf2be2ca0173e6dc5bf4b1eefe484d53e8cd68b049b100b87645ddd769
```

## 偏差

- Wokey 未提供可核验的受管配置，遵照纯离线边界使用 `https://wokey.invalid/v1`、`wokey-demo` 和从 Kie 复制的三个 GPT 模型 ID 作为明确标注的模拟。没有联网寻找平台信息或使用凭据。完成的是迁移机制演示，不声称真实模型 ID、协议或平台可用性已核实。
- DashScope 仅保留未启用的平台目录；DB 路由不导出、不对账，因为连接数据库在本次禁止范围。check 验证表内凭据名称声明存在，不等同验证线上凭据或 secret 存在。
- YAML 往返验收采用结构化等价，不采用文本排版一致：注释、空行、键顺序不会进入渲染片段，但所有列表顺序/字段/参数/metadata 均比较一致。
- 续跑是在本次任务已经创建的原型文件上补测试和报告；“只新增文件”按与 origin 基线的差异核验，未修改任何基线已有文件。
- 本机没有 poetry 命令，现有 python3 可直接加载已安装的 PyYAML，因此用 python3 跑离线 CLI 和单测，不安装或变更环境。
- 不运行 GitHub Issue/自动发布流程，不推进 TEST，也不执行与目标无关的缺陷线索；遵守本 brief 限定的本地分支、不推送边界。

## 未完成/风险

本次离线目标无剩余步骤。真实 Wokey 接入、DB 路由整合、既有脚本改造与网关部署均属于后续集成，必须待网关空出并进入相应任务后执行。本原型本身没有部署入口，默认渲染与基线一致，合并新增文件不会主动改变线上路由。
平台适配器复用已支持的网关协议，不实现新协议；回滚仅一版且无多人写入协调；真实备用触发、DB 覆盖顺序和内部别名授权只能在未来受管环境验证，离线测试不代替该验证。
没有执行任何无关改动。

## 复核

