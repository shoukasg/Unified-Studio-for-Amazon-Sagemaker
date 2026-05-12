"""
Lambda: Asset Sync (v2 - Managed Asset Revision)
Trigger: DataZone "Asset Added To Catalog" from Account 1 (on PUBLISH)
Action: 
  1. Mirrors Glue table to Account 2
  2. Finds the existing MANAGED asset (created by data source) by table name
  3. Updates it with business metadata from Account 1 via create_asset_revision
  4. Auto-publishes the revision
  5. Syncs lineage
"""
import json, os, boto3, logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SOURCE_ACCOUNT = os.environ['SOURCE_ACCOUNT_ID']
SOURCE_DATABASE = os.environ['SOURCE_DATABASE']
TARGET_DATABASE = os.environ['TARGET_DATABASE']
SOURCE_ROLE_ARN = os.environ['SOURCE_ROLE_ARN']
SOURCE_DZ_ROLE_ARN = os.environ['SOURCE_DZ_ROLE_ARN']
SOURCE_DOMAIN_ID = os.environ['SOURCE_DOMAIN_ID']
SOURCE_PROJECT_ID = os.environ.get('SOURCE_PROJECT_ID', '')
SOURCE_DOMAIN_ID = os.environ['SOURCE_DOMAIN_ID']
DOMAIN_ID = os.environ['DOMAIN_ID']
PROJECT_ID = os.environ['PROJECT_ID']
ACCOUNT_ID = os.environ['ACCOUNT_ID']

sts = boto3.client('sts')
glue_local = boto3.client('glue')
dz = boto3.client('datazone')


def get_source_glue_client():
    resp = sts.assume_role(RoleArn=SOURCE_ROLE_ARN, RoleSessionName='asset-sync-glue')
    c = resp['Credentials']
    return boto3.client('glue',
        aws_access_key_id=c['AccessKeyId'],
        aws_secret_access_key=c['SecretAccessKey'],
        aws_session_token=c['SessionToken']
    )


def get_source_dz_client():
    resp = sts.assume_role(RoleArn=SOURCE_DZ_ROLE_ARN, RoleSessionName='asset-sync-dz')
    c = resp['Credentials']
    return boto3.client('datazone',
        aws_access_key_id=c['AccessKeyId'],
        aws_secret_access_key=c['SecretAccessKey'],
        aws_session_token=c['SessionToken']
    )


def mirror_table(table_name):
    """Mirror Glue table from Account 1 to Account 2."""
    source_glue = get_source_glue_client()
    source_table = source_glue.get_table(
        CatalogId=SOURCE_ACCOUNT, DatabaseName=SOURCE_DATABASE, Name=table_name
    )['Table']

    table_input = {
        'Name': source_table['Name'],
        'Description': source_table.get('Description', ''),
        'StorageDescriptor': source_table['StorageDescriptor'],
        'TableType': source_table.get('TableType', 'EXTERNAL_TABLE'),
        'Parameters': source_table.get('Parameters', {})
    }

    try:
        glue_local.create_table(DatabaseName=TARGET_DATABASE, TableInput=table_input)
        return "Created", source_table
    except glue_local.exceptions.AlreadyExistsException:
        glue_local.update_table(DatabaseName=TARGET_DATABASE, TableInput=table_input)
        return "Updated", source_table


def find_managed_asset(table_name):
    """Find the managed asset in Account 2 by matching catalogId + databaseName + tableName in GlueTableForm."""
    try:
        resp = dz.search(
            domainIdentifier=DOMAIN_ID,
            owningProjectIdentifier=PROJECT_ID,
            searchScope='ASSET',
            searchText=table_name,
            maxResults=20
        )
        for item in resp.get('items', []):
            asset = item.get('assetItem', {})
            asset_id = asset.get('identifier')
            if not asset_id:
                continue
            # Get full asset to inspect GlueTableForm
            try:
                full_asset = dz.get_asset(domainIdentifier=DOMAIN_ID, identifier=asset_id)
                for form in full_asset.get('formsOutput', []):
                    if form.get('formName') == 'GlueTableForm':
                        form_data = json.loads(form.get('content', '{}'))
                        if (form_data.get('catalogId') == ACCOUNT_ID and
                            form_data.get('databaseName') == TARGET_DATABASE and
                            form_data.get('tableName') == table_name):
                            logger.info(f"Found managed asset {asset_id} for table {table_name}")
                            return asset_id
            except Exception as e:
                logger.warning(f"Could not inspect asset {asset_id}: {e}")
                continue
        logger.warning(f"No managed asset matched catalogId={ACCOUNT_ID}, db={TARGET_DATABASE}, table={table_name}")
    except Exception as e:
        logger.error(f"Search for managed asset failed: {e}")
    return None


