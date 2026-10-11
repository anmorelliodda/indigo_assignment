# Deploy the backend to Azure

Current deployment recorded by the search-release handoff: `indigoaicr.azurecr.io/indigoai-assignment:search-modes-v1`, API **0.8.0**. This is a narrow patch from final-v2 preserving the deployed SOAP sender, return service and effective runtime bindings. Read-only platform verification on 10 October 2026 confirmed published Production API 0.8.0, identical Test/Production schemas, semantic/hybrid/Jev modes and an "All published" collection status. The configured goal matches the repository source and the four actions are selected. Real returns remain enabled for eligible SOAP-verified order/email pairs. See [search modes](search-modes.md).

These PowerShell commands use existing infrastructure and retain runtime secrets. Git and registry pushes do not deploy automatically. Use a new image tag for each release; do not overwrite a deployed tag.

## 1. Select resources and record rollback configuration

Run from the repository root with Docker Desktop running:

```powershell
Set-Location C:\Users\Andrea\Desktop\code\indigoai_assignment
$Az = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
$Docker = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
$ResourceGroup = "indigoairg"
$App = "indigoai-api"
$Registry = "indigoaicr"
$Tag = "REPLACE_WITH_NEW_RELEASE_TAG"
$Image = "${Registry}.azurecr.io/indigoai-assignment:${Tag}"

& $Az account show --query "{Name:name,Subscription:id}" --output table
if ($LASTEXITCODE -ne 0) { throw "Azure login is required." }

# Confirm the intended subscription before continuing.
$PreviousImage = & $Az containerapp show --name $App --resource-group $ResourceGroup `
  --query "properties.template.containers[0].image" --output tsv
if ($LASTEXITCODE -ne 0) { throw "Target app lookup failed." }
```

Record the previous image and configuration privately. Do not print secrets or customer entities. Image-only updates preserve environment settings, including the real-send gate.

## 2. Test, build and push

All assistant tests must use simulated SOAP/storage. Do not run legacy online diagnostic scripts or call live order/return endpoints, including reads.

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Tests failed. Stop the release." }

& $Docker buildx build --load --platform linux/amd64 --tag $Image .
if ($LASTEXITCODE -ne 0) { throw "Image build failed." }

& $Az acr login --name $Registry
if ($LASTEXITCODE -ne 0) { throw "Registry login failed." }
& $Docker push $Image
if ($LASTEXITCODE -ne 0) { throw "Image push failed." }

& $Az acr repository show --name $Registry `
  --image "indigoai-assignment:${Tag}" --query digest --output tsv
if ($LASTEXITCODE -ne 0) { throw "Image digest lookup failed." }
```

Do not change the global Docker builder. Also run the simulated suite in the built Linux runtime before deploying. Tests and prompt checks require read-only mounts of `tests/` and `docs/`: these files are deliberately absent from the production image.

The `.dockerignore` allowlist includes runtime files, the original catalogue, public `data/search-vectors.json` and `frontend/index.html`. Credentials, tests, documentation and private `assignment/` material stay outside image layers.

## 3. Deploy and inspect configuration

```powershell
& $Az containerapp update --name $App --resource-group $ResourceGroup --image $Image `
  --query "{Provisioning:properties.provisioningState,Image:properties.template.containers[0].image,Revision:properties.latestRevisionName}" --output table
if ($LASTEXITCODE -ne 0) { throw "Container App update failed." }

& $Az containerapp revision list --name $App --resource-group $ResourceGroup `
  --query "[].{Revision:name,Active:properties.active,Health:properties.healthState,Provisioning:properties.provisioningState}" --output table
if ($LASTEXITCODE -ne 0) { throw "Revision inspection failed." }

