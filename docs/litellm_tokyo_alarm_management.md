# LiteLLM Tokyo 告警配置与管理手册

这份文档是给后续 agent / 运维同学用的 runbook，用来查看、修改、扩展当前东京区 LiteLLM 实例的监控和告警。

适用范围：
- AWS 区域：`ap-northeast-1`
- AWS 账号：`766691179306`
- LiteLLM EC2：`i-04ba3dd7ee265426f`
- 实例名：`litellm-tokyo-ec2`

## 当前资源清单

当前已经落地的资源如下：

- EC2 实例：`i-04ba3dd7ee265426f`
- CloudWatch Dashboard：`litellm-tokyo-monitoring`
- SNS Topic：`arn:aws:sns:ap-northeast-1:766691179306:litellm-tokyo-alerts`
- Lambda 函数：`litellm-alarm-to-feishu`
- Lambda 角色：`litellm-alarm-to-feishu-role`
- EC2 CloudWatch Agent 角色权限：`CloudWatchAgentServerPolicy`
- Private hosted zone：`llm.kollab.im.` (`Z0862216BMU2LPHPD7VG`)
- Public hosted zone：`kollab.im.` (`Z0670501PRO3U4KXNDG6`)

当前已有告警：

- `litellm-tokyo-ec2-cpu-high`
- `litellm-tokyo-ec2-memory-high`
- `litellm-tokyo-ec2-disk-root-high`
- `litellm-tokyo-ec2-status-check-failed`

当前告警接收链路：

- CloudWatch Alarm
- SNS Topic `litellm-tokyo-alerts`
- 邮箱订阅
- Feishu webhook 通过 Lambda 转发

## 当前访问模型

当前 LiteLLM 已经收口为纯内网访问：

- 访问域名：`https://llm.kollab.im`
- 私有 DNS：`llm.kollab.im -> 172.31.44.132`
- 私有 hosted zone：仅关联 `vpc-04fa7231cc5c88f9b`
- 公网 security group：已关闭
- 公网 Route53 `A` 记录：保留，但返回私网 IP `172.31.44.132`

当前允许访问的来源：

- 东京账户默认 VPC 内部网段：`172.31.0.0/16`
- 员工通过堡垒机 VPN 访问

VPN 路由现状：

- 堡垒机：`agentcore-tokyo-bastion` (`i-0ca129f97bdbfe9f2`)
- OpenVPN 客户端网段：`10.88.0.0/24`
- 堡垒机会把 VPN 流量 NAT 成自身 VPC 地址后再访问内网
- OpenVPN 只下发内网路由 `172.31.0.0/16`，不再强制下发 AWS DNS `172.31.0.2`
- 因此 LiteLLM 安全组只放行 `172.31.0.0/16` 即可，VPN 用户也能访问，而且不会影响其他网站的 DNS 解析

VPN 配置变更说明：

- 如果刚改过 OpenVPN 配置，已经在线的客户端需要断开重连一次，才能拿到新的 DNS / 路由行为

## 当前阈值

当前基线阈值如下：

- CPU：`>= 70%`，连续 `15` 分钟
- 内存：`>= 80%`，连续 `15` 分钟
- 根盘使用率：`>= 85%`，连续 `15` 分钟
- EC2 状态检查失败：连续 `2` 分钟

## 告警架构

```text
CloudWatch Alarm
  -> SNS topic: litellm-tokyo-alerts
      -> Email subscription
      -> Lambda: litellm-alarm-to-feishu
          -> Feishu bot webhook
```

## 操作前提

建议所有命令都带这一段前缀，避免本机代理影响 AWS CLI：

```bash
env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY
```

下文如果没特别说明，默认都在仓库根目录执行：

```bash
cd /Users/caokeji/code/litellm
```

建议先确认当前 AWS 身份：

```bash
env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws sts get-caller-identity
```

## 日常查看

### 1. 看当前告警状态

