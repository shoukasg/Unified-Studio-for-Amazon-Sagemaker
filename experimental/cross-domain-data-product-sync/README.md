# Cross-Domain Data Product Publication & Subscription

Publish data products from one SageMaker Unified Studio (SMUS) domain and make them discoverable, subscribable, and queryable in another domain — without data duplication.

## Problem

SMUS does not natively support cross-domain data product publishing. Enterprises with multiple domains (M&A activity, regulatory separation, multi-BU structures) cannot share Iceberg data products across organizational boundaries without custom infrastructure. Resource links (RAM shares) received in a target domain cannot be further shared within that domain's governance layer.

## Solution

Event-driven catalog mirroring with two Lambda functions that sync Iceberg table metadata, business context, data quality results, and lineage across domains. Consumers subscribe and query through native SMUS workflows with Lake Formation governing access.

### Key Features

- **Zero-copy data access** — consumers read producer's S3 directly via LF credential vending
- **Native DataZone subscription fulfilment** — managed assets with automatic LF grants on approval
- **Business metadata sync** — name, description, readme, column descriptions from producer domain
- **DQ results propagation** — pulled from producer on each sync cycle
- **Lineage sync** — OpenLineage events forwarded across domains
- **Lake Formation fine-grained access control** — column/row level security in the consumer domain

## Architecture

```
Producer Account                    Marketplace Account                 Consumer Project
┌─────────────────┐               ┌──────────────────────────┐        ┌─────────────────┐
│                 │               │                          │        │                 │
│ Iceberg Table   │──EventBridge─▶│ Lambda 1: glue-table-sync│        │                 │
│ (S3 data)       │  (CreateTable)│  • Mirrors table         │        │                 │
│                 │               │  • Grants LF (grantable) │        │                 │
│                 │               │                          │        │                 │
│ Publish Asset   │──EventBridge─▶│ Lambda 2: catalog-sync   │        │                 │
│ (curated)       │  (Asset Added)│  • Syncs business metadata│       │                 │
│                 │               │  • Pulls DQ results      │        │                 │
│                 │               │  • Syncs lineage         │        │                 │
│                 │               │  • Auto-publishes        │        │                 │
│                 │               │                          │        │                 │
│                 │               │ Data Source (manual)      │        │                 │
│                 │               │  • Creates MANAGED asset  │        │                 │
│                 │               │                          │        │                 │
│                 │               │ Subscribe → Approve       │◀───────│ Subscribe       │
│                 │               │  → DataZone grants LF     │───────▶│ Query (zero copy)│
│  S3 Data ◀──────│───────────────│──────────────────────────│────────│─┘               │
└─────────────────┘               └──────────────────────────┘        └─────────────────┘
```

## Components

| Component | Purpose |
|-----------|---------|
| `lambda/glue_table_sync.py` | Mirrors Glue table schema + grants LF permissions to project role |
| `lambda/catalog_sync_mirror.py` | Syncs business metadata, DQ, lineage; creates asset revision; auto-publishes |
| `cloudformation/template.yaml` | Full infrastructure deployment (Lambdas, EventBridge rules, IAM roles, LF grants) |

## Prerequisites

- Two AWS accounts with SMUS domains configured
- Producer account: Iceberg tables in a Glue database, S3 bucket with data
- Marketplace account: SMUS project with Lakehouse Database environment
- Cross-account EventBridge permissions configured
- Lake Formation: Producer's S3 location registered in Marketplace account

## Deployment

### Step 1: Deploy CFN in Marketplace Account (Account 2)

This creates: `CatalogSyncLambdaRole`, `MirrorCatalogLFRole`, 2 Lambdas, 3 EventBridge rules, EventBus policy.

