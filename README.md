# J&J Collibra DQ Chatbot

An AI-assisted operations tool for inspecting, diagnosing, and maintaining J&J Collibra Data Quality (CDQ) dataset definitions. The repository combines a local Gradio chat application, a Strands agent, a custom J&J GenAI Gateway model provider, direct Collibra DQ REST tools, Redshift inspection helpers, curated business-unit lookups, and optional Atlassian MCP tools.

The primary application is intended for a small group of Collibra DQ super-users. It can read live configuration and, after an explicit preview-and-confirm interaction, update dataset definitions, alerts, and business-unit assignments.

## Capabilities

### Collibra DQ inspection

The local agent can query either the `apac` or `cn` CDQ deployment to:

- List datasets, optionally filtered by name.
- Retrieve a complete `DatasetDef`, including load configuration, metadata tags, schedule, profiling, duplicate checks, and Spark settings.
- Retrieve the separate alert configuration for a dataset.
- Resolve a dataset's business-unit mapping and parse its market, project, and CDE status.
- List available business units, active DQ rules, and the live metaTag catalog.
- Retrieve DQ findings for a dataset and run date, and check whether a single rule passed on a given run (`validate_rule_run`) after a `ruleValue` change.

### Dataset and metadata assistance

The assistant provides read-only helpers that ground recommendations in repository and platform data:

- Suggest a compliant dataset name for a Redshift table or S3 file.
- Probe both configured Redshift clusters to determine whether a table is on the `local` or `region` cluster.
- Sample Redshift column names and rows with a bounded `SELECT ... LIMIT` query.
- Search Redshift table and column names by keyword across both clusters, to find the physical `schema.table`/join column backing a cross-reference before it has a CDQ dataset.
- Run a single, wrapped and row-capped read-only `SELECT` against Redshift to troubleshoot a candidate custom-rule query before saving it.
- List objects under an S3 bucket/prefix and sample a CSV file's header and rows via a ranged, size-capped `GET` -- the S3 equivalent of Redshift sampling, so new S3-backed datasets can be grounded in real file content.
- Check whether candidate `linkId` columns uniquely identify Redshift rows and return duplicate examples.
- Find similar tagged datasets in `public.dqm_business_unit_mapping` and summarize Data Domain and sub-domain usage.
- Show Data Domain distribution across the curated mapping table.
- Suggest a business unit from schema, table, and dataset naming conventions while showing available projects for user review.
- Suggest Data Domain and Sub-Domain values from the fixed taxonomy, current DatasetDef, business-unit mapping, similar datasets, and optional table samples.
- List Collibra's template rule catalog (`GET /v2/templateRules`) to find a suitable template before building a template-backed custom rule.

### Controlled write operations

Writes use a mandatory two-step workflow:

1. A `propose_*` tool creates a preview, diff or summary, and short-lived `change_id`.
2. The user reviews the proposed operation and explicitly confirms it.
3. `apply_dataset_change` executes the previously proposed operation.

Pending changes are held in process memory and expire after 15 minutes. Applying a change consumes its `change_id`, so stale or reused proposals are rejected.

Supported write workflows include:

- Update DatasetDef metadata tags, schedule time, job description, or `linkId`.
- Enable/disable a dataset's modifiable profile checks (row/null/empty/time/min/max/mean/unique/string-length); a fixed set of Collibra UI defaults can never be changed.
- Create a new Redshift- or S3-backed dataset from a known-good reference template.
- Create or update a single custom DQ rule for a dataset (`POST /v3/rules`, matched by `ruleNm`), including renaming one (no rename endpoint exists, so this deletes the old rule and creates the new one under a fresh name).
- Configure the standard `Low Dataset Score` email alert.
- Create or update a business-unit definition and attach it to a dataset.
- Trigger a job run after a DatasetDef update, custom rule change, or new dataset creation.