```bash
env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws cloudwatch describe-alarms \
  --region ap-northeast-1 \
  --alarm-name-prefix litellm-tokyo-ec2- \
  --query 'MetricAlarms[].{Name:AlarmName,State:StateValue,Reason:StateReason}' \
  --output table
```

### 2. 看 Dashboard

控制台：

- `CloudWatch -> Dashboards -> litellm-tokyo-monitoring`

CLI：

```bash
env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws cloudwatch get-dashboard \
  --region ap-northeast-1 \
  --dashboard-name litellm-tokyo-monitoring
```

### 3. 看 SNS 订阅状态

```bash
TOPIC_ARN=arn:aws:sns:ap-northeast-1:766691179306:litellm-tokyo-alerts

env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws sns list-subscriptions-by-topic \
  --region ap-northeast-1 \
  --topic-arn "$TOPIC_ARN"
```

如果邮箱显示 `PendingConfirmation`，说明用户还没点确认邮件。

## 新增或修改邮箱告警

### 新增邮箱订阅

```bash
TOPIC_ARN=arn:aws:sns:ap-northeast-1:766691179306:litellm-tokyo-alerts
EMAIL="<new_email@example.com>"

env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws sns subscribe \
  --region ap-northeast-1 \
  --topic-arn "$TOPIC_ARN" \
  --protocol email \
  --notification-endpoint "$EMAIL"
```

注意：

- 新邮箱一定会先进入 `PendingConfirmation`
- 必须去收件箱点 AWS SNS 发出的确认链接

### 删除邮箱订阅

先查出订阅 ARN：

```bash
TOPIC_ARN=arn:aws:sns:ap-northeast-1:766691179306:litellm-tokyo-alerts

env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws sns list-subscriptions-by-topic \
  --region ap-northeast-1 \
  --topic-arn "$TOPIC_ARN"
```

然后删除：

```bash
SUB_ARN="<subscription_arn>"

env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws sns unsubscribe \
  --region ap-northeast-1 \
  --subscription-arn "$SUB_ARN"
```

## 修改 Feishu webhook

不要把新的 webhook 明文写进仓库。正确做法是只更新 Lambda 环境变量。

### 查看当前函数配置

```bash
env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws lambda get-function-configuration \
  --region ap-northeast-1 \
  --function-name litellm-alarm-to-feishu
```

### 更新 webhook

```bash
NEW_WEBHOOK_URL="<feishu_webhook_url>"

env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws lambda update-function-configuration \
  --region ap-northeast-1 \
  --function-name litellm-alarm-to-feishu \
  --environment "Variables={FEISHU_WEBHOOK_URL=$NEW_WEBHOOK_URL}"
```

如果后续函数还需要其他环境变量，先把原值读出来再一起回写，避免覆盖。

## 新增告警

原则：

- 告警名统一用前缀 `litellm-tokyo-ec2-`
- 所有告警都挂到同一个 SNS Topic
- 如果是主机指标，优先复用 CloudWatch / CWAgent 已有指标

统一使用：

```bash
TOPIC_ARN=arn:aws:sns:ap-northeast-1:766691179306:litellm-tokyo-alerts
INSTANCE_ID=i-04ba3dd7ee265426f
REGION=ap-northeast-1
```

### 示例 1：新增更严格的 CPU 告警

```bash
env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws cloudwatch put-metric-alarm \
  --region "$REGION" \
  --alarm-name litellm-tokyo-ec2-cpu-warn \
  --alarm-description 'LiteLLM EC2 CPU average >= 50% for 15 minutes' \
  --namespace AWS/EC2 \
  --metric-name CPUUtilization \
  --dimensions Name=InstanceId,Value="$INSTANCE_ID" \
  --statistic Average \
  --period 300 \
  --evaluation-periods 3 \
  --datapoints-to-alarm 3 \
  --threshold 50 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions "$TOPIC_ARN"
```

### 示例 2：新增内存告警

当前机器的 `CWAgent` 指标维度是：

