import functools
import json
import logging
import os
import time

from datetime import datetime
from threading import Lock
from typing import Literal, Union, TYPE_CHECKING
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from boto3 import Session
from sretoolbox.utils import threaded
import botocore

import reconcile.utils.lean_terraform_client as terraform

from reconcile.utils.secret_reader import SecretReader

if TYPE_CHECKING:
    from mypy_boto3_ec2 import EC2Client
    from mypy_boto3_ec2.type_defs import (
        RouteTableTypeDef, SubnetTypeDef, TransitGatewayTypeDef,
        TransitGatewayVpcAttachmentTypeDef, VpcTypeDef
    )
    from mypy_boto3_iam import IAMClient
    from mypy_boto3_iam.type_defs import AccessKeyMetadataTypeDef
else:
    EC2Client = RouteTableTypeDef = SubnetTypeDef = TransitGatewayTypeDef = \
        TransitGatewayVpcAttachmentTypeDef = VpcTypeDef = IAMClient = \
        AccessKeyMetadataTypeDef = object


class InvalidResourceTypeError(Exception):
    pass


class MissingARNError(Exception):
    pass


Account = Dict[str, Any]
KeyStatus = Union[Literal['Active'], Literal['Inactive']]


class AWSApi:
    """Wrapper around AWS SDK"""

    def __init__(self, thread_pool_size, accounts, settings=None,
                 init_ecr_auth_tokens=False, init_users=True):
        self.thread_pool_size = thread_pool_size
        self.secret_reader = SecretReader(settings=settings)
        self.init_sessions_and_resources(accounts)
        if init_ecr_auth_tokens:
            self.init_ecr_auth_tokens(accounts)
        if init_users:
            self.init_users()
        self._lock = Lock()
        self.resource_types = \
            ['s3', 'sqs', 'dynamodb', 'rds', 'rds_snapshots']

        # store the app-interface accounts in a dictionary indexed by name
        self.accounts = {acc['name']: acc for acc in accounts}

        # Setup caches on the instance itself to avoid leak
        # https://stackoverflow.com/questions/33672412/python-functools-lru-cache-with-class-methods-release-object
        # using @lru_cache decorators on methods would lek AWSApi instances
        # since the cache keeps a reference to self.
        self._account_ec2_client = functools.lru_cache()(
            self._account_ec2_client)
        self._get_assumed_role_client = functools.lru_cache()(
            self._get_assumed_role_client)
        self.get_account_vpcs = functools.lru_cache()(
            self.get_account_vpcs)
        self.get_vpc_route_tables = functools.lru_cache()(
            self.get_vpc_route_tables)
        self.get_vpc_subnets = functools.lru_cache()(
            self.get_vpc_subnets)
        self.get_vpc_default_sg_id = functools.lru_cache()(
            self.get_vpc_default_sg_id)
        self.get_transit_gateways = functools.lru_cache()(
            self.get_transit_gateways)
        self.get_transit_gateway_vpc_attachments = functools.lru_cache()(
            self.get_transit_gateway_vpc_attachments)

    def init_sessions_and_resources(self, accounts: Iterable[Account]):
        results = threaded.run(self.get_tf_secrets, accounts,
                               self.thread_pool_size)
        self.sessions: Dict[str, Session] = {}
        self.resources: Dict[str, Any] = {}
        for account, secret in results:
            access_key = secret['aws_access_key_id']
            secret_key = secret['aws_secret_access_key']
            region_name = secret['region']
            session = Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region_name,
            )
            self.sessions[account] = session
            self.resources[account] = {}

    def get_session(self, account: str) -> Session:
        return self.sessions[account]

    # pylint: disable=method-hidden
    def _account_ec2_client(self, account_name: str,
                            region_name: Optional[str] = None) -> EC2Client:
        session = self.get_session(account_name)
        region = region_name if region_name else session.region_name
        return session.client('ec2', region_name=region)

    def get_tf_secrets(self, account):
        account_name = account['name']
        automation_token = account['automationToken']
        secret = self.secret_reader.read_all(automation_token)
        return (account_name, secret)

    def init_users(self):
        self.users = {}
        for account, s in self.sessions.items():
            iam = s.client('iam')
            users = self.paginate(iam, 'list_users', 'Users')
            users = [u['UserName'] for u in users]
            self.users[account] = users

    def simulate_deleted_users(self, io_dir):
        src_integrations = ['terraform_resources', 'terraform_users']
        if not os.path.exists(io_dir):
            return
        for i in src_integrations:
            file_path = os.path.join(io_dir, i + '.json')
            if not os.path.exists(file_path):
                continue
            with open(file_path, 'r') as f:
                deleted_users = json.load(f)
            for deleted_user in deleted_users:
                delete_from_account = deleted_user['account']
                delete_user = deleted_user['user']
                self.users[delete_from_account].remove(delete_user)

    def map_resources(self):
        threaded.run(self.map_resource, self.resource_types,
                     self.thread_pool_size)

    def map_resource(self, resource_type):
        if resource_type == 's3':
            self.map_s3_resources()
        elif resource_type == 'sqs':
            self.map_sqs_resources()
        elif resource_type == 'dynamodb':
            self.map_dynamodb_resources()
        elif resource_type == 'rds':
            self.map_rds_resources()
        elif resource_type == 'rds_snapshots':
            self.map_rds_snapshots()
        elif resource_type == 'route53':
            self.map_route53_resources()
        else:
            raise InvalidResourceTypeError(resource_type)

    def map_s3_resources(self):
        for account, s in self.sessions.items():
            s3 = s.client('s3')
            buckets_list = s3.list_buckets()
            if 'Buckets' not in buckets_list:
                continue
            buckets = [b['Name'] for b in buckets_list['Buckets']]
            self.set_resouces(account, 's3', buckets)
            buckets_without_owner = \
                self.get_resources_without_owner(account, buckets)
            unfiltered_buckets = \
                self.custom_s3_filter(account, s3, buckets_without_owner)
            self.set_resouces(account, 's3_no_owner', unfiltered_buckets)

    def map_sqs_resources(self):
        for account, s in self.sessions.items():
            sqs = s.client('sqs')
            queues_list = sqs.list_queues()
            if 'QueueUrls' not in queues_list:
                continue
            queues = queues_list['QueueUrls']
            self.set_resouces(account, 'sqs', queues)
            queues_without_owner = \
                self.get_resources_without_owner(account, queues)
            unfiltered_queues = \
                self.custom_sqs_filter(account, sqs, queues_without_owner)
            self.set_resouces(account, 'sqs_no_owner', unfiltered_queues)

    def map_dynamodb_resources(self):
        for account, s in self.sessions.items():
            dynamodb = s.client('dynamodb')
            tables = self.paginate(dynamodb, 'list_tables', 'TableNames')
            self.set_resouces(account, 'dynamodb', tables)
            tables_without_owner = \
                self.get_resources_without_owner(account, tables)
            unfiltered_tables = \
                self.custom_dynamodb_filter(
                    account,
                    s,
                    dynamodb,
                    tables_without_owner
                )
            self.set_resouces(account, 'dynamodb_no_owner', unfiltered_tables)

    def map_rds_resources(self):
        for account, s in self.sessions.items():
            rds = s.client('rds')
            results = \
                self.paginate(rds, 'describe_db_instances', 'DBInstances')
            instances = [t['DBInstanceIdentifier'] for t in results]
            self.set_resouces(account, 'rds', instances)
            instances_without_owner = \
                self.get_resources_without_owner(account, instances)
            unfiltered_instances = \
                self.custom_rds_filter(account, rds, instances_without_owner)
            self.set_resouces(account, 'rds_no_owner', unfiltered_instances)

    def map_rds_snapshots(self):
        self.wait_for_resource('rds')
        for account, s in self.sessions.items():
            rds = s.client('rds')
            results = \
                self.paginate(rds, 'describe_db_snapshots', 'DBSnapshots')
            snapshots = [t['DBSnapshotIdentifier'] for t in results]
            self.set_resouces(account, 'rds_snapshots', snapshots)
            snapshots_without_db = [t['DBSnapshotIdentifier'] for t in results
                                    if t['DBInstanceIdentifier'] not in
                                    self.resources[account]['rds']]
            unfiltered_snapshots = \
                self.custom_rds_snapshot_filter(account, rds,
                                                snapshots_without_db)
            self.set_resouces(account, 'rds_snapshots_no_owner',
                              unfiltered_snapshots)

    def map_route53_resources(self):
        for account, s in self.sessions.items():
            client = s.client('route53')
            results = \
                self.paginate(client, 'list_hosted_zones', 'HostedZones')
            zones = list(results)
            for zone in zones:
                results = \
                    self.paginate(client, 'list_resource_record_sets',
                                          'ResourceRecordSets',
                                          {'HostedZoneId': zone['Id']})
                zone['records'] = results
            self.set_resouces(account, 'route53', zones)

    def map_ecr_resources(self):
        for account, s in self.sessions.items():
            client = s.client('ecr')
            repositories = self.paginate(client=client,
                                         method='describe_repositories',
                                         key='repositories')
            self.set_resouces(account, 'ecr', repositories)

    @staticmethod
    def paginate(client, method, key, params={}):
        """ paginate returns an aggregated list of the specified key
        from all pages returned by executing the client's specified method."""
        paginator = client.get_paginator(method)
        return [values
                for page in paginator.paginate(**params)
                for values in page.get(key, [])]

    def wait_for_resource(self, resource):
        """ wait_for_resource waits until the specified resource type
        is ready for all accounts.
        When we have more resource types then threads,
        this function will need to change to a dependency graph."""
        wait = True
        while wait:
            wait = False
            for account in self.sessions:
                if self.resources[account].get(resource) is None:
                    wait = True
            if wait:
                time.sleep(2)

    def set_resouces(self, account, key, value):
        with self._lock:
            self.resources[account][key] = value

    def get_resources_without_owner(self, account, resources):
        return [r for r in resources if not self.has_owner(account, r)]

    def has_owner(self, account, resource):
        has_owner = False
        for u in self.users[account]:
            if resource.lower().startswith(u.lower()):
                has_owner = True
                break
            if '://' in resource:
                if resource.split('/')[-1].startswith(u.lower()):
                    has_owner = True
                    break
        return has_owner

    def custom_s3_filter(self, account, s3, buckets):
        type = 's3 bucket'
        unfiltered_buckets = []
        for b in buckets:
            try:
                tags = s3.get_bucket_tagging(Bucket=b)
            except botocore.exceptions.ClientError:
                tags = {}
            if not self.should_filter(account, type, b, tags, 'TagSet'):
                unfiltered_buckets.append(b)

        return unfiltered_buckets

    def custom_sqs_filter(self, account, sqs, queues):
        type = 'sqs queue'
        unfiltered_queues = []
        for q in queues:
            tags = sqs.list_queue_tags(QueueUrl=q)
            if not self.should_filter(account, type, q, tags, 'Tags'):
                unfiltered_queues.append(q)

        return unfiltered_queues

    def custom_dynamodb_filter(self, account, session, dynamodb, tables):
        type = 'dynamodb table'
        dynamodb_resource = session.resource('dynamodb')
        unfiltered_tables = []
        for t in tables:
            table_arn = dynamodb_resource.Table(t).table_arn
            tags = dynamodb.list_tags_of_resource(ResourceArn=table_arn)
            if not self.should_filter(account, type, t, tags, 'Tags'):
                unfiltered_tables.append(t)

        return unfiltered_tables

    def custom_rds_filter(self, account, rds, instances):
        type = 'rds instance'
        unfiltered_instances = []
        for i in instances:
            instance = rds.describe_db_instances(DBInstanceIdentifier=i)
            instance_arn = instance['DBInstances'][0]['DBInstanceArn']
            tags = rds.list_tags_for_resource(ResourceName=instance_arn)
            if not self.should_filter(account, type, i, tags, 'TagList'):
                unfiltered_instances.append(i)

        return unfiltered_instances

    def custom_rds_snapshot_filter(self, account, rds, snapshots):
        type = 'rds snapshots'
        unfiltered_snapshots = []
        for s in snapshots:
            snapshot = rds.describe_db_snapshots(DBSnapshotIdentifier=s)
            snapshot_arn = snapshot['DBSnapshots'][0]['DBSnapshotArn']
            tags = rds.list_tags_for_resource(ResourceName=snapshot_arn)
            if not self.should_filter(account, type, s, tags, 'TagList'):
                unfiltered_snapshots.append(s)

        return unfiltered_snapshots

    def should_filter(self, account, resource_type,
                      resource_name, resource_tags, tags_key):
        if self.resource_has_special_name(account, resource_type,
                                          resource_name):
            return True
        if tags_key in resource_tags:
            tags = resource_tags[tags_key]
            if self.resource_has_special_tags(account, resource_type,
                                              resource_name, tags):
                return True

        return False

    @staticmethod
    def resource_has_special_name(account, type, resource):
        skip_msg = '[{}] skipping {} '.format(account, type) + \
            '({} related) {}'

        ignore_names = {
            'production': ['prod'],
            'stage': ['stage', 'staging'],
            'terraform': ['terraform', '-tf-'],
        }

        for msg, tags in ignore_names.items():
            for tag in tags:
                if tag.lower() in resource.lower():
                    logging.debug(skip_msg.format(msg, resource))
                    return True

        return False

    def resource_has_special_tags(self, account, type, resource, tags):
        skip_msg = '[{}] skipping {} '.format(account, type) + \
            '({}={}) {}'

        ignore_tags = {
            'ENV': ['prod', 'stage', 'staging'],
            'environment': ['prod', 'stage', 'staging'],
            'owner': ['app-sre'],
            'managed_by_integration': [
                'terraform_resources',
                'terraform_users'
            ],
            'aws_gc_hands_off': ['true'],
        }

        for tag, ignore_values in ignore_tags.items():
            for ignore_value in ignore_values:
                value = self.get_tag_value(tags, tag)
                if ignore_value.lower() in value.lower():
                    logging.debug(skip_msg.format(tag, value, resource))
                    return True

        return False

    @staticmethod
    def get_tag_value(tags, tag):
        if isinstance(tags, dict):
            return tags.get(tag, '')
        elif isinstance(tags, list):
            for t in tags:
                if t['Key'] == tag:
                    return t['Value']

        return ''

    def delete_resources_without_owner(self, dry_run):
        for account, s in self.sessions.items():
            for rt in self.resource_types:
                for r in self.resources[account].get(rt + '_no_owner', []):
                    logging.info(['delete_resource', account, rt, r])
                    if not dry_run:
                        self.delete_resource(s, rt, r)

    def delete_resource(self, session, resource_type, resource_name):
        if resource_type == 's3':
            resource = session.resource(resource_type)
            self.delete_bucket(resource, resource_name)
        elif resource_type == 'sqs':
            client = session.client(resource_type)
            self.delete_queue(client, resource_name)
        elif resource_type == 'dynamodb':
            resource = session.resource(resource_type)
            self.delete_table(resource, resource_name)
        elif resource_type == 'rds':
            client = session.client(resource_type)
            self.delete_instance(client, resource_name)
        elif resource_type == 'rds_snapshots':
            client = session.client(resource_type)
            self.delete_snapshot(client, resource_name)
        else:
            raise InvalidResourceTypeError(resource_type)

    @staticmethod
    def delete_bucket(s3, bucket_name):
        bucket = s3.Bucket(bucket_name)
        bucket.object_versions.delete()
        bucket.delete()

    @staticmethod
    def delete_queue(sqs, queue_url):
        sqs.delete_queue(QueueUrl=queue_url)

    @staticmethod
    def delete_table(dynamodb, table_name):
        table = dynamodb.Table(table_name)
        table.delete()

    @staticmethod
    def delete_instance(rds, instance_name):
        rds.delete_db_instance(
            DBInstanceIdentifier=instance_name,
            SkipFinalSnapshot=True,
            DeleteAutomatedBackups=True
        )

    @staticmethod
    def delete_snapshot(rds, snapshot_identifier):
        rds.delete_db_snapshot(
            DBSnapshotIdentifier=snapshot_identifier
        )

    @staticmethod
    def determine_key_type(iam, user):
        tags = iam.list_user_tags(UserName=user)['Tags']
        managed_by_integration_tag = \
            [t['Value'] for t in tags
             if t['Key'] == 'managed_by_integration']
        # if this key belongs to a user without tags, i.e. not
        # managed by an integration, this key is probably created
        # manually. disable it to leave a trace
        if not managed_by_integration_tag:
            return 'unmanaged'
        # if this key belongs to a user created by the
        # 'terraform-users' integration, we just delete the key
        if managed_by_integration_tag[0] == 'terraform_users':
            return 'user'
        # if this key belongs to a user created by the
        # 'terraform-resources' integration, we remove
        # the key from terraform state and let it create
        # a new one on its own
        if managed_by_integration_tag[0] == 'terraform_resources':
            return 'service_account'

        huh = 'unrecognized managed_by_integration tag: {}'.format(
            managed_by_integration_tag[0])
        raise InvalidResourceTypeError(huh)

    def delete_keys(self, dry_run, keys_to_delete, working_dirs,
                    disable_service_account_keys):
        error = False
        users_keys = self.get_users_keys()
        for account, s in self.sessions.items():
            iam = s.client('iam')
            keys = keys_to_delete.get(account, [])
            for key in keys:
                user_and_user_keys = [(user, user_keys) for user, user_keys
                                      in users_keys[account].items()
                                      if key in user_keys]
                if not user_and_user_keys:
                    continue
                # unpack single item from sequence
                # since only a single user can have a given key
                [user_and_user_keys] = user_and_user_keys
                user = user_and_user_keys[0]
                user_keys = user_and_user_keys[1]
                key_type = self.determine_key_type(iam, user)
                key_status = self.get_user_key_status(iam, user, key)
                if key_type == 'unmanaged' and key_status == 'Active':
                    logging.info(['disable_key', account, user, key])

                    if not dry_run:
                        iam.update_access_key(
                            UserName=user,
                            AccessKeyId=key,
                            Status='Inactive'
                        )
                elif key_type == 'user':
                    logging.info(['delete_key', account, user, key])

                    if not dry_run:
                        iam.delete_access_key(
                            UserName=user,
                            AccessKeyId=key
                        )
                elif key_type == 'service_account':
                    # if key is disabled - delete it
                    # this will happen after terraform-resources ran,
                    # provisioned a new key, updated the output Secret,
                    # recycled the pods and disabled the key.
                    if key_status == 'Inactive':
                        logging.info(['delete_inactive_key',
                                     account, user, key])
                        if not dry_run:
                            iam.delete_access_key(
                                UserName=user,
                                AccessKeyId=key
                            )
                        continue

                    # if key is active and it is the only one -
                    # remove it from terraform state. terraform-resources
                    # will provision a new one.
                    # may be a race condition here. TODO: check it
                    if len(user_keys) == 1:
                        logging.info(['remove_from_state',
                                      account, user, key])
                        if not dry_run:
                            terraform.state_rm_access_key(
                                working_dirs, account, user
                            )

                    # if user has 2 keys and we remove the key from
                    # terraform state, terraform-resources will not
                    # be able to provision a new key - limbo.
                    # this state should happen when terraform-resources
                    # is running, provisioned a new key,
                    # but did not disable the old key yet.
                    if len(user_keys) == 2:
                        # if true, this is a call made by terraform-resources
                        # itself. disable the key and proceed. the key will be
                        # deleted in a following iteration of aws-iam-keys.
                        if disable_service_account_keys:
                            logging.info(['disable_key', account, user, key])

                            if not dry_run:
                                iam.update_access_key(
                                    UserName=user,
                                    AccessKeyId=key,
                                    Status='Inactive'
                                )
                        else:
                            msg = \
                                'user {} has 2 keys, skipping to avoid error'
                            logging.error(msg.format(user))
                            error = True

        return error

    def get_users_keys(self):
        users_keys = {}
        for account, s in self.sessions.items():
            iam = s.client('iam')
            users_keys[account] = {user: self.get_user_keys(iam, user)
                                   for user in self.users[account]}

        return users_keys

    def reset_password(self, account, user_name):
        s = self.sessions[account]
        iam = s.client('iam')
        iam.delete_login_profile(UserName=user_name)

    def reset_mfa(self, account, user_name):
        s = self.sessions[account]
        iam = s.client('iam')
        mfa_devices = iam.list_mfa_devices(UserName=user_name)['MFADevices']
        for d in mfa_devices:
            serial_number = d['SerialNumber']
            iam.deactivate_mfa_device(
                UserName=user_name,
                SerialNumber=serial_number
            )
            iam.delete_virtual_mfa_device(SerialNumber=serial_number)

    @staticmethod
    def _get_user_key_list(iam: IAMClient,
                           user: str
                           ) -> List[AccessKeyMetadataTypeDef]:
        try:
            return iam.list_access_keys(UserName=user)['AccessKeyMetadata']
        except iam.exceptions.NoSuchEntityException:
            return []

    def get_user_keys(self,
                      iam: IAMClient,
                      user: str
                      ) -> list[str]:
        key_list = self._get_user_key_list(iam, user)
        return [uk['AccessKeyId'] for uk in key_list]

    def get_user_key_status(self,
                            iam: IAMClient,
                            user: str,
                            key: str
                            ) -> KeyStatus:
        key_list = self._get_user_key_list(iam, user)
        return [k['Status'] for k in key_list if k['AccessKeyId'] == key][0]

    def get_support_cases(self):
        all_support_cases = {}
        for account, s in self.sessions.items():
            if not self.accounts[account].get('premiumSupport'):
                continue
            try:
                support = s.client('support')
                support_cases = support.describe_cases(
                    includeResolvedCases=True,
                    includeCommunications=True
                )['cases']
                all_support_cases[account] = support_cases
            except Exception as e:
                msg = '[{}] error getting support cases. details: {}'
                logging.error(msg.format(account, str(e)))

        return all_support_cases

    def init_ecr_auth_tokens(self, accounts):
        accounts_with_ecr = [a for a in accounts if a.get('ecrs')]
        if not accounts_with_ecr:
            return

        auth_tokens = {}
        results = threaded.run(self.get_tf_secrets, accounts_with_ecr,
                               self.thread_pool_size)
        account_secrets = dict(results)
        for account in accounts_with_ecr:
            account_name = account['name']
            account_secret = account_secrets[account_name]
            access_key = account_secret['aws_access_key_id']
            secret_key = account_secret['aws_secret_access_key']

            ecrs = account['ecrs']
            for ecr in ecrs:
                region_name = ecr['region']
                session = Session(
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    region_name=region_name,
                )
                client = session.client('ecr')
                token = client.get_authorization_token()
                auth_tokens[f"{account_name}/{region_name}"] = token

        self.auth_tokens = auth_tokens

    @staticmethod
    def _get_account_assume_data(account: Account) -> Tuple[str, str, str]:
        """
        returns mandatory data to be able to assume a role with this account:
        (account_name, assume_role, assume_region)
        """
        required_keys = \
            ['name', 'assume_role', 'assume_region']
        ok = all(elem in account.keys() for elem in required_keys)
        if not ok:
            account_name = account.get('name')
            raise KeyError(
                '[{}] account is missing required keys'.format(account_name))
        return (account['name'], account['assume_role'],
                account['assume_region'])

    def _get_assume_role_session(self, account_name: str, assume_role: str,
                                 assume_region: str) -> Session:
        """
        Returns a session for a supplied role to assume:

        :param name:          name of the AWS account
        :param assume_role:   role to assume to get access
                              to the cluster's AWS account
        :param assume_region: region in which to operate
        """
        session = self.get_session(account_name)
        sts = session.client('sts')
        if not assume_role:
            raise MissingARNError(
                f'Could not find Role ARN {assume_role} on account '
                f'{account_name}. This is likely caused by a missing '
                'awsInfrastructureAccess section.'
            )
        role_name = assume_role.split('/')[1]
        response = sts.assume_role(
            RoleArn=assume_role,
            RoleSessionName=role_name
        )
        credentials = response['Credentials']

        assumed_session = Session(
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'],
            region_name=assume_region
        )

        return assumed_session

    # pylint: disable=method-hidden
    def _get_assumed_role_client(self, account_name: str, assume_role: str,
                                 assume_region: str,
                                 client_type='ec2') -> EC2Client:
        assumed_session = self._get_assume_role_session(account_name,
                                                        assume_role,
                                                        assume_region)
        return assumed_session.client(client_type)

    @staticmethod
    # pylint: disable=method-hidden
    def get_account_vpcs(ec2: EC2Client) -> List[VpcTypeDef]:
        vpcs = ec2.describe_vpcs()
        return vpcs.get('Vpcs', [])

    # filters a list of aws resources according to tags
    @staticmethod
    def filter_on_tags(items: Iterable[Any], tags: Mapping[str, str] = {}) \
            -> List[Any]:
        res = []
        for item in items:
            tags_dict = {t['Key']: t['Value'] for t in item.get('Tags', [])}
            if all(tags_dict.get(k) == values for k, values in tags.items()):
                res.append(item)
        return res

    @staticmethod
    # pylint: disable=method-hidden
    def get_vpc_route_tables(vpc_id: str, ec2: EC2Client) \
            -> List[RouteTableTypeDef]:
        rts = ec2.describe_route_tables(
                Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
        return rts.get('RouteTables', [])

    @staticmethod
    # pylint: disable=method-hidden
    def get_vpc_subnets(vpc_id: str, ec2: EC2Client) \
            -> List[SubnetTypeDef]:
        subnets = ec2.describe_subnets(
                    Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
        return subnets.get('Subnets', [])

    def get_cluster_vpc_details(self, account, route_tables=False,
                                subnets=False):
        """
        Returns a cluster VPC details:
            - VPC ID
            - Route table IDs (optional)
            - Subnets list including Subnet ID and Subnet Availability zone
        :param account: a dictionary containing the following keys:
                        - name - name of the AWS account
                        - assume_role - role to assume to get access
                                        to the cluster's AWS account
                        - assume_region - region in which to operate
                        - assume_cidr - CIDR block of the cluster to
                                        use to find the matching VPC
        """
        assume_role_data = self._get_account_assume_data(account)
        assumed_ec2 = self._get_assumed_role_client(*assume_role_data)
        vpcs = self.get_account_vpcs(assumed_ec2)
        vpc_id = None
        for vpc in vpcs:
            if vpc['CidrBlock'] == account['assume_cidr']:
                vpc_id = vpc['VpcId']
                break

        route_table_ids = None
        subnets_id_az = None
        if vpc_id:
            if route_tables:
                vpc_route_tables = \
                    self.get_vpc_route_tables(vpc_id, assumed_ec2)
                route_table_ids = [rt['RouteTableId']
                                   for rt in vpc_route_tables]
            if subnets:
                vpc_subnets = self.get_vpc_subnets(vpc_id, assumed_ec2)
                subnets_id_az = \
                    [
                        {
                            'id': s['SubnetId'],
                            'az': s['AvailabilityZone']
                        }
                        for s in vpc_subnets
                    ]

        return vpc_id, route_table_ids, subnets_id_az

    def get_cluster_nat_gateways_egress_ips(self, account):
        assumed_role_data = self._get_account_assume_data(account)
        assumed_ec2 = self._get_assumed_role_client(*assumed_role_data)
        nat_gateways = assumed_ec2.describe_nat_gateways()
        egress_ips = set()
        for nat in nat_gateways.get('NatGateways'):
            for address in nat['NatGatewayAddresses']:
                egress_ips.add(address['PublicIp'])

        return egress_ips

    def get_vpcs_details(self, account, tags=None, route_tables=False):
        results = []
        ec2 = self._account_ec2_client(account['name'])
        regions = [r['RegionName'] for r in ec2.describe_regions()['Regions']]
        for region_name in regions:
            ec2 = self._account_ec2_client(account['name'], region_name)
            vpcs = self.get_account_vpcs(ec2)
            vpcs = self.filter_on_tags(vpcs, tags)
            for vpc in vpcs:
                vpc_id = vpc['VpcId']
                cidr_block = vpc['CidrBlock']
                route_table_ids = None
                if route_tables:
                    vpc_route_tables = self.get_vpc_route_tables(vpc_id, ec2)
                    route_table_ids = [rt['RouteTableId']
                                       for rt
                                       in vpc_route_tables]
                item = {
                    'vpc_id': vpc_id,
                    'region': region_name,
                    'cidr_block': cidr_block,
                    'route_table_ids': route_table_ids,
                }
                results.append(item)

        return results

    def get_alb_network_interface_ips(self, account, service_name):
        assumed_role_data = self._get_account_assume_data(account)
        ec2_client = self._get_assumed_role_client(*assumed_role_data, 'ec2')
        elb_client = self._get_assumed_role_client(*assumed_role_data, 'elb')
        service_tag = \
            {'Key': 'kubernetes.io/service-name', 'Value': service_name}
        nis = ec2_client.describe_network_interfaces()['NetworkInterfaces']
        lbs = elb_client.describe_load_balancers()['LoadBalancerDescriptions']
        result_ips = set()
        for lb in lbs:
            lb_name = lb['LoadBalancerName']
            tag_descriptions = elb_client.describe_tags(
                LoadBalancerNames=[lb_name]
            )['TagDescriptions']
            for td in tag_descriptions:
                tags = td['Tags']
                if service_tag not in tags:
                    continue
                # found a load balancer we want to work with
                # find all network interfaces related to it
                for ni in nis:
                    if ni['Description'] != f"ELB {lb_name}":
                        continue
                    if ni['Status'] != 'in-use':
                        continue
                    # found a network interface!
                    ip = ni['PrivateIpAddress']
                    result_ips.add(ip)

        return result_ips

    @staticmethod
    # pylint: disable=method-hidden
    def get_vpc_default_sg_id(vpc_id: str, ec2: EC2Client) -> Optional[str]:
        vpc_security_groups = ec2.describe_security_groups(
            Filters=[
                {'Name': 'vpc-id', 'Values': [vpc_id]},
                {'Name': 'group-name', 'Values': ['default']}
            ]
        )
        # there is only one default
        for sg in vpc_security_groups.get('SecurityGroups', []):
            return sg['GroupId']

        return None

    @staticmethod
    # pylint: disable=method-hidden
    def get_transit_gateways(ec2: EC2Client) -> List[TransitGatewayTypeDef]:
        tgws = ec2.describe_transit_gateways()
        return tgws.get('TransitGateways', [])

    def get_tgw_default_route_table_id(self, ec2: EC2Client, tgw_id: str,
                                       tags: Mapping[str, str]) \
            -> Optional[str]:
        tgws = self.get_transit_gateways(ec2)
        tgws = self.filter_on_tags(tgws, tags)
        # we know the party TGW exists, so we can be
        # a little less catious about getting it
        [tgw] = [t for t in tgws if t['TransitGatewayId'] == tgw_id]
        tgw_options = tgw['Options']
        tgw_has_route_table = \
            tgw_options['DefaultRouteTableAssociation'] == 'enable'
        # currently only adding routes
        # to the default route table
        if tgw_has_route_table:
            return tgw_options['AssociationDefaultRouteTableId']

        return None

    @staticmethod
    # pylint: disable=method-hidden
    def get_transit_gateway_vpc_attachments(tgw_id: str, ec2: EC2Client) \
            -> List[TransitGatewayVpcAttachmentTypeDef]:
        atts = ec2.describe_transit_gateway_vpc_attachments(
                Filters=[
                    {'Name': 'transit-gateway-id', 'Values': [tgw_id]}
                ])
        return atts.get('TransitGatewayVpcAttachments', [])

    def get_tgws_details(self, account, region_name, routes_cidr_block,
                         tags=None, route_tables=False,
                         security_groups=False):
        results = []
        ec2 = self._account_ec2_client(account['name'], region_name)
        tgws = ec2.describe_transit_gateways(
            Filters=[
                {'Name': f'tag:{k}', 'Values': [v]}
                for k, v in tags.items()
            ]
        )
        for tgw in tgws.get('TransitGateways'):
            tgw_id = tgw['TransitGatewayId']
            tgw_arn = tgw['TransitGatewayArn']
            item = {
                'tgw_id': tgw_id,
                'tgw_arn': tgw_arn,
                'region': region_name,
            }

            if route_tables or security_groups:
                # both routes and rules are provisioned for resources
                # that are indirectly attached to the TGW.
                # routes are provisioned for route tables that belong
                # to TGWs which are peered to the TGW we are currently
                # handling.
                routes = []
                # rules are provisioned for security groups that belong
                # to VPCs which are attached to the TGW we are currently
                # handling AND to TGWs which are peered to it.
                rules = []
                # this will require to iterate over all reachable TGWs
                attachments = \
                    ec2.describe_transit_gateway_peering_attachments(
                        Filters=[
                            {'Name': 'transit-gateway-id',
                             'Values': [tgw_id]}
                        ]
                    )
                for a in attachments.get('TransitGatewayPeeringAttachments'):
                    tgw_attachment_id = a['TransitGatewayAttachmentId']
                    tgw_attachment_state = a['State']
                    if tgw_attachment_state != 'available':
                        continue
                    # we don't care who is who, so let's iterate over parties
                    attachment_parties = \
                        [a['RequesterTgwInfo'], a['AccepterTgwInfo']]
                    for party in attachment_parties:
                        if party['OwnerId'] != account['uid']:
                            # TGW attachment to another account, skipping
                            continue
                        party_tgw_id = party['TransitGatewayId']
                        party_region = party['Region']
                        party_ec2 = self._account_ec2_client(account['name'],
                                                             party_region)

                        # the TGW route table is automatically populated
                        # with the peered VPC cidr block.
                        # however, to achieve global routing across peered
                        # TGWs in different regions, we need to find all
                        # peering attachments in different regions and collect
                        # the data to later create a route in each peered TGW
                        # in a different region. this will require getting:
                        # - cluster cidr block
                        # - transit gateway attachment id
                        # - transit gateway route table id
                        # we will also pass some additional information:
                        # - transit gateway id
                        # - transit gateway region
                        if route_tables:
                            # don't act on yourself and
                            # routes are propogated within the same region
                            if party_tgw_id != tgw_id and \
                                    party_region != region_name:
                                party_tgw_route_table_id = \
                                    self.get_tgw_default_route_table_id(
                                        party_ec2, party_tgw_id, tags)
                                if party_tgw_route_table_id is not None:
                                    # that's it, we have all
                                    # the information we need
                                    route_item = {
                                        'cidr_block': routes_cidr_block,
                                        'tgw_attachment_id':
                                            tgw_attachment_id,
                                        'tgw_id': party_tgw_id,
                                        'tgw_route_table_id':
                                            party_tgw_route_table_id,
                                        'region': party_region
                                    }
                                    routes.append(route_item)

                        # once all the routing is in place, we need to allow
                        # connections in security groups.
                        # in TGW, we need to allow the rules in the VPCs
                        # associated to the TGWs that need to accept the
                        # traffic. we need to collect data about the vpc
                        # attachments for the TGWs, and for each VPC get
                        # the details of it's default securiry group.
                        # this will require getting:
                        # - cluster cidr block
                        # - security group id
                        # we will also pass some additional information:
                        # - vpc id
                        # - vpc region
                        if security_groups:
                            vpc_attachments = \
                                self.get_transit_gateway_vpc_attachments(
                                    party_tgw_id, party_ec2)
                            for va in vpc_attachments:
                                vpc_attachment_vpc_id = va['VpcId']
                                vpc_attachment_state = va['State']
                                if vpc_attachment_state != 'available':
                                    continue
                                sg_id = self.get_vpc_default_sg_id(
                                    vpc_attachment_vpc_id, party_ec2)
                                if sg_id is not None:
                                    # that's it, we have all
                                    # the information we need
                                    rule_item = {
                                        'cidr_block': routes_cidr_block,
                                        'security_group_id': sg_id,
                                        'vpc_id': vpc_attachment_vpc_id,
                                        'region': party_region
                                    }
                                    rules.append(rule_item)

                if route_tables:
                    item['routes'] = routes
                if security_groups:
                    item['rules'] = rules

            results.append(item)

        return results

    def get_route53_zones(self):
        """
        Return a list of (str, dict) representing Route53 DNS zones per account

        :return: route53 dns zones per account
        :rtype: list of (str, dict)
        """
        return {
            account: self.resources.get(account, {}).get('route53', [])
            for account, _ in self.sessions.items()
        }

    def create_route53_zone(self, account_name, zone_name):
        """
        Create a Route53 DNS zone

        :param account_name: the account name to operate on
        :param zone_name: name of the zone to create
        :type account_name: str
        :type zone_name: str
        """
        session = self.get_session(account_name)
        client = session.client('route53')

        try:
            caller_ref = f"{datetime.now()}"
            client.create_hosted_zone(
                Name=zone_name,
                CallerReference=caller_ref,
                HostedZoneConfig={
                    'Comment': 'Managed by App-Interface',
                },
            )
        except client.exceptions.InvalidDomainName:
            logging.error(f'[{account_name}] invalid domain name {zone_name}')
        except client.exceptions.HostedZoneAlreadyExists:
            logging.error(
                f'[{account_name}] hosted zone already exists: {zone_name}'
            )
        except client.exceptions.TooManyHostedZones:
            logging.error(f'[{account_name}] too many hosted zones in account')
        except Exception as e:
            logging.error(f'[{account_name}] unhandled exception: {e}')

    def delete_route53_zone(self, account_name, zone_id):
        """
        Delete a Route53 DNS zone

        :param account_name: the account name to operate on
        :param zone_id: aws zone id of the zone to delete
        :type account_name: str
        :type zone_id: str
        """
        session = self.get_session(account_name)
        client = session.client('route53')

        try:
            client.delete_hosted_zone(Id=zone_id)
        except client.exceptions.NoSuchHostedZone:
            logging.error(f'[{account_name}] Error trying to delete '
                          f'unknown DNS zone {zone_id}')
        except client.exceptions.HostedZoneNotEmpty:
            logging.error(f'[{account_name}] Cannot delete DNS zone that '
                          f'is not empty {zone_id}')
        except Exception as e:
            logging.error(f'[{account_name}] unhandled exception: {e}')

    def delete_route53_record(self, account_name, zone_id, awsdata):
        """
        Delete a Route53 DNS zone record

        :param account_name: the account name to operate on
        :param zone_id: aws zone id of the zone to operate on
        :param awsdata: aws record data of the record to delete
        :type account_name: str
        :type zone_id: str
        :type awsdata: dict
        """
        session = self.get_session(account_name)
        client = session.client('route53')

        try:
            client.change_resource_record_sets(
                HostedZoneId=zone_id,
                ChangeBatch={
                    'Changes': [{
                        'Action': 'DELETE',
                        'ResourceRecordSet': awsdata,
                    }]
                }
            )
        except client.exceptions.NoSuchHostedZone:
            logging.error(f'[{account_name}] Error trying to delete record: '
                          f'unknown DNS zone {zone_id}')
        except Exception as e:
            logging.error(f'[{account_name}] unhandled exception: {e}')

    def upsert_route53_record(self, account_name, zone_id, recordset):
        """
        Upsert a Route53 DNS zone record

        :param account_name: the account name to operate on
        :param zone_id: aws zone id of the zone to operate on
        :param recordset: aws record data of the record to create or update
        :type account_name: str
        :type zone_id: str
        :type recordset: dict
        """
        session = self.get_session(account_name)
        client = session.client('route53')

        try:
            client.change_resource_record_sets(
                HostedZoneId=zone_id,
                ChangeBatch={
                    'Changes': [{
                        'Action': 'UPSERT',
                        'ResourceRecordSet': recordset,
                    }]
                }
            )
        except client.exceptions.NoSuchHostedZone:
            logging.error(f'[{account_name}] Error trying to delete record: '
                          f'unknown DNS zone {zone_id}')
        except Exception as e:
            logging.error(f'[{account_name}] unhandled exception: {e}')