New dataset creation applies repository-defined defaults: a Monday-Friday daily schedule in `Asia/Singapore`, standard Spark sizing, selected profiling checks, shape/outlier/pattern layers disabled, and duplicate checking enabled for Redshift datasets with a supplied `linkId`. The tool reports best-practice violations before anything is written.

New custom rules are checked against a naming/typing convention (`columnName` set when the rule targets a column; `ruleNm` starting with `if_{columnName}_...`; one of three `ruleType`/`ruleRepo`/`ruleValue` combinations for template, full-SQL, or condition-only rules) -- reported as non-blocking `best_practice_issues`, never re-flagged when only updating or renaming an existing rule. When a rule change alters `ruleValue`, the proposal reports `requires_run_validation=True`; after applying, `validate_rule_run` fetches the triggered job's findings (`GET /v3/jobs/{dataset}/{run_date}/findings`) and reports whether the rule passed (`breakMsg`/`score`/`exception`) once the job has finished. Renaming a rule or editing only its dimension/description/purpose does not require that validation.

Every applied change is written immediately to the PostgreSQL change-history audit table and queued for Jira. A separate `sync_jira_change_request` tool (called once per dataset, after the user confirms, with the dataset's market so the ticket is attached to the right Jira version) creates or reopens a "DQ Change Request" ticket under a pre-configured epic, stacks the new entries on top of its existing description (long `ruleValue`/SQL diffs are rendered as a compact unified diff instead of the full before/after text), and closes it again. See `collibra_dq_app/jira_logger.py`.

### Atlassian investigation tools

The optional Atlassian MCP integration connects the Strands agent to Jira, Confluence, and Bitbucket through the configured streamable HTTP MCP endpoint. It is useful for gathering evidence about JGPV data-quality tickets and JEJQ upstream ETL tickets. The local chatbot continues with Collibra tools if the Atlassian MCP client cannot be initialized.

The standalone `strands_agent.py` factory is also available for one-shot Jira-oriented prompts or reusable agent sessions.

## Architecture

```text
Gradio app (collibra_dq_app/app.py)
        |
        v
Strands Agent (collibra_dq_app/dq_agent.py)
        |
        +--> JNJClaudeGatewayModel (jnj_strands_model.py)
        +--> Local Collibra tools (collibra_dq_app/collibra_tools.py)
        |       +--> CollibraDQClient -> Collibra CDQ REST API
        |       +--> Redshift helpers
        |       +--> PostgreSQL business-unit reference lookups
        |
        +--> Optional Atlassian MCP client (strands_agent.py)
```

## Repository layout

| Path | Responsibility |
| --- | --- |
| `collibra_dq_app/app.py` | Local Gradio UI, super-user login, streaming activity display, and agent lifecycle. |
| `collibra_dq_app/dq_agent.py` | Builds an agent with local Collibra tools and optional MCP clients. |
| `collibra_dq_app/collibra_tools.py` | Strands tool definitions, read/write workflows, previews, and confirmation enforcement. |
| `collibra_dq_app/collibra_dq_client.py` | Low-level authenticated Collibra CDQ REST client. |
| `collibra_dq_app/dataset_builder.py` | DatasetDef cloning, source-specific payload construction, validation, and alert defaults. |
| `collibra_dq_app/redshift_connections.py` | Static connection registry, cluster detection, S3 parsing, table sampling, and key uniqueness checks. |
| `collibra_dq_app/business_unit.py` | Business-unit hierarchy parsing and market/project inference. |
| `collibra_dq_app/bu_mapping_reference.py` | Similarity-ranked lookups against the curated PostgreSQL mapping table. |
| `collibra_dq_app/chat_store.py` | PostgreSQL persistence for chat history (`dqm_chatbot_chat_history`, resumable on next login) and applied-change audit trail (`dqm_chatbot_change_history`). |
| `collibra_dq_app/jira_logger.py` | Creates/reopens/closes Jira "DQ Change Request" tickets, stacking new change-history entries on the existing description and attaching the ticket to the dataset's market via a Jira version. |
| `collibra_dq_app/agent_instruction.txt` | Production system instructions, best-practice guidance, and hard safety rules for the local agent. |
| `jnj_strands_model.py` | Strands-compatible provider for the J&J GenAI Gateway, including native tool calling and structured-output fallback. |
| `strands_agent.py` | Standalone agent factory for Atlassian MCP workflows. |
| `token_manager.py` | Collibra token acquisition, in-memory caching, expiry buffer, and 401 refresh. |
| `postgres_io.py` | Reusable PostgreSQL and pandas read/write helpers. |
| `architecture.drawio` | Architecture diagram source. |
| `collibra_dq_app/requirements.txt` | Python dependencies for the local application. |

## Prerequisites

- Python 3.10 or newer is recommended because the code uses modern type-hint syntax.
- Network access to the J&J GenAI Gateway, Collibra CDQ, and any Redshift/PostgreSQL services used by the selected workflow.
- A J&J GenAI Gateway API key.
- Collibra CDQ credentials for every region the chatbot must access.
- Redshift credentials only when sampling tables, probing clusters, or checking `linkId` uniqueness.
- PostgreSQL credentials only when using similar-dataset or Data Domain distribution lookups.
- PostgreSQL credentials are also required to persist chat history and the change-history audit trail (`collibra_dq_app/chat_store.py`) -- run `python collibra_dq_app/chat_store.py` once to create the tables.
- AWS credentials (read-only S3 permissions) only when inspecting S3 files via `list_s3_objects`/`sample_s3_file`.
- Atlassian Jira credentials (`X_ATLASSIAN_JIRA_URL`/`X_ATLASSIAN_JIRA_PERSONAL_TOKEN`) are also used directly (outside the MCP integration) by `jira_logger.py` to log/close DQ Change Request tickets.
- Atlassian credentials only when enabling the MCP integration.

## Installation

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r collibra_dq_app/requirements.txt
```

The repository does not currently include a `.env.example`. Create a local `.env` file or export variables in the shell. `.env` is ignored by Git.

## Configuration

### Required for the local chatbot

```bash
export SUPERUSER_CREDENTIALS='alice:strong-password,bob:another-password'
export JNJ_GENAI_API_KEY='...'

export CDQ_BASE_URL_APAC='https://your-apac-cdq-host'
export CDQ_USERNAME_APAC='...'
export CDQ_PASSWORD_APAC='...'

# Required if the CN deployment will be used.
export CDQ_BASE_URL_CN='https://your-cn-cdq-host'
export CDQ_USERNAME_CN='...'
export CDQ_PASSWORD_CN='...'
```

`SUPERUSER_CREDENTIALS` is a comma-separated `username:password` allowlist used by the local UI. If it is missing, login is disabled. This is an application-level gate, not a replacement for network controls or enterprise identity management.

### Optional Redshift access

```bash
export REDSHIFT_USER='...'
export REDSHIFT_PASSWORD='...'
export REDSHIFT_DBNAME='idiscover'

# Override the built-in cluster hosts when required.
export REDSHIFT_CONN1_HOST='...'
export REDSHIFT_CONN2_HOST='...'
```

The connection registry maps names containing `local` and `region` to the two configured hosts. S3 connection names are resolved from the static registry in `redshift_connections.py`.

### Optional S3 file inspection

```bash
export AWS_ACCESS_KEY_ID='...'
export AWS_SECRET_ACCESS_KEY='...'
export AWS_DEFAULT_REGION='ap-southeast-1'
```

Used by `boto3` for `list_s3_objects`/`sample_s3_file`. Grant this key read-only S3 permissions only -- the app has no upload/overwrite/delete tool, and the agent instructions explicitly refuse such requests.

### Optional PostgreSQL reference access

```bash
export DB_HOST='...'
export DB_PORT='5432'
export DB_NAME='...'
export DB_USER='...'
export DB_PASSWORD='...'
export DB_SSLMODE='require'
export DQM_BU_MAPPING_TABLE='public.dqm_business_unit_mapping'
export DQM_CHAT_HISTORY_TABLE='public.dqm_chatbot_chat_history'
export DQM_CHANGE_HISTORY_TABLE='public.dqm_chatbot_change_history'
```

These variables are used for curated similarity/distribution lookups and for the chatbot's own
audit tables. The reference table is read into a process-level pandas cache after its first use.
Run `python collibra_dq_app/chat_store.py` once (with DB_* set) to create `dqm_chatbot_chat_history`
and `dqm_chatbot_change_history` if they don't already exist.

### Optional Atlassian MCP access

```bash
export MCP_ATLASSIAN_URL='https://atlassian-mcp.xena.dev/mcp/'
export X_ATLASSIAN_JIRA_URL='https://your-company.atlassian.net'
export X_ATLASSIAN_JIRA_PERSONAL_TOKEN='...'
export X_ATLASSIAN_USERNAME='...'
export X_ATLASSIAN_READ_ONLY_MODE='true'
export X_ATLASSIAN_ENABLE_XRAY='false'

# Optional service headers.
export X_ATLASSIAN_CONFLUENCE_URL='...'
export X_ATLASSIAN_CONFLUENCE_PERSONAL_TOKEN='...'
export X_ATLASSIAN_BITBUCKET_URL='...'
export X_ATLASSIAN_BITBUCKET_PERSONAL_TOKEN='...'
```

`X_ATLASSIAN_JIRA_URL` and `X_ATLASSIAN_JIRA_PERSONAL_TOKEN` are required even without the MCP integration, since `jira_logger.py` calls the Jira REST API directly to log DQ Change Request tickets.

The gateway model and Atlassian modules can also read selected values from the Databricks secret scope named by `DATABRICKS_SECRET_SCOPE`, which defaults to `collibra`. In local execution, environment variables are the normal configuration path.

### Optional Jira change-request logging overrides

```bash
export CHANGE_REQUEST_EPIC_KEY='JGPV-1007'
export CHANGE_REQUEST_PROJECT_KEY='JGPV'
export CHANGE_REQUEST_COMPONENT_NAME='DQ Change Requests'
export JIRA_TASK_TYPE_ID='3'
```

All four have sensible defaults baked into `jira_logger.py`; override them only if the pre-created epic/project/component/task-type differ.

### Dataset templates

New dataset creation clones existing reference DatasetDefs. Override the reference names if those templates are retired:

```bash
export CDQ_REDSHIFT_TEMPLATE='ds_redshift_region_anz_itg_enrichment_brand_ta_mapping'
export CDQ_S3_TEMPLATE='ds_conn_s3_dq_iconnect_source_Account_Objectives_vs_iDiscover'
```

## Running the application

Start the local Gradio application from its directory:

```bash
python collibra_dq_app/app.py
```

The app binds to `127.0.0.1:7860` and does not create a public Gradio share link. Open `http://127.0.0.1:7860`, log in with a configured super-user account, and then use the Chat tab.

The assistant shows model activity, reasoning when enabled, tool calls, and response progress. The Atlassian MCP connection is initialized lazily after the first authenticated chat request. If it is unavailable, the app reports that it is running with local Collibra tools only.

If a user has a prior chat session in `dqm_chatbot_chat_history`, the Chat tab offers to resume it (reloading both the visible history and the agent's own conversation memory) or start fresh.

## Example prompts

Read-only inspection:

```text
What business unit is ds_conn_s3_angen_maf_target_ki_au mapped to?
Show the current DatasetDef and alert for ds_example in APAC.
Find the DQ findings for ds_example for run date 2026-08-20.
```

New dataset workflow:

```text
Help me create a DQ dataset for Redshift schema sales and table orders.
Suggest the appropriate Data Domain and sub-domain using the table data and similar datasets.
```

Atlassian investigation:

```text
Find JEJQ tickets related to table jpubsdata.sales and summarize the evidence.
```

For a write request, provide the requested schedule, metadata, and key-column decisions. The agent should show the proposal before asking for explicit confirmation.

## Safety and operational behavior

- Read and write credentials are supplied through environment variables or Databricks secrets; do not commit them.
- Collibra API tokens are cached in memory and refreshed when missing, near expiry, or after a 401 response.
- Collibra requests use a 60-second timeout. The token manager currently disables SSL verification for the internal CDQ service, so deployment network controls and certificate policy should be reviewed before production use.
- Redshift sampling limits results to at most 50 rows (200 for keyword search); `test_redshift_query` only accepts a single `SELECT` statement, wrapped and row-capped. Identifiers are defensively quoted before query execution.
- S3 access is read-only: `list_s3_objects`/`sample_s3_file` only call `ListObjectsV2`/ranged `GetObject`; there is no upload/overwrite/delete tool, and the agent instructions explicitly refuse such requests.
- The agent instruction limits batch actions to 10 datasets, and explicitly refuses any request to delete/wipe/mass-remove Collibra DQ configuration -- there is no delete tool exposed.
- `change_reason` is a mandatory argument to `apply_dataset_change`; the tool raises rather than applying a change without one.
- Alert configuration is fetched through the alert endpoint, not inferred from a DatasetDef response.
- An update or creation may succeed even if its follow-up alert creation or job trigger fails; the result reports those follow-up errors separately.
- Every applied change is logged to the database immediately, then queued for Jira; `sync_jira_change_request` should only be called once per dataset, with explicit user confirmation, after all of that dataset's changes for the conversation are done.
- The Atlassian integration may expose write-capable tools depending on server configuration. Keep read-only mode enabled unless write access is deliberately required.

## Standalone agent usage

The top-level `strands_agent.py` exposes a reusable Atlassian-aware agent without the Gradio UI:

```python
from strands_agent import atlassian_agent_session, diagnose_with_agent

answer = diagnose_with_agent("Find Jira tickets mentioning table jpubsdata.sales")

with atlassian_agent_session() as agent:
    print(agent("Search JEJQ tickets related to dataset ds_example"))
```

The local Collibra agent can be used directly from `collibra_dq_app`:

```python
from dq_agent import build_agent

agent = build_agent()
try:
    print(agent("List APAC datasets containing sales"))
finally:
    agent.cleanup()
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Login is disabled | `SUPERUSER_CREDENTIALS` is missing or empty. |
| Missing API key error | Set `JNJ_GENAI_API_KEY`, or provide the key through the configured Databricks secret scope. |
| Collibra credentials missing | Set the complete `CDQ_BASE_URL_<REGION>`, `CDQ_USERNAME_<REGION>`, and `CDQ_PASSWORD_<REGION>` set for the requested region. |
| Redshift sampling fails | Set `REDSHIFT_USER` and `REDSHIFT_PASSWORD`, verify network access, and use a connection name containing `local` or `region`. |
| Similar-dataset lookup fails | Set all required `DB_*` variables and verify access to the configured mapping table. |
| Atlassian tools are unavailable | Check the MCP URL and Atlassian headers; the chatbot can still operate with local Collibra tools. |
| A proposed change cannot be applied | The `change_id` may have expired, already been consumed, or been generated in a different process. Propose the change again. |
| Port 7860 is occupied | Stop the process listening on the port or change the `demo.launch` port in `collibra_dq_app/app.py`. |

## Testing and development status

No automated test suite is currently included in the repository. Before using write-capable functionality against a live deployment, validate credentials and read-only calls first, then exercise the proposal flow and inspect the returned diff before confirming any change.