def update_managed_asset(asset_id, asset_name, description, source_table, column_metadata=None, readme=None, summary=None):
    """Update the managed asset with business metadata, preserving all existing forms."""
    # Read ALL existing forms from the managed asset
    existing = dz.get_asset(domainIdentifier=DOMAIN_ID, identifier=asset_id)
    
    forms_input = []
    for form in existing.get('formsOutput', []):
        form_name = form.get('formName')
        content = form.get('content', '{}')
        
        # Merge business metadata into AssetCommonDetailsForm
        if form_name == 'AssetCommonDetailsForm':
            try:
                form_data = json.loads(content)
            except:
                form_data = {}
            if summary:
                form_data['summary'] = summary
            if readme:
                form_data['readMe'] = readme
            content = json.dumps(form_data)
        
        forms_input.append({"formName": form_name, "content": content})
    
    # Add ColumnBusinessMetadataForm if we have column metadata and it's not already there
    if column_metadata:
        has_col_form = any(f['formName'] == 'ColumnBusinessMetadataForm' for f in forms_input)
        if has_col_form:
            forms_input = [
                {"formName": f['formName'], "content": json.dumps(column_metadata)} if f['formName'] == 'ColumnBusinessMetadataForm' else f
                for f in forms_input
            ]
        else:
            forms_input.append({"formName": "ColumnBusinessMetadataForm", "content": json.dumps(column_metadata)})

    try:
        resp = dz.create_asset_revision(
            domainIdentifier=DOMAIN_ID,
            identifier=asset_id,
            name=asset_name,
            description=description,
            formsInput=forms_input
        )
        logger.info(f"Asset revision created: {asset_id}, revision: {resp.get('revision')}")

        # Auto-publish
        pub = dz.create_listing_change_set(
            domainIdentifier=DOMAIN_ID,
            entityIdentifier=asset_id,
            entityType='ASSET',
            action='PUBLISH'
        )
        logger.info(f"Published: {pub.get('listingId')}")
        return f"Asset {asset_id} updated and published"
    except Exception as e:
        logger.error(f"Asset revision failed: {e}")
        return f"Asset revision failed: {str(e)[:100]}"


def sync_asset_from_source(source_asset_id, target_asset_id):
    """Read metadata from Account 1 asset and update the managed asset in Account 2."""
    source_dz = get_source_dz_client()
    source_asset = source_dz.get_asset(domainIdentifier=SOURCE_DOMAIN_ID, identifier=source_asset_id)

    asset_name = source_asset.get('name', '')
    description = source_asset.get('description', '')[:2048]

    readme = None
    summary = None
    column_metadata = None
    for form in source_asset.get("formsOutput", []):
        if form.get("formName") == "AssetCommonDetailsForm":
            try:
                form_data = json.loads(form.get("content", "{}"))
                summary = form_data.get("summary", "")
                readme = form_data.get("readMe")
            except: pass
        elif form.get("formName") == "ColumnBusinessMetadataForm":
            try:
                column_metadata = json.loads(form.get("content", "{}"))
            except: pass

    # Get source table for schema
    table_name = asset_name
    for form in source_asset.get('formsOutput', []):
        if form.get('formName') == 'GlueTableForm':
            try:
                form_data = json.loads(form.get('content', '{}'))
                table_name = form_data.get('tableName', asset_name)
            except: pass

    source_glue = get_source_glue_client()
    source_table = source_glue.get_table(CatalogId=SOURCE_ACCOUNT, DatabaseName=SOURCE_DATABASE, Name=table_name)['Table']

    update_managed_asset(target_asset_id, asset_name, description, source_table, column_metadata, readme, summary)
    sync_dq_results(table_name, target_asset_id)
    logger.info(f"Synced {source_asset_id} -> {target_asset_id}")