```bash
aws cloudformation deploy \
  --template-file cloudformation/template.yaml \
  --stack-name cross-domain-data-product-sync \
  --parameter-overrides \
    SourceAccountId=<PRODUCER_ACCOUNT_ID> \
    SourceDatabase=<PRODUCER_GLUE_DB> \
    SourceDomainId=<PRODUCER_DOMAIN_ID> \
    SourceProjectId=<PRODUCER_PROJECT_ID> \
    TargetDatabase=<MARKETPLACE_GLUE_DB> \
    DomainId=<MARKETPLACE_DOMAIN_ID> \
    ProjectId=<MARKETPLACE_PROJECT_ID> \
    ProjectEnvironmentRoleArn=<MARKETPLACE_PROJECT_ENV_ROLE_ARN> \
    SourceGlueRoleArn=arn:aws:iam::<PRODUCER_ACCOUNT_ID>:role/GlueFederationAccessRole \
    SourceDataZoneRoleArn=arn:aws:iam::<PRODUCER_ACCOUNT_ID>:role/DataZoneReaderRole \
    S3BucketArn=arn:aws:s3:::<PRODUCER_S3_BUCKET> \
  --capabilities CAPABILITY_NAMED_IAM
```

### Step 2: Deploy Lambda Code

```bash
cd lambda/
zip glue_table_sync.zip glue_table_sync.py
zip catalog_sync_mirror.zip catalog_sync_mirror.py

aws lambda update-function-code --function-name glue-table-sync --zip-file fileb://glue_table_sync.zip
aws lambda update-function-code --function-name catalog-sync-mirror --zip-file fileb://catalog_sync_mirror.zip
```

### Step 3: Manual Setup in Marketplace Account (Account 2)

| Step | Action | Why |
|------|--------|-----|
| 3a | Register Producer's S3 location in Lake Formation with `MirrorCatalogLFRole` | Enables credential vending for cross-account S3 reads |
| 3b | Add `CatalogSyncLambdaRole` as **Contributor** to the Marketplace SMUS project | Lambda needs DataZone permissions to search/update assets |
| 3c | Create a **Data Source** in the Marketplace project pointing to the target Glue database | Required for creating managed assets (subscription-eligible) |
| 3d | Enable LF Application Integration Settings: "Allow external engines to access data in Amazon S3 locations with full table access" | Required for credential vending to work |

```bash
# 3a: Register S3 location
aws lakeformation register-resource \
  --resource-arn arn:aws:s3:::<PRODUCER_S3_BUCKET>/<PATH> \
  --role-arn arn:aws:iam::<MARKETPLACE_ACCOUNT_ID>:role/MirrorCatalogLFRole \
  --use-service-linked-role false

# 3b: Add Lambda role as project Contributor (via SMUS UI or API)
# Navigate to: SMUS Portal → Project → Members → Add Member → CatalogSyncLambdaRole
```

### Step 4: Setup in Producer Account (Account 1)

Create two IAM roles that trust the Marketplace account's `CatalogSyncLambdaRole`:

**4a: GlueFederationAccessRole** (for reading Glue table definitions)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::<MARKETPLACE_ACCOUNT_ID>:role/CatalogSyncLambdaRole",
        "Service": "events.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

Permissions: `glue:GetTable`, `glue:GetTables`, `glue:GetDatabase`, `glue:ListDataQualityResults`, `glue:GetDataQualityResult`

**4b: DataZoneReaderRole** (for reading asset metadata and lineage)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::<MARKETPLACE_ACCOUNT_ID>:role/CatalogSyncLambdaRole"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

Permissions: `datazone:GetAsset`, `datazone:Search`, `datazone:ListLineageEvents`, `datazone:GetLineageEvent`

**Important:** Add `DataZoneReaderRole` as a **Contributor** to the Producer's SMUS project (required for `GetAsset` API access).

### Step 5: EventBridge Rules in Producer Account (Account 1)

Forward events cross-account to the Marketplace account's default event bus:

```bash
# Rule 1: Glue table changes
aws events put-rule \
  --name glue-table-sync-to-marketplace \
  --event-pattern '{
    "source": ["aws.glue"],
    "detail-type": ["AWS API Call via CloudTrail"],
    "detail": {
      "eventSource": ["glue.amazonaws.com"],
      "eventName": ["CreateTable", "UpdateTable"],
      "requestParameters": {"databaseName": ["<PRODUCER_GLUE_DB>"]}
    }
  }'

aws events put-targets --rule glue-table-sync-to-marketplace \
  --targets "Id=marketplace,Arn=arn:aws:events:us-east-1:<MARKETPLACE_ACCOUNT_ID>:event-bus/default,RoleArn=arn:aws:iam::<PRODUCER_ACCOUNT_ID>:role/GlueFederationAccessRole"

# Rule 2: Asset publish
aws events put-rule \
  --name datazone-publish-sync-to-marketplace \
  --event-pattern '{
    "source": ["aws.datazone"],
    "detail-type": ["Asset Added To Catalog", "New Asset Version Available"]
  }'

aws events put-targets --rule datazone-publish-sync-to-marketplace \
  --targets "Id=marketplace,Arn=arn:aws:events:us-east-1:<MARKETPLACE_ACCOUNT_ID>:event-bus/default,RoleArn=arn:aws:iam::<PRODUCER_ACCOUNT_ID>:role/GlueFederationAccessRole"
```

### Step 6: S3 Bucket Policy in Producer Account

Add `MirrorCatalogLFRole` from the Marketplace account to the Producer's S3 bucket policy:

```json
{
  "Effect": "Allow",
  "Principal": {
    "AWS": "arn:aws:iam::<MARKETPLACE_ACCOUNT_ID>:role/MirrorCatalogLFRole"
  },
  "Action": ["s3:GetObject", "s3:ListBucket"],
  "Resource": [
    "arn:aws:s3:::<PRODUCER_BUCKET>",
    "arn:aws:s3:::<PRODUCER_BUCKET>/*"
  ]
}
```

### Setup Summary

| What | Where | How |
|------|-------|-----|
| CFN stack (Lambdas, roles, EventBridge) | Marketplace account | `cloudformation deploy` |
| Lambda code deployment | Marketplace account | `update-function-code` |
| LF S3 registration | Marketplace account | CLI (`register-resource`) |
| CatalogSyncLambdaRole as project Contributor | Marketplace SMUS project | UI or API |
| Data Source creation | Marketplace SMUS project | UI |
| LF Application Integration Settings | Marketplace account | LF Console |
| GlueFederationAccessRole | Producer account | IAM Console/CLI |
| DataZoneReaderRole | Producer account | IAM Console/CLI |
| DataZoneReaderRole as project Contributor | Producer SMUS project | UI |
| EventBridge forwarding rules | Producer account | CLI |
| S3 bucket policy update | Producer account | S3 Console/CLI |

## End-to-End Workflow

### First-time setup

1. **Producer:** Create Iceberg table → auto-mirrors to marketplace account (~30s)
2. **Producer:** Run data source, curate asset (name, description, readme), publish
3. **Marketplace:** Run data source (manual) → creates managed asset → triggers metadata sync
4. **Consumer:** Discovers asset in catalog → subscribes → approver approves → queries data

### Ongoing updates

- Re-publish in producer → auto-syncs metadata to marketplace
- Schema changes → auto-mirrored via Glue table sync
- DQ results → pulled on next sync cycle

## Known Limitations

| Limitation | Workaround |
|-----------|------------|
| Glossary terms not synced (domain-scoped) | Create matching terms in marketplace domain manually |
| Data source run is manual | Schedule hourly or trigger via `StartDataSourceRun` API |
| Subscription approval in marketplace account only | Route notifications to producer via EventBridge + callback |
| ~30s latency for table sync (CloudTrail-based) | Acceptable for most use cases |
| DQ results not real-time | Pulled on next asset sync (re-publish or data source run) |

## Security

See [CONTRIBUTING](../../CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This project is licensed under the Apache-2.0 License.
