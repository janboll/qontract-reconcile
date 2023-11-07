from typing import Optional, Callable

from pydantic import BaseModel

from reconcile.aws_resources.resources import sqs_from_query
from reconcile.gql_definitions.aws_resources.aws_resources import query, \
    NamespaceV1
from reconcile.queries import get_aws_accounts
from reconcile.utils import gql

QONTRACT_INTEGRATION = "aws-resources"




def aws_accounts(account_names: list[str]) -> list[dict]:
    return [
        a
        for a in get_aws_accounts(cleanup=True)
        if account_names == a["name"]
    ]


def get_namespaces(query_func: Optional[Callable] = None) -> list[NamespaceV1]:
    if not query_func:
        query_func = gql.get_api().query
    data = query(query_func=query_func)
    return list(data.namespaces or [])


def get_desired():
    for namespace in get_namespaces():
        for provider in [p  for p in namespace.external_resources if p.provider == "aws-resource"]:
            for a in provider.resources:
                x= sqs_from_query(a)
                for cf in x.queues:
                    print(cf)
                print(x.has_diff())



def run(dry_run: bool = False, thread_pool_size: int = 10) -> None:
    """
    state:
        key: $cluster_$namespace
        value: $account_$resource_name $arn

    get_current() -> list[SQSQueue] # get current state from AWS
    get_known() -> list[SQSQueue] # get arn from s3
    get_desired() -> list[SQSQueue] # get desired state from app-interface

    diff:
        for resource in desired:
            if resource not in current:
                SQSQueueHandler(resource).create()

        for resource in known:
            if resource not in desired:
                SQSQueueHandler(resource).delete()


    """

    get_desired()


