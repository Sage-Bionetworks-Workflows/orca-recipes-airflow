import os
import json
import time

from airflow.decorators import task
from airflow.models import Variable
import boto3
import synapseclient

#VARIABLES

# AWS creds
AWS_CREDS = {
    "AWS_ACCESS_KEY_ID": os.environ.get(
        "AWS_ACCESS_KEY_ID"
    ),
    "AWS_SECRET_ACCESS_KEY": os.environ.get(
        "AWS_SECRET_ACCESS_KEY"
    ),
    "AWS_SESSION_TOKEN": os.environ.get(
        "AWS_SESSION_TOKEN"
    ),
}

# cluster names
DATABASE_NAME = "tower"
CLONE_DATABASE_NAME = DATABASE_NAME + "-clone"
# database name - name of actual database within cluster
DATABASE_NAME = "tower"

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

#FUNCTIONS

# takes responses from query requests and packages them into dictionaries to be combined into JSON later
def package_query_data(query_name: str, response: dict) -> dict:
    """
    takes responses from query requests and packages them into dictionaries to be combined into JSON later

    Args:
        query_name (string): name of the query to be used as the key in output JSON file
        response (dict): dictionary request response from boto3

    Returns:
        dict: JSON-ready dictionary to be exported as report file
    """
    col_headers = []
    for item in response["columnMetadata"]:
        col_headers.append(item.get("name"))
    # handle single-value queries
    if len(col_headers) < 2:
        final_dict = {query_name: list(response["records"][0][0].values())[0]}
        return final_dict
    # handle n-value queries
    else:
        data_dict = {}
        for item in response["records"]:
            key = list(item[0].values())[0]
            sub_dict = {key: {}}
            for i in [x for x in range(len(col_headers)) if x != 0]:
                sub_dict[key].update({col_headers[i]: list(item[i].values())[0]})
            data_dict.update(sub_dict)
        final_dict = {query_name: data_dict}
        return final_dict