def sync_dq_results(table_name, target_asset_id):
    """Pull latest DQ results from Account 1 and post to Account 2 asset."""
    try:
        source_glue = get_source_glue_client()
        # List DQ results for this table
        resp = source_glue.list_data_quality_results(
            Filter={
                'DataSource': {
                    'GlueTable': {
                        'DatabaseName': SOURCE_DATABASE,
                        'TableName': table_name,
                        'CatalogId': SOURCE_ACCOUNT
                    }
                }
            },
            MaxResults=5
        )
        results = resp.get('Results', [])
        if not results:
            logger.info(f"No DQ results found for {table_name}")
            return

        # Get the latest result
        latest = results[0]
        result_id = latest.get('ResultId', '')
        if not result_id:
            return

        dq_result = source_glue.get_data_quality_result(ResultId=result_id)

        # Convert to DataZone format
        rule_results = dq_result.get('RuleResults', [])
        evaluations = []
        for rule in rule_results:
            evaluations.append({
                'types': [rule.get('Name', 'Unknown')],
                'description': rule.get('Description', rule.get('EvaluatedRule', '')),
                'details': {},
                'applicableFields': [],
                'status': rule.get('Result', 'UNKNOWN')
            })

        total = len(rule_results)
        passed = sum(1 for r in rule_results if r.get('Result') == 'PASS')
        percentage = (passed / total * 100) if total > 0 else 0

        dq_content = json.dumps({
            'evaluations': evaluations,
            'passingPercentage': percentage,
            'evaluationsCount': total
        })

        # Get form type revision
        try:
            ft_resp = dz.get_form_type(
                domainIdentifier=DOMAIN_ID,
                formTypeIdentifier='amazon.datazone.DataQualityResultFormType'
            )
            revision = ft_resp.get('revision', '1')
        except:
            revision = '1'

        # Post to Account 2 asset
        from datetime import datetime
        ruleset_name = dq_result.get('RulesetName', 'dq_rules')
        dz.post_time_series_data_points(
            domainIdentifier=DOMAIN_ID,
            entityIdentifier=target_asset_id,
            entityType='ASSET',
            forms=[{
                'formName': ruleset_name,
                'content': dq_content,
                'timestamp': datetime.now().timestamp(),
                'typeIdentifier': 'amazon.datazone.DataQualityResultFormType',
                'typeRevision': revision
            }]
        )
        logger.info(f"DQ results posted for {table_name} ({total} rules, {percentage:.0f}% pass)")
    except Exception as e:
        logger.error(f"DQ sync error for {table_name}: {e}")