& $Az containerapp ingress traffic show --name $App --resource-group $ResourceGroup --output table
if ($LASTEXITCODE -ne 0) { throw "Traffic inspection failed." }
```

The pull identity `indigoai-app-pull` has registry-scoped AcrPull. The separate data identity `indigoai-returns-data` accesses Azure Table through Entra; `AZURE_CLIENT_ID` selects it. Never use storage account keys.

Current settings: `RETURNS_ENABLED=true`, `RETURN_SUBMISSION_ENABLED=true`, dedicated `ReturnOperations` table. `RETURN_ALLOWED_ORDER` and `RETURN_ALLOWED_EMAIL` are both absent; a paired restriction is optional. Original SOAP/API secrets remain unchanged. See [returns](returns.md) for configuration and safeguards.

Inspect provisioning, revision health and traffic. In multiple-revision mode, updating an image alone does not prove that all traffic reaches it. Never change traffic blindly.

## 4. Verification and acceptance

Verify the rollout through Azure deployment configuration, without reading or changing customer entities. `/health` reports process readiness, not SOAP availability. The current OpenAPI exposes four operations: `get_catalog_options`, `search_products`, `get_order_status`, `manage_return`.

On 10 October 2026, the native Gift Shop Production schema was inspected in its read-only published view. Its version is 0.8.0 and it is identical to Test. Paths/components match the backend after resolving local `$ref` values and excluding editor-added servers. The search mode enum is semantic/hybrid/Jev, with semantic as default. The agent page shows the four selected actions, a goal matching the repository source, and a recent publication notice. No new workspace Publish was needed or performed for this documentation update. These checks establish configuration/schema alignment, not conversational acceptance; no messages or live product/order/return calls were sent. Backend deployment and platform publication remain separate operations.

Recorded search-release acceptance includes 152 simulated local tests and 154 tests in the final Linux image with networking disabled. The earlier 135-test result belongs to 0.7.0. Andrea supplied Live traces of real creation and cross-chat replay with the same return ID and no new send. The latest return/order status prompt corrections still await user-run verification. Neither test counts nor publication establish full conversational regression or strong buyer authentication.

Only Andrea performs online customer tests and real-send confirmation. Never reset or delete current, submitted or uncertain return records to retest. Three obsolete sandbox tables were privately backed up and deleted on explicit authorization; their three obsolete Container Apps were subsequently deleted, preserving images and current return records. This does not authorize inspecting or deleting other tables.

The service scales to zero, so the first request can be slower. Log streams are replica-specific: reconnect after deployment. Each handled tool call emits two correlated, redacted events at response completion; HTTP responses are not truncated. A CLI `eventStreamEndpoint` error was observed historically and does not establish a backend outage. See [logging](tools.md#console-logging).

## 5. Emergency disable and rollback

**Disable real submission before rollback. Changing only the image does not disarm the current runtime gate.**

```powershell
& $Az containerapp update --name $App --resource-group $ResourceGroup `
  --set-env-vars RETURN_SUBMISSION_ENABLED=false --output none
if ($LASTEXITCODE -ne 0) { throw "Emergency disable failed. Stop and escalate." }

# Confirm the new configuration and active traffic before restoring code.
& $Az containerapp update --name $App --resource-group $ResourceGroup --image $PreviousImage `
  --query "{Provisioning:properties.provisioningState,Image:properties.template.containers[0].image}" --output table
if ($LASTEXITCODE -ne 0) { throw "Rollback failed." }
```

Use the exact privately recorded prior image, not a historical example tag. Inspect configuration, active revisions and traffic again. A schema rollback also requires a compatible platform collection/goal. Do not restore a configuration backup that silently re-enables sending. Code rollback must never erase or roll back durable customer state.

## Deployment versus publication

- Runtime Python/dependencies changed: test, build, push and explicitly update Container Apps.
- Only documentation changed: no image deployment.
- Only agent goals/tools/workflows changed: save, reopen, test and Publish in indigo.ai; no image deployment.
- Backend OpenAPI changed: also reload the collection and reselect actions before testing/publication.

Publish is workspace-wide. Promote the context workflow and its references together; a missing Live hook previously halted routing. A successful save is not proof of transport or conversational correctness.
