# messages 工具选择修复：TEST 部署后验收

本变更只修转换，不包含部署、配置/数据库修改或真实付费请求。

## 离线验证

使用 pytest 禁网执行 messages adapter、Responses adapter 和 Kie 合同测试。
导入 LiteLLM 前设置 `LITELLM_LOCAL_MODEL_COST_MAP=True`，禁用网络 socket；
仅保留 asyncio 内部 UNIX socket。新增 `test_tool_choice_contract.py` 有 52 个用例。

封网运行器（保存到工作区外的 run_offline.py）：

```python
import os
import sys
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
import pytest_socket
pytest_socket.disable_socket(allow_unix_socket=True)
sys.path.insert(0, os.getcwd())
import pytest
raise SystemExit(pytest.main([
    "-p", "pytest_asyncio.plugin", "-p", "pytest_socket",
    "--disable-socket", "--allow-unix-socket", *sys.argv[1:]
]))
```

在本 worktree 下执行；uv 仅复用缓存、不联网、不改依赖清单：

```sh
uv run --offline --no-project --with litellm --with pytest --with pytest-asyncio \
  --with pytest-socket --with pyyaml --with fastapi --with orjson \
  --with backoff --with cryptography python /absolute/path/run_offline.py \
  tests/test_litellm/llms/anthropic/experimental_pass_through \
  tests/test_litellm/llms/openai/responses/test_openai_responses_transformation.py \
  tests/litellm/llms/anthropic/test_kie_messages.py \
  scripts/test_kie_chat_routes.py -q
```


转换：auto → `"auto"`；any → `"required"`；none → `"none"`。
指定工具在 Responses 使用 `{type:function,name:ping}`，chat 使用
`{type:function,function:{name:ping}}`。
显式 `disable_parallel_tool_use` 取反为顶层 `parallel_tool_calls`；缺省时不发送。
同步、异步及流式共用这两条请求转换路径。

## TEST 换版之后（本任务不执行）

1. 经既有发布契约换成包含本地修复 commit 的 TEST 网关镜像，确认运行版本。
2. 仅在 TEST 的 `wokey-gpt-6.1-sol`、`wokey-grok-4.7` 部署上移除
   `additional_drop_params` 中的 `tool_choice`，其他项保留。先保存非秘密参数快照，再回读。
3. 从 TEST 网关容器发 `/v1/messages` 请求，使用既有受管主认证；不调用 provider endpoint，
   不使用业务凭据，不打印认证头。关闭 retry/fallback，避免成功结果掩盖真实供应商。
4. 两模型分别跑下面四种请求，各补 `stream:true`。再补
   `tool_choice.disable_parallel_tool_use:true` 的四种选择，检查出站 parallel_tool_calls=false。
5. 每项记录 HTTP 状态、实际上游模型/路由和不含认证信息的出站请求体。
   auto 必须传字符串 auto（模型可选工具或文本，不能把它当强制）；any 必须产生工具调用；
   tool 必须产生 ping；none 必须不产生工具调用。工具调用检查 value=OK；
   流式检查 tool_use/text 增量和 message_stop，不能仅凭 HTTP 200 判通过。
6. 回归 Kie `/codex/v1/responses`：URL、model、tools、input、usage、流式终止契约保留；
   messages 的选择参数按上述正确形式发送，原生 chat/responses 已有合法请求行为应保持。
   关联实际供应商路由及计费回执；失败时不要无限重复付费。

以下请求体均可直接用于 `/v1/messages`；两模型各跑一次。保留相同 messages/tools，
四次分别替换 tool_choice 为下面任一值。

```json
{
  "model": "wokey-gpt-6.1-sol",
  "max_tokens": 512,
  "stream": false,
  "messages": [{"role":"user","content":"Call ping with value OK. If tools are forbidden, reply with the text OK."}],
  "tools": [{"name":"ping","description":"Echo a value","input_schema":{"type":"object","properties":{"value":{"type":"string"}},"required":["value"],"additionalProperties":false}}],
  "tool_choice": {"type":"auto"},
  "num_retries": 0,
  "disable_fallbacks": true
}
```

| 请求 | tool_choice | Responses 出站 | chat 出站 |
|---|---|---|---|
| auto | `{"type":"auto"}` | `"auto"` | `"auto"` |
| any | `{"type":"any"}` | `"required"` | `"required"` |
| tool | `{"type":"tool","name":"ping"}` | `{"type":"function","name":"ping"}` | `{"type":"function","function":{"name":"ping"}}` |
| none | `{"type":"none"}` | `"none"` | `"none"` |

第二模型把 model 改为 `wokey-grok-4.7`。流式把 stream 改为 true。
认证和 TEST 网关地址通过已受控的容器进程配置注入，禁止写入请求体或提交文件。

## 并回最新基线

获取最新 origin/openrouter-fix-20260326，只把本地修复 commit cherry-pick 到
协调会话批准的集成分支。先比较这四个源码/测试文件与本文件是否被同时改动，
必要时解决语义冲突；重跑同一离线检查，再按发布会话统一处理 TEST 部署。
本修复不提交 config、scripts、Kollab origin/test/main 或发布工作流。