def handler(event, context):
    logger.info(f"Event: {json.dumps(event)}")

    # Manual sync
    if 'table_name' in event:
        table_name = event['table_name']
        action, source_table = mirror_table(table_name)
        logger.info(f"Glue table: {action}")

        asset_id = find_managed_asset(table_name)
        if asset_id:
            result = update_managed_asset(asset_id, event.get('asset_name', table_name), event.get('description', ''), source_table)
            sync_lineage_for_asset(asset_id)
        else:
            result = f"Table mirrored but no managed asset found for '{table_name}'. Run data source first."
        return {"statusCode": 200, "body": result}

    # DataZone event (Asset Added To Catalog / New Asset Version Available)
    detail_type = event.get('detail-type', '')

    # Data Source Run completed - sync metadata for all tables from Account 1
    if detail_type == 'Data Source Run Succeeded':
        logger.info("Data source run completed - syncing metadata for all tables")
        source_glue = get_source_glue_client()
        tables = source_glue.get_tables(CatalogId=SOURCE_ACCOUNT, DatabaseName=SOURCE_DATABASE).get('TableList', [])
        results = []
        for table in tables:
            table_name = table['Name']
            asset_id = find_managed_asset(table_name)
            if asset_id:
                # Find the source asset ID from Account 1 for this table
                try:
                    source_dz = get_source_dz_client()
                    search_resp = source_dz.search(
                        domainIdentifier=SOURCE_DOMAIN_ID,
                        owningProjectIdentifier=SOURCE_PROJECT_ID,
                        searchScope='ASSET',
                        searchText=table_name,
                        maxResults=5
                    )
                    for item in search_resp.get('items', []):
                        source_asset_id = item.get('assetItem', {}).get('identifier')
                        if source_asset_id:
                            # Trigger the full sync for this asset
                            sync_asset_from_source(source_asset_id, asset_id)
                            results.append(f"{table_name}: synced")
                            break
                except Exception as e:
                    results.append(f"{table_name}: error - {str(e)[:50]}")
            else:
                results.append(f"{table_name}: no managed asset found")
        return {"statusCode": 200, "body": f"Data source sync: {results}"}

    if detail_type in ('Asset Added To Catalog', 'New Asset Version Available', 'Asset Schema Changed'):
        asset_id_source = event.get('detail', {}).get('data', {}).get('assetId', '')
        if not asset_id_source:
            return {'statusCode': 400, 'body': 'No assetId in event'}

        logger.info(f"Reading asset {asset_id_source} from Account 1 domain")

        # Get full asset metadata from Account 1
        try:
            source_dz = get_source_dz_client()
            source_asset = source_dz.get_asset(
                domainIdentifier=SOURCE_DOMAIN_ID,
                identifier=asset_id_source
            )
        except Exception as e:
            logger.error(f"Failed to read asset from Account 1: {e}")
            return {'statusCode': 500, 'body': f'GetAsset failed: {str(e)[:100]}'}

        asset_name = source_asset.get('name', '')

        # Use the asset's top-level description (short) for the asset description
        description = source_asset.get('description', '')[:2048]

        # Extract readme, summary, and column metadata from forms
        readme = None
        summary = None
        column_metadata = None
        for form in source_asset.get("formsOutput", []):
            if form.get("formName") == "AssetCommonDetailsForm":
                try:
                    form_data = json.loads(form.get("content", "{}"))
                    summary = form_data.get("summary", "")
                    readme = form_data.get("readMe")
                except: pass
            elif form.get("formName") == "ColumnBusinessMetadataForm":
                try:
                    column_metadata = json.loads(form.get("content", "{}"))
                except: pass

        # Extract table name from GlueTableForm
        table_name = asset_name
        for form in source_asset.get('formsOutput', []):
            if form.get('formName') == 'GlueTableForm':
                try:
                    form_data = json.loads(form.get('content', '{}'))
                    table_name = form_data.get('tableName', asset_name)
                except: pass

        logger.info(f"Asset: {asset_name}, Table: {table_name}")

        # Step 1: Mirror the Glue table
        try:
            action, source_table = mirror_table(table_name)
            logger.info(f"Glue table: {action}")
        except Exception as e:
            logger.error(f"Glue mirror failed: {e}")
            return {'statusCode': 500, 'body': f'Glue mirror failed: {str(e)[:100]}'}

        # Step 2: Find the managed asset in Account 2
        asset_id = find_managed_asset(table_name)
        if not asset_id:
            logger.warning(f"No managed asset found for '{table_name}'. Data source may not have run yet.")
            return {'statusCode': 200, 'body': f'Table mirrored. No managed asset found for {table_name} - awaiting data source run.'}

        # Step 3: Update with business metadata
        result = update_managed_asset(asset_id, asset_name, description, source_table, column_metadata, readme, summary)

        # Step 4: Sync lineage
        sync_lineage_for_asset(asset_id_source)

        return {"statusCode": 200, "body": result}

    return {'statusCode': 200, 'body': 'No action needed'}


def sync_lineage_for_asset(asset_id):
    """Sync lineage events from Account 1 to Account 2."""
    try:
        source_dz = get_source_dz_client()
        resp = source_dz.list_lineage_events(
            domainIdentifier=SOURCE_DOMAIN_ID,
            maxResults=10
        )

        synced = 0
        for e in resp.get('items', []):
            if e.get('processingStatus') == 'SUCCESS':
                event_resp = source_dz.get_lineage_event(
                    domainIdentifier=SOURCE_DOMAIN_ID,
                    identifier=e['id']
                )
                body = event_resp.get('event')
                if body:
                    ol_event = json.loads(body.read().decode('utf-8'))
                    if ol_event.get('eventType') == 'COMPLETE':
                        event_str = json.dumps(ol_event).replace(SOURCE_DOMAIN_ID, DOMAIN_ID)
                        try:
                            dz.post_lineage_event(
                                domainIdentifier=DOMAIN_ID,
                                event=event_str.encode('utf-8')
                            )
                            synced += 1
                        except Exception as ex:
                            logger.error(f"Lineage post failed: {ex}")

        logger.info(f"Synced {synced} lineage events for asset {asset_id}")
        return synced
    except Exception as e:
        logger.error(f"Lineage sync error: {e}")
        return 0
