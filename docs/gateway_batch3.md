# GLM / Haiku / refusal / JEV TEST batch

## Scope and gates

Only `config/litellm.test.yaml` is opted in. PROD YAML and catalog remain
unchanged. This batch contains gateway code: promotion requires
`build-and-restart`, never config-only restart.

Before TEST use, batch2 must have no deployment lock, its report conclusion
must no longer start with `执行中`, and no batch2 `codex exec resume` process
may be alive. All three conditions are required. Before they hold, local
commits only; no push, deployment or TEST request.

Before applying candidate routes, create bounded TEST candidate deployments
with existing named credentials. Aliyun must accept one natural Flash prompt
on the unchanged private source. Wokey Haiku must correctly describe the same
ordinary travel picture used for Kie Haiku. Never retry a refusal, policy
failure, 403 or denylisted response for that model.

Candidate route decisions use the existing offline compiler:

```bash
python scripts/route_cli.py --env test apply
# Aliyun permission unavailable: select the explicitly prepared alternative.
python scripts/route_cli.py --env test set glm-5.3-flash --primary wokey --fallback openrouter --apply
# Wokey lacks vision: omit it completely from the public Haiku chain.
python scripts/route_cli.py --env test set bedrock-claude-haiku --primary kie --fallback openrouter --apply
```

`db_managed_routes.glm-5.3-flash` copies the immutable
`deepseek-v4-flash` DB-source binding in memory, changing only the new model ID
to `dashscope/ZHIPU/GLM-5.3-Flash`. Existing Aliyun models, credentials and
private connection are never edited. Fingerprint all existing Aliyun
parameters/deployment IDs before and after; do not export decrypted values.

## Refusal contract

Trusted structured `upstream_model_refusal` and HTTP 403 `model_denylisted`
are terminal regardless of retry policy, healthy deployment count or ordinary
and content-policy fallbacks. Native 200 `stop_reason=refusal` is returned
unchanged; chat content_filter roundtrips to messages refusal. OpenAI
message/delta.refusal text and identity are retained. Never classify prose.

The upstream HTTP JSON/SDK body provides a real usage path. Whitelisted
nonnegative integer token counts survive normalized errors and JSON/SSE
serialization in `error.usage` and `provider_specific_fields.usage`.
Missing usage remains missing; no estimate is invented at the gateway.
Ordinary errors retain existing retry/fallback semantics.

## Managed JEV

Generic authenticated passthrough needs a premium license and does not read
named credentials, so a minimal route reuses existing gateway authentication
and `CredentialAccessor`. No enterprise restriction is bypassed.

- Named credential: `typesafe-jev`, values `{api_key: <process-only key>}`.
- POST gateway `/jev/v1/systemone` -> fixed TypeSafe `/v1/systemone`.
- Mounted YAML `jev_passthrough.enabled: true` opts TEST in. Default is 404.
- Original `{state (text or structured projection), model: "jev-latest", questions}` and response/usage pass
  through once. No caller headers, redirect, retry or model fallback.
- Kollab configuration: `JEV_GUARD_BASE_URL=https://llm-test.kollab.im/jev/v1/systemone`.
  The value is the full URL, not a base directory. Authentication reuses
  `AGENTCORE_LLM_GATEWAY_UPSTREAM_API_KEY` (existing managed gateway key).
  No TypeSafe key on Kollab. This task does not enable Kollab's guard.
- Only yes and confidence >=0.8 blocks; no or lower confidence releases.
  Real quality verification sends 20 fixed design examples only to JEV.
  Three repeated-phrase variants use the existing short-fragment rule.

## Production checklist supplement (do not execute in this task)

1. Merge this supplement into batch2's release checklist; keep its previous
   provider credentials, media probes and rollback steps.
2. Provision only missing named provider credentials in production under a
   separate release authorization. Do not copy private source values to disk
   or alter existing Aliyun connections. JEV/TypeSafe remains TEST-only until
   separately authorized; do not enable it on production by this checklist.
3. Verify GLM permission and Haiku picture/text/messages via temporary managed
   candidates before selecting public routes. Preserve original fingerprints.
4. Apply only the separately approved PROD route catalog/YAML. Existing TEST
   changes do not opt PROD in. Build gateway image with `build-and-restart`
   and fixed expected SHA. Read back running image/code and actual route list.
5. Probe GLM chat/messages/tools and Haiku picture/chat, then batch2's approved
   minimal regression aliases. Stop that model on any refusal/policy/403.
6. Confirm code's mocked terminal refusal suite and real usage projection;
   never send refusal-triggering content to a model.
7. Rollback: restore preceding gateway image and preceding PROD YAML/catalog,
   remove only newly created migrated-alias deployments by exact IDs. Never
   delete any pre-existing Aliyun source, credential or private connection.