# creates RDS boto3 client
def create_rds_client(aws_creds):
    rds = boto3.client(
        "rds",
        aws_access_key_id=aws_creds["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=aws_creds["AWS_SECRET_ACCESS_KEY"],
        aws_session_token=aws_creds["AWS_SESSION_TOKEN"],
    )
    return rds


# creates RDS Data boto3 client
def create_rds_data_client(aws_creds):
    rdsData = boto3.client(
        "rds-data",
        aws_access_key_id=aws_creds["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=aws_creds["AWS_SECRET_ACCESS_KEY"],
        aws_session_token=aws_creds["AWS_SESSION_TOKEN"],
    )
    return rdsData


# creates Secrets Manager boto3 client
def create_secret_client(aws_creds):
    secrets = boto3.client(
        "secretsmanager",
        aws_access_key_id=aws_creds["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=aws_creds["AWS_SECRET_ACCESS_KEY"],
        aws_session_token=aws_creds["AWS_SESSION_TOKEN"],
    )
    return secrets


def wait_seconds(seconds: int):
    """
    wait for number of seconds provided to 'seconds' argument of function

    Args:
        seconds (int): number of seconds to wait
    """
    time.sleep(seconds)


def check_database_status(aws_creds: dict, db_name: str) -> str:
    """
    Checks availability status of database cluster

    Args:
        aws_creds (dict): dictionary containing aws credentials
        db_name (str):  name of database cluster to be checked

    Returns:
        str: status of database cluster
    """
    rds = create_rds_client(aws_creds)

    response = rds.describe_db_clusters(
        DBClusterIdentifier=db_name,
    )
    status = response["DBClusters"][0].get("Status")
    return status


# create authenticated synapse session
def create_synapse_session():
    syn = synapseclient.Synapse()
    syn.login(authToken=Variable.get("SYNAPSE_AUTH_TOKEN"))
    return syn

#TASKS

@task
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
    subnet_group = response["DBClusters"][0].get("DBSubnetGroup")
    security_group = response["DBClusters"][0]["VpcSecurityGroups"][0].get(
        "VpcSecurityGroupId"
    )
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
    clone db - takes 5-7 min
    creates complete db clone from latest restorable time.
    this function was chosen over others as it has the least overhead through only
    using the parent db itself, no snapshot or full recreation required.

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
        "user": response["DBCluster"].get("MasterUsername"),
        "host": response["DBCluster"].get("Endpoint"),
        "resource_id": response["DBCluster"].get("DbClusterResourceId"),
        "resource_arn": response["DBCluster"].get("DBClusterArn"),
    }
    # ensure cloning is complete before moving on
    wait_seconds(360)  # it takes six minutes on average for clone to spin up
    while True:
        status = check_database_status(
            aws_creds=AWS_CREDS, db_name=CLONE_DATABASE_NAME
        )
        if status == "available":
            break
        else:
            wait_seconds(
                60
            )  # wait a further minute per loop if modification is not complete

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
    # secrets = create_secret_client(aws_creds)

    # response = secrets.get_random_password(
    #     PasswordLength=30,
    #     ExcludeCharacters="@",
    #     ExcludePunctuation=True,
    #     IncludeSpace=False,
    #     RequireEachIncludedType=True,
    # )
    # password = response["RandomPassword"]
    password = "password"
    print(password)
    return password

@task
def modify_cloned_cluster(aws_creds: dict, clone_name: str, password: str):
    """
    takes 1-2 minutes
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
    wait_seconds(120)  # it takes two minutes on average for clone to be modified
    while True:
        status = check_database_status(
            aws_creds=AWS_CREDS, db_name=CLONE_DATABASE_NAME
        )
        if status == "available":
            break
        else:
            wait_seconds(
                60
            )  # wait a further minute per loop if modification is not complete

@task
def update_secret(
    aws_creds: dict, clone_name: str, db_info: dict, password: str
) -> str:
    """
    takes 1-2 minutes
    updates secret in secretmanager with formatted string includung new database info and random password

    Args:
        aws_creds (dict): dictionary containing aws credentials
        clone_name (str): name of cloned database cluster
        db_info (dict): dictionary with information from cloned database returned from clone_tower_cluster
        password (str): randomly generated password assigned to cloned database

    Returns:
        str: secret arn string
    """
    secrets = create_secret_client(aws_creds)

    secret_string = (
        "{"
        + f'"dbInstanceIdentifier":"{clone_name}","engine":"aurora-mysql","host":"{db_info["host"]}","port":3306,"resourceId":"{db_info["resource_id"]}","username":"{db_info["user"]}","password":"{password}"'
        + "}"
    )
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

        json_list.append(package_query_data(query_name, response))

    return json_list

@task
def delete_clone_database(aws_creds: dict, clone_name: str):
    """
    deletes the cloned database cluster. takes ~2 min

    Args:
        aws_creds (dict): _description_
        db_info (dict): _description_
    """
    rds = create_rds_client(aws_creds)

    rds.delete_db_cluster(
        DBClusterIdentifier=clone_name,
        SkipFinalSnapshot=True,
    )

    # ensure deleting is complete before moving on
    wait_seconds(120)  # it takes two minutes on average for clone to spin down
    while True:
        try:
            check_database_status(aws_creds=AWS_CREDS, db_name=CLONE_DATABASE_NAME)
            wait_seconds(
                60
            )  # wait a further minute per try if the database still exists
        except:
            # we actually want an error - or here an 'except'. this indicates that the database does not exist
            # TODO add logic to look for the expected error message
            break

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
    data = synapseclient.File(
        file_name, parent="syn48186663"
    )  # investigate how dump can work without creating json file on disk
    data = syn.store(data)
    os.remove(file_name)

@task
def send_synapse_notification():
    """
    sends email notification to chosen synapse users that report has been uploaded
    """
    user_list = [
        "bwmac",  # Brad
        # "thomas.yu", #tom
    ]
    id_list = []
    for user in user_list:
        id_list.append(syn.getUserProfile(user).get("ownerId"))
    user_list

    syn = create_synapse_session()

    syn.sendMessage(
        id_list,
        "Nextflow Tower Metrics JSON Dump Complete",
        "A new Nextflow Tower Metrics report has been uploaded to https://www.synapse.org/#!Synapse:syn48186663",
    )