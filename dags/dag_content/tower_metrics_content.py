import os
import json
import time
from itertools import chain
from typing import Any

from airflow.decorators import task
from airflow.models import Variable
import boto3
import synapseclient

# VARIABLES

# AWS creds
AWS_CREDS = {
    "AWS_ACCESS_KEY_ID": Variable.get("TOWER_DB_ACCESS_KEY"),
    "AWS_SECRET_ACCESS_KEY": Variable.get("TOWER_DB_SECRET_ACCESS_KEY"),
}
# AWS region
AWS_REGION = "us-east-1"

# cluster names
DATABASE_NAME = "tower"
CLONE_DATABASE_NAME = "tower-clone"

# query dictionary - maps query names to queries
QUERY_DICT = {
    "avg_workflows_run_per_month": """
            select round(AVG(total_runs), 2) as month_avg_past_year from (
            select count(*) as total_runs, DATE_FORMAT(`date_created`,'%M %Y') as month from tw_workflow
            where date_created between date_sub(now(),INTERVAL 1 YEAR) and now()
            group by DATE_FORMAT(`date_created`,'%M %Y')
            order by date_created
            ) as month_totals;
            """,
    "avg_workflows_run_per_week": """
            select round(AVG(total_runs), 2) as week_avg_past_year from (
            select count(*) as total_runs, CONCAT(YEAR(date_created), '/', WEEK(date_created)) as week from tw_workflow
            where date_created between date_sub(now(),INTERVAL 1 YEAR) and now()
            group by CONCAT(YEAR(date_created), '/', WEEK(date_created))
            order by date_created
            ) as week_totals;
            """,
    "run_status_breakdown_past_year": """
            select
            status,
            count(*) as status_count,
            round(count(*)/total_runs*100, 2) as percentage
            from (
            select
            status,
            date_created,
            (select count(*) as `total_runs` from tw_workflow where date_created between date_sub(now(),INTERVAL 1 YEAR) and now() and complete is not null) as total_runs
            from tw_workflow
            where date_created between date_sub(now(),INTERVAL 1 YEAR) and now()
            and complete is not null
            ) as set_up
            group by status
            order by count(*)/total_runs*100 desc;
            """,
    "workflow_breakdown_past_month": """
            select
            project_name,
            count(*) as runs,
            round(count(*)/total_runs*100, 2) as percentage
            from (
            select
            project_name,
            (select count(*) as `total_runs` from tw_workflow where date_created between date_sub(now(),INTERVAL 1 MONTH) and now() and complete is not null) as total_runs
            from tw_workflow
            where date_created between date_sub(now(),INTERVAL 1 MONTH) and now()
            and complete is not null
            ) as set_up
            group by project_name
            order by count(*)/total_runs*100 desc;      
            """,
    "total_tower_users": """
            select count(*) as total_users from tw_user;
            """,
    "active_users_past_month": """
            select count(distinct user_name) as users_past_month from tw_workflow where date_created between date_sub(now(),INTERVAL 1 MONTH) and now();
            """,
}

# FUNCTIONS


def package_query_data(response: dict) -> list[dict[str, Any]]:
    """
    takes responses from query requests and packages them into JSON friendly list of dictionaries

    Args:
        query_name (string): name of the query to be used as the key in output JSON file
        response (dict): dictionary request response from boto3

    Returns:
        dict: JSON-ready list of dictionaries
    """
    col_names = [col["name"] for col in response["columnMetadata"]]
    result = []
    for row in response["records"]:
        row_values = chain.from_iterable(field.values() for field in row)
        row_dict = dict(zip(col_names, row_values))
        result.append(row_dict)
    return result