- `InstanceId=i-04ba3dd7ee265426f`
- `ImageId=ami-088b486f20fab3f0e`
- `InstanceType=t3.xlarge`

命令示例：

```bash
env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws cloudwatch put-metric-alarm \
  --region "$REGION" \
  --alarm-name litellm-tokyo-ec2-memory-warn \
  --alarm-description 'LiteLLM EC2 memory used >= 70% for 15 minutes' \
  --namespace CWAgent \
  --metric-name mem_used_percent \
  --dimensions \
    Name=InstanceId,Value=i-04ba3dd7ee265426f \
    Name=ImageId,Value=ami-088b486f20fab3f0e \
    Name=InstanceType,Value=t3.xlarge \
  --statistic Average \
  --period 300 \
  --evaluation-periods 3 \
  --datapoints-to-alarm 3 \
  --threshold 70 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions "$TOPIC_ARN"
```

## 修改已有告警阈值

CloudWatch 的 `put-metric-alarm` 是覆盖更新，不需要先删除。

例如把当前 CPU 告警从 `70%` 改成 `60%`：

```bash
TOPIC_ARN=arn:aws:sns:ap-northeast-1:766691179306:litellm-tokyo-alerts

env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws cloudwatch put-metric-alarm \
  --region ap-northeast-1 \
  --alarm-name litellm-tokyo-ec2-cpu-high \
  --alarm-description 'LiteLLM EC2 CPU average >= 60% for 15 minutes' \
  --namespace AWS/EC2 \
  --metric-name CPUUtilization \
  --dimensions Name=InstanceId,Value=i-04ba3dd7ee265426f \
  --statistic Average \
  --period 300 \
  --evaluation-periods 3 \
  --datapoints-to-alarm 3 \
  --threshold 60 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions "$TOPIC_ARN"
```

## 删除告警

```bash
env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws cloudwatch delete-alarms \
  --region ap-northeast-1 \
  --alarm-names litellm-tokyo-ec2-cpu-warn
```

## 测试整条告警链路

### 1. 通过 SNS 发一条测试告警

```bash
TOPIC_ARN=arn:aws:sns:ap-northeast-1:766691179306:litellm-tokyo-alerts

cat > /tmp/litellm-test-message.json <<'JSON'
{
  "AlarmName": "litellm-tokyo-test-notification",
  "NewStateValue": "ALARM",
  "NewStateReason": "Manual test notification.",
  "Region": "ap-northeast-1",
  "AWSAccountId": "766691179306",
  "StateChangeTime": "2026-03-31T03:00:00Z"
}
JSON

env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws sns publish \
  --region ap-northeast-1 \
  --topic-arn "$TOPIC_ARN" \
  --subject 'LiteLLM Alarm Test' \
  --message file:///tmp/litellm-test-message.json
```

这会同时走：

- SNS -> 邮件
- SNS -> Lambda -> Feishu

### 2. 直接测试 Feishu 转发函数

```bash
cat > /tmp/litellm-feishu-lambda-event.json <<'JSON'
{
  "Records": [
    {
      "Sns": {
        "Subject": "LiteLLM Alarm Test Direct Invoke",
        "Message": "{\"AlarmName\":\"litellm-tokyo-test-notification-direct\",\"NewStateValue\":\"ALARM\",\"NewStateReason\":\"Direct Lambda invoke test for Feishu delivery.\",\"Region\":\"ap-northeast-1\",\"AWSAccountId\":\"766691179306\",\"StateChangeTime\":\"2026-03-31T03:05:00Z\"}"
      }
    }
  ]
}
JSON

env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws lambda invoke \
  --region ap-northeast-1 \
  --function-name litellm-alarm-to-feishu \
  --payload fileb:///tmp/litellm-feishu-lambda-event.json \
  /tmp/litellm-feishu-lambda-response.json

cat /tmp/litellm-feishu-lambda-response.json
```

成功时返回里会看到类似：

```json
{"ok": true, "results": [{"status": 200, "response": "{\"code\":0,\"msg\":\"success\"}"}]}
```

