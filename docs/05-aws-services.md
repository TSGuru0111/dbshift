# AWS services and access

**Nothing here is provisioned yet.** No AWS account exists for this project as
of the last update. Read `06-cost-model.md` before creating anything billable.

## Request-first items (these stall a build if not asked for separately)

| Service | Why it blocks | Ask for |
|---|---|---|
| **Amazon Bedrock** | Model access is opt-in **per model, per region**. A generic "enable Bedrock" approval leaves nothing working. | `InvokeModel`, `InvokeModelWithResponseStream`, plus named model access in the target region |
| **AWS IAM** | Most corporate accounts restrict role creation | Create roles/policies scoped to a `dbshift-*` prefix or permissions boundary |
| **AWS CloudFormation** | CDK does not provision anything itself — it synthesizes a template that CloudFormation executes. Without stack permissions the Provision phase cannot run at all. | Create/update/delete stacks, scoped to `dbshift-*` |
| **AWS Budgets** | Often owned by a central Finance/FinOps team, not platform engineering | Create budgets and alerts |

## Day-one routine

Lambda · Step Functions · API Gateway · EventBridge · RDS (engine
`oracle-se2`) · Aurora PostgreSQL Serverless v2 · DMS · S3 · SSM Parameter
Store · KMS (AWS-managed keys) · CloudTrail · VPC · CloudWatch · SNS

## Conditional

**NAT Gateway** — only needed if security refuses a public RDS endpoint. The
beta design puts RDS in a public subnet with a security group locked to one
office IP. That is a real downgrade and must not survive into v1. If refused,
budget ~$33/month for the NAT gateway.

## Deferred to v1 (list in the same ticket, don't request yet)

Secrets Manager · customer-managed KMS key · VPC interface endpoints ·
ECS/Fargate + ECR · QuickSight · Athena · Bedrock Knowledge Base

## Non-AWS prerequisites

- Outbound HTTPS from the build machine to `*.amazonaws.com`. A corporate proxy
  that blocks or MITMs AWS API traffic breaks the collector ingest path
  entirely. **Test this before building anything.**
- Oracle 21c XE installed locally (done)
- ~60 GB local disk