# creates RDS boto3 client inside task
def create_rds_client(aws_creds):
    rds = boto3.client(
        "rds",
        aws_access_key_id=aws_creds["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=aws_creds["AWS_SECRET_ACCESS_KEY"],
        region_name=AWS_REGION,
    )
    return rds


# creates RDS Data boto3 client inside task
def create_rds_data_client(aws_creds):
    rdsData = boto3.client(
        "rds-data",
        aws_access_key_id=aws_creds["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=aws_creds["AWS_SECRET_ACCESS_KEY"],
        region_name=AWS_REGION,
    )
    return rdsData


# creates Secrets Manager boto3 client inside task
def create_secret_client(aws_creds):
    secrets = boto3.client(
        "secretsmanager",
        aws_access_key_id=aws_creds["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=aws_creds["AWS_SECRET_ACCESS_KEY"],
        region_name=AWS_REGION,
    )
    return secrets


# create authenticated synapse session
def create_synapse_session():
    syn = synapseclient.Synapse()
    syn.login(
        authToken=Variable.get("SYNAPSE_AUTH_TOKEN")
    )  # TODO - this is currently Brad's synapse token
    return syn


# check if database process is complete - can be used for clone creation, modification and deletion
def check_database_process_complete(client, waiter_type, db_name):
    time.sleep(20)  # allow process time to start before starting waiter
    waiter = client.get_waiter(waiter_type)
    waiter.wait(DBClusterIdentifier=db_name)


# TASKS


@task(multiple_outputs=True)
def get_database_info(aws_creds: dict, db_name: str) -> str:
    """
    Gets DBSubnetGroup and VpcSecurityGroupId from production database needed for later requests

    Args:
        aws_creds (dict): dictionary containing aws credentials
        db_name (str): name of production database cluster

    Returns:
        str: DBSubnetGroup and VpcSecurityGroupId from production database
    """
    rds = create_rds_client(aws_creds)

    response = rds.describe_db_clusters(
        DBClusterIdentifier=db_name,
    )
    subnet_group = response["DBClusters"][0]["DBSubnetGroup"]
    security_group = response["DBClusters"][0]["VpcSecurityGroups"][0][
        "VpcSecurityGroupId"
    ]

    return {"subnet_group": subnet_group, "security_group": security_group}


@task
def clone_tower_database(
    aws_creds: dict,
    db_name: str,
    clone_name: str,
    subnet_group: str,
    security_group: str,
) -> dict:

    """
    creates complete db clone from latest restorable time.

    Args:
        aws_creds (dict): dictionary containing aws credentials
        db_name (str): name of production database cluster
        clone_name (str): name of cloned database cluster to be created
        subnet_group (str): DBSubnetGroup from production database
        security_group (str): VpcSecurityGroupId from production database

    Returns:
        dict: dictionary containing information gathered from the request response necessary for later steps
    """
    rds = create_rds_client(aws_creds)

    response = rds.restore_db_cluster_to_point_in_time(
        SourceDBClusterIdentifier=db_name,
        DBClusterIdentifier=clone_name,
        RestoreType="copy-on-write",
        UseLatestRestorableTime=True,
        Port=3306,
        DBSubnetGroupName=subnet_group,
        VpcSecurityGroupIds=[
            security_group,
        ],
        Tags=[],
        EnableIAMDatabaseAuthentication=False,
        DeletionProtection=False,
        CopyTagsToSnapshot=False,
    )
    clone_db_info = {
        "user": response["DBCluster"]["MasterUsername"],
        "host": response["DBCluster"]["Endpoint"],
        "resource_id": response["DBCluster"]["DbClusterResourceId"],
        "resource_arn": response["DBCluster"]["DBClusterArn"],
    }
    # ensure cloning is complete before moving on
    check_database_process_complete(
        client=rds, waiter_type="db_cluster_available", db_name=clone_name
    )

    return clone_db_info


@task
def generate_random_password(aws_creds: dict) -> str:
    """
    generates random string password using boto3 secret client

    Args:
        aws_creds (dict): dictionary containing aws credentials

    Returns:
        str: generated password
    """
    secrets = create_secret_client(aws_creds)

    response = secrets.get_random_password(
        PasswordLength=30,
        ExcludeCharacters="@",
        ExcludePunctuation=True,
        IncludeSpace=False,
        RequireEachIncludedType=True,
    )
    password = response["RandomPassword"]
    return password


@task
def modify_database_clone(aws_creds: dict, clone_name: str, password: str):
    """
    modifies cloned database cluster to have new master password

    Args:
        aws_creds (dict): dictionary containing aws credentials
        clone_name (str): name of cloned database cluster
        password (str): password generated in previous step
    """
    rds = create_rds_client(aws_creds)

    rds.modify_db_cluster(
        ApplyImmediately=True,
        DBClusterIdentifier=clone_name,
        MasterUserPassword=password,
        EnableHttpEndpoint=True,
    )
    # ensure modification is complete before moving on
    check_database_process_complete(
        client=rds, waiter_type="db_cluster_available", db_name=clone_name
    )


@task
def update_secret(
    aws_creds: dict, clone_name: str, db_info: dict, password: str
) -> str:
    """
    updates secret in secret manager with formatted string includung new database info and random password

    Args:
        aws_creds (dict): dictionary containing aws credentials
        clone_name (str): name of cloned database cluster
        db_info (dict): dictionary with information from cloned database returned from clone_tower_cluster
        password (str): randomly generated password assigned to cloned database

    Returns:
        str: secret arn string needed to access clone for queries
    """
    secrets = create_secret_client(aws_creds)

    secret_dict = {
        "dbInstanceIdentifier": clone_name,
        "resourceId": db_info["resource_id"],
        "engine": "aurora-mysql",
        "host": db_info["host"],
        "port": 3306,
        "username": db_info["user"],
        "password": password,
    }
    secret_string = json.dumps(secret_dict)
    response = secrets.update_secret(
        SecretId="Programmatic-DB-Clone-Access", SecretString=secret_string
    )
    secret_arn = response["ARN"]

    return secret_arn


@task
def query_database(aws_creds: dict, resource_arn: str, secret_arn: str):
    """
    queries cloned database cluster with all desired queries. appends data to json_list for json export

    Args:
        aws_creds (dict): dictionary containing aws credentials
        resource_arn (string): string containing the resource arn for the cloned database
        secret_arn (str): string containing secret arn from update_secret task
    """
    rdsData = create_rds_data_client(aws_creds)

    json_list = []

    for query_name, query in QUERY_DICT.items():
        response = rdsData.execute_statement(
            resourceArn=resource_arn,
            secretArn=secret_arn,
            database="tower",
            includeResultMetadata=True,
            sql=query,
        )

        query_data = package_query_data(response)
        query_dict = {query_name: query_data}
        json_list.append(query_dict)

    return json_list


@task
def delete_clone_database(aws_creds: dict, clone_name: str):
    """
    deletes the cloned database cluster. takes ~2 min

    Args:
        aws_creds (dict):  dictionary containing aws credentials
        clone_name (str): name of the cloned database to be deleted
    """
    rds = create_rds_client(aws_creds)

    rds.delete_db_cluster(
        DBClusterIdentifier=clone_name,
        SkipFinalSnapshot=True,
    )

    # ensure deleting is complete before moving on
    check_database_process_complete(
        client=rds, waiter_type="db_cluster_deleted", db_name=clone_name
    )


@task
def export_json_to_synapse(json_list: list):
    """
    dumps JSON data to local file, uploads to synapse location, removes local file

    Args:
        json_list (list): list of dictionaries ready for JSON export
    """
    syn = create_synapse_session()
    file_name = "tower_metrics_report.json"
    with open(file_name, "w") as file:
        json.dump(json_list, file)
    data = synapseclient.File(file_name, parent="syn48186663")
    data = syn.store(data)
    os.remove(file_name)


@task
def send_synapse_notification():
    """
    sends email notification to synapse users in user_list that report has been uploaded
    """
    user_list = [
        "bwmac",  # Brad
        # "thomas.yu", # Tom
        # "bgrande", # Bruno
    ]

    syn = create_synapse_session()

    id_list = []
    for user in user_list:
        id_list.append(syn.getUserProfile(user).get("ownerId"))

    syn.sendMessage(
        id_list,
        "Nextflow Tower Metrics JSON Dump Complete",
        "A new Nextflow Tower Metrics report has been uploaded to https://www.synapse.org/#!Synapse:syn48186663",
    )