## 查看 Feishu 转发日志

```bash
env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws logs describe-log-streams \
  --region ap-northeast-1 \
  --log-group-name /aws/lambda/litellm-alarm-to-feishu \
  --order-by LastEventTime \
  --descending \
  --max-items 1
```

然后读取日志：

```bash
LOG_STREAM="<log_stream_name>"

env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws logs get-log-events \
  --region ap-northeast-1 \
  --log-group-name /aws/lambda/litellm-alarm-to-feishu \
  --log-stream-name "$LOG_STREAM" \
  --limit 50
```

## CloudWatch Agent 管理

这台 EC2 已经安装并启动 `amazon-cloudwatch-agent`，目前上报：

- `mem_used_percent`
- `mem_available_percent`
- `disk_used_percent`
- `swap_used_percent`

### 通过 SSM 检查 agent 状态

```bash
cat > /tmp/litellm_cwagent_probe.json <<'JSON'
{
  "commands": [
    "rpm -q amazon-cloudwatch-agent || true",
    "systemctl status amazon-cloudwatch-agent --no-pager || true",
    "cat /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json || true"
  ]
}
JSON

env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws ssm send-command \
  --region ap-northeast-1 \
  --instance-ids i-04ba3dd7ee265426f \
  --document-name AWS-RunShellScript \
  --parameters file:///tmp/litellm_cwagent_probe.json
```

### 替换实例后必须做的事

如果 LiteLLM EC2 换机器了，务必同步做这几件事：

1. 给新实例角色挂上 `CloudWatchAgentServerPolicy`
2. 安装并启动 `amazon-cloudwatch-agent`
3. 确认 `CWAgent` 指标已开始上报
4. 更新告警和 Dashboard 中的 `InstanceId`
5. 如果 `ImageId` / `InstanceType` / 磁盘维度变化了，更新内存和磁盘告警的 dimensions

## 常见问题

### 1. 邮箱收不到告警

先看 SNS 订阅：

- 如果是 `PendingConfirmation`，说明邮箱没点确认
- 如果已确认但没收到，先发一条 SNS 测试消息

### 2. 告警一直是 `INSUFFICIENT_DATA`

常见原因：

- 刚创建，CloudWatch 还没攒够数据点
- `CWAgent` 指标还没开始上报
- dimensions 写错了，尤其是 `ImageId`、`InstanceType`、`path`、`device`、`fstype`

先用这个查指标：

```bash
env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY \
aws cloudwatch list-metrics \
  --region ap-northeast-1 \
  --namespace CWAgent \
  --dimensions Name=InstanceId,Value=i-04ba3dd7ee265426f
```

### 3. 飞书没收到消息

排查顺序：

1. SNS topic 是否已订阅 Lambda
2. Lambda 是否有最新 webhook
3. Lambda 日志是否报错
4. 直接 `aws lambda invoke` 看返回是否 `code: 0`

### 4. 需要新增更多业务告警

优先考虑：

- LiteLLM 容器不可用
- `/health/liveliness` 连续失败
- 5xx 错误率升高
- 响应延迟异常

这类告警通常需要额外埋点或外部 health check，不属于当前这套“纯 EC2 / CWAgent 主机告警”的范围。

## 建议约定

后续 agent 修改时尽量遵守：

- 保持区域固定为 `ap-northeast-1`
- 告警命名统一前缀：`litellm-tokyo-ec2-`
- 所有告警统一挂到 `litellm-tokyo-alerts`
- 不把邮箱、Webhook、密钥硬编码进仓库
- 变更后都补一次测试：至少发一次 SNS 测试消息

## 变更后最小验证清单

每次修改后，至少检查：

1. `describe-alarms` 能看到目标告警
2. `list-subscriptions-by-topic` 能看到目标订阅
3. 发一次 SNS 测试消息
4. 飞书 webhook 直调或 Lambda 直调成功
5. 如改了邮箱，确认邮箱订阅不是 `PendingConfirmation`
