"""
Lambda 1: Glue Table Sync
Trigger: Glue CreateTable/UpdateTable event from Account 1
Action: Mirrors the Glue table into Account 2's project database, grants LF permissions to project role
"""
import json, os, boto3, logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SOURCE_ACCOUNT = os.environ['SOURCE_ACCOUNT_ID']
SOURCE_DATABASE = os.environ['SOURCE_DATABASE']
TARGET_DATABASE = os.environ['TARGET_DATABASE']
SOURCE_ROLE_ARN = os.environ['SOURCE_ROLE_ARN']
PROJECT_ROLE_ARN = os.environ.get('PROJECT_ROLE_ARN', '')

sts = boto3.client('sts')
glue_local = boto3.client('glue')
lf = boto3.client('lakeformation')


def get_source_glue_client():
    resp = sts.assume_role(RoleArn=SOURCE_ROLE_ARN, RoleSessionName='glue-sync')
    c = resp['Credentials']
    return boto3.client('glue',
        aws_access_key_id=c['AccessKeyId'],
        aws_secret_access_key=c['SecretAccessKey'],
        aws_session_token=c['SessionToken']
    )


def mirror_table(table_name):
    source_glue = get_source_glue_client()
    source_table = source_glue.get_table(
        CatalogId=SOURCE_ACCOUNT, DatabaseName=SOURCE_DATABASE, Name=table_name
    )['Table']

    table_input = {
        'Name': source_table['Name'],
        'Description': f"Mirrored from Account {SOURCE_ACCOUNT} - {source_table.get('Description', '')}",
        'StorageDescriptor': source_table['StorageDescriptor'],
        'TableType': source_table.get('TableType', 'EXTERNAL_TABLE'),
        'Parameters': source_table.get('Parameters', {})
    }

    try:
        glue_local.create_table(DatabaseName=TARGET_DATABASE, TableInput=table_input)
        action = "Created"
    except glue_local.exceptions.AlreadyExistsException:
        glue_local.update_table(DatabaseName=TARGET_DATABASE, TableInput=table_input)
        action = "Updated"

    # Grant LF permissions to the Federation project's environment role (with grantable)
    grant_project_role_access(table_name)
    return action


def grant_project_role_access(table_name):
    """Grant SELECT + DESCRIBE with grant option to the Federation project's environment role."""
    if not PROJECT_ROLE_ARN:
        logger.warning("PROJECT_ROLE_ARN not set, skipping LF grant")
        return
    try:
        lf.grant_permissions(
            Principal={'DataLakePrincipalIdentifier': PROJECT_ROLE_ARN},
            Resource={'Table': {'DatabaseName': TARGET_DATABASE, 'Name': table_name}},
            Permissions=['SELECT', 'DESCRIBE'],
            PermissionsWithGrantOption=['SELECT', 'DESCRIBE']
        )
        logger.info(f"Granted LF SELECT+DESCRIBE (grantable) to {PROJECT_ROLE_ARN} on {table_name}")
    except Exception as e:
        logger.error(f"LF grant failed: {e}")


def handler(event, context):
    logger.info(f"Event: {json.dumps(event)}")

    # Manual sync
    if 'table_name' in event:
        action = mirror_table(event['table_name'])
        return {'statusCode': 200, 'body': f'{action} table {event["table_name"]} in {TARGET_DATABASE}'}

    # EventBridge Glue event
    detail = event.get('detail', {})
    event_name = detail.get('eventName', '')
    if event_name in ('CreateTable', 'UpdateTable'):
        request_params = detail.get('requestParameters', {})
        table_name = request_params.get('tableInput', {}).get('name', '')
        database_name = request_params.get('databaseName', '')
        if database_name == SOURCE_DATABASE and table_name:
            action = mirror_table(table_name)
            logger.info(f"{action} table {table_name} in {TARGET_DATABASE}")
            return {'statusCode': 200, 'body': f'{action} table {table_name} in {TARGET_DATABASE}'}

    return {'statusCode': 200, 'body': 'No action needed'}
