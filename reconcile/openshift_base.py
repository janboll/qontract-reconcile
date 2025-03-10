import logging
import itertools

from typing import Optional, Iterable, Mapping

import yaml

from sretoolbox.utils import retry
from sretoolbox.utils import threaded

from reconcile import queries
from reconcile.utils.oc import FieldIsImmutableError
from reconcile.utils.oc import MayNotChangeOnceSetError
from reconcile.utils.oc import PrimaryClusterIPCanNotBeUnsetError
from reconcile.utils.oc import InvalidValueApplyError
from reconcile.utils.oc import MetaDataAnnotationsTooLongApplyError
from reconcile.utils.oc import StatefulSetUpdateForbidden
from reconcile.utils.oc import OC_Map
from reconcile.utils.oc import StatusCodeError
from reconcile.utils.oc import UnsupportedMediaTypeError
from reconcile.utils.openshift_resource import OpenshiftResource as OR
from reconcile.utils.openshift_resource import ResourceInventory


ACTION_APPLIED = 'applied'
ACTION_DELETED = 'deleted'


class ValidationError(Exception):
    pass


class ValidationErrorJobFailed(Exception):
    pass


class StateSpec:
    def __init__(self, type, oc, cluster, namespace, resource, parent=None,
                 resource_type_override=None, resource_names=None,
                 privileged=False):
        self.type = type
        self.oc = oc
        self.cluster = cluster
        self.namespace = namespace
        self.resource = resource
        self.parent = parent
        self.resource_type_override = resource_type_override
        self.resource_names = resource_names
        self.privileged = privileged


def init_specs_to_fetch(ri: ResourceInventory, oc_map: OC_Map,
                        namespaces: Optional[Iterable[Mapping]] = None,
                        clusters: Optional[Iterable[Mapping]] = None,
                        override_managed_types: Optional[Iterable[str]] = None,
                        managed_types_key: str = 'managedResourceTypes'
                        ) -> list[StateSpec]:
    state_specs = []

    if clusters and namespaces:
        raise KeyError('expected only one of clusters or namespaces.')
    elif namespaces:
        for namespace_info in namespaces:
            if override_managed_types is None:
                managed_types = set(namespace_info.get(managed_types_key)
                                    or [])
            else:
                managed_types = set(override_managed_types)

            if not managed_types:
                continue

            cluster = namespace_info['cluster']['name']
            privileged = namespace_info.get("clusterAdmin", False) is True
            oc = oc_map.get(cluster, privileged)
            if not oc:
                if oc.log_level >= logging.ERROR:
                    ri.register_error()
                logging.log(level=oc.log_level, msg=oc.message)
                continue

            namespace = namespace_info['name']
            # These may exit but have a value of None
            managed_resource_names = \
                namespace_info.get('managedResourceNames') or []
            managed_resource_type_overrides = \
                namespace_info.get('managedResourceTypeOverrides') or []

            # Initialize current state specs
            for resource_type in managed_types:
                ri.initialize_resource_type(cluster, namespace, resource_type)
            resource_names = {}
            resource_type_overrides = {}
            for mrn in managed_resource_names:
                # Current implementation guarantees only one
                # managed_resource_name of each managed type
                if mrn['resource'] in managed_types:
                    resource_names[mrn['resource']] = mrn['resourceNames']
                elif override_managed_types:
                    logging.debug(
                        f"Skipping resource {mrn['resource']} in {cluster}/"
                        f"{namespace} because the integration explicitly "
                        "dismisses it")
                else:
                    raise KeyError(
                        f"Non-managed resource name {mrn} listed on "
                        f"{cluster}/{namespace} (valid kinds: {managed_types})"
                    )

            for o in managed_resource_type_overrides:
                # Current implementation guarantees only one
                # override of each managed type
                if o['resource'] in managed_types:
                    resource_type_overrides[o['resource']] = o['override']
                elif override_managed_types:
                    logging.debug(
                        f"Skipping resource type override {o} listed on"
                        f"{cluster}/{namespace} because the integration "
                        "dismisses it explicitly"
                    )
                else:
                    raise KeyError(
                        f"Non-managed override {o} listed on "
                        f"{cluster}/{namespace} (valid kinds: {managed_types})"
                    )

            for kind, names in resource_names.items():
                c_spec = StateSpec(
                    "current", oc, cluster, namespace,
                    kind,
                    resource_type_override=resource_type_overrides.get(kind),
                    resource_names=names)
                state_specs.append(c_spec)
                managed_types.remove(kind)

            # Produce "empty" StateSpec's for any resource type that
            # doesn't have an explicit managedResourceName listed in
            # the namespace
            state_specs.extend(
                StateSpec(
                    "current",
                    oc,
                    cluster,
                    namespace,
                    t,
                    resource_type_override=resource_type_overrides.get(t),
                    resource_names=None
                ) for t in managed_types)

            # Initialize desired state specs
            openshift_resources = namespace_info.get('openshiftResources')
            for openshift_resource in openshift_resources or []:
                d_spec = StateSpec("desired", oc, cluster, namespace,
                                   openshift_resource, namespace_info,
                                   privileged=privileged)
                state_specs.append(d_spec)
    elif clusters:
        # set namespace to something indicative
        namespace = 'cluster'
        for cluster_info in clusters:
            cluster = cluster_info['name']
            oc = oc_map.get(cluster)
            if not oc:
                if oc.log_level >= logging.ERROR:
                    ri.register_error()
                logging.log(level=oc.log_level, msg=oc.message)
                continue

            # we currently only use override_managed_types,
            # and not allow a `managedResourcesTypes` field in a cluster file
            for resource_type in override_managed_types or []:
                ri.initialize_resource_type(cluster, namespace, resource_type)
                # Initialize current state specs
                c_spec = StateSpec("current", oc, cluster, namespace,
                                   resource_type)
                state_specs.append(c_spec)
                # Initialize desired state specs
                d_spec = StateSpec("desired", oc, cluster, namespace,
                                   resource_type)
                state_specs.append(d_spec)
    else:
        raise KeyError('expected one of clusters or namespaces.')

    return state_specs


def populate_current_state(spec, ri, integration, integration_version):
    oc = spec.oc
    if oc is None:
        return
    api_resources = oc.api_resources
    if api_resources and spec.resource not in api_resources:
        msg = f"[{spec.cluster}] cluster has no API resource {spec.resource}."
        logging.warning(msg)
        return
    try:
        for item in oc.get_items(spec.resource,
                                 namespace=spec.namespace,
                                 resource_names=spec.resource_names):
            openshift_resource = OR(item,
                                    integration,
                                    integration_version)
            ri.add_current(
                spec.cluster,
                spec.namespace,
                spec.resource,
                openshift_resource.name,
                openshift_resource
            )
    except StatusCodeError:
        ri.register_error(cluster=spec.cluster)


def fetch_current_state(namespaces=None,
                        clusters=None,
                        thread_pool_size=None,
                        integration=None,
                        integration_version=None,
                        override_managed_types=None,
                        internal=None,
                        use_jump_host=True,
                        init_api_resources=False,
                        cluster_admin=False):
    ri = ResourceInventory()
    settings = queries.get_app_interface_settings()
    oc_map = OC_Map(namespaces=namespaces,
                    clusters=clusters,
                    integration=integration,
                    settings=settings,
                    internal=internal,
                    use_jump_host=use_jump_host,
                    thread_pool_size=thread_pool_size,
                    init_api_resources=init_api_resources,
                    cluster_admin=cluster_admin)
    state_specs = \
        init_specs_to_fetch(
            ri,
            oc_map,
            namespaces=namespaces,
            clusters=clusters,
            override_managed_types=override_managed_types
        )
    threaded.run(populate_current_state, state_specs, thread_pool_size,
                 ri=ri,
                 integration=integration,
                 integration_version=integration_version)

    return ri, oc_map


@retry(max_attempts=20)
def wait_for_namespace_exists(oc, namespace):
    if not oc.project_exists(namespace):
        raise Exception(f'namespace {namespace} does not exist')


def apply(dry_run: bool, oc_map: OC_Map, cluster: str, namespace: str,
          resource_type: OR, resource, wait_for_namespace: bool,
          recycle_pods: bool = True, privileged: bool = False) -> None:
    logging.info(['apply', cluster, namespace, resource_type, resource.name])

    oc = oc_map.get(cluster, privileged)
    if not oc:
        logging.log(level=oc.log_level, msg=oc.message)
        return None
    if not dry_run:
        annotated = resource.annotate()
        # skip if namespace does not exist (as it will soon)
        # do not skip if this is a cluster scoped integration
        if namespace != 'cluster' and not oc.project_exists(namespace):
            msg = f"[{cluster}/{namespace}] namespace does not exist (yet)."
            if wait_for_namespace:
                logging.info(msg + ' waiting...')
                wait_for_namespace_exists(oc, namespace)
            else:
                logging.warning(msg)
                return

        try:
            oc.apply(namespace, annotated)
        except InvalidValueApplyError:
            oc.remove_last_applied_configuration(
                namespace, resource_type, resource.name)
            oc.apply(namespace, annotated)
        except (MetaDataAnnotationsTooLongApplyError,
                UnsupportedMediaTypeError):
            if not oc.get(namespace, resource_type,
                          resource.name, allow_not_found=True):
                oc.create(namespace, annotated)
            oc.replace(namespace, annotated)
        except FieldIsImmutableError:
            # Add more resources types to the list when you're
            # sure they're safe.
            if resource_type not in ['Route', 'Service', 'Secret']:
                raise

            oc.delete(namespace=namespace, kind=resource_type,
                      name=resource.name)
            oc.apply(namespace=namespace, resource=annotated)
        except (MayNotChangeOnceSetError, PrimaryClusterIPCanNotBeUnsetError):
            if resource_type not in ['Service']:
                raise

            oc.delete(namespace=namespace, kind=resource_type,
                      name=resource.name)
            oc.apply(namespace=namespace, resource=annotated)
        except StatefulSetUpdateForbidden:
            if resource_type != 'StatefulSet':
                raise

            logging.info(['delete_sts_and_apply', cluster, namespace,
                          resource_type, resource.name])
            current_resource = oc.get(namespace, resource_type, resource.name)
            current_storage = oc.get_storage(current_resource)
            desired_storage = oc.get_storage(resource.body)
            resize_required = current_storage != desired_storage
            if resize_required:
                owned_pods = oc.get_owned_pods(namespace, resource)
                owned_pvc_names = oc.get_pod_owned_pvc_names(owned_pods)
            oc.delete(namespace=namespace, kind=resource_type,
                      name=resource.name, cascade=False)
            oc.apply(namespace=namespace, resource=annotated)
            # the resource was applied without cascading.
            # if the change was in the storage, we need to
            # take care of the resize ourselves.
            # ref: https://github.com/kubernetes/enhancements/pull/2842
            if resize_required:
                logging.info(['resizing_pvcs', cluster, namespace,
                              owned_pvc_names])
                oc.resize_pvcs(namespace, owned_pvc_names, desired_storage)

    if recycle_pods:
        oc.recycle_pods(dry_run, namespace, resource_type, resource)


def create(dry_run, oc_map, cluster, namespace, resource_type, resource):
    logging.info(['create', cluster, namespace, resource_type, resource.name])

    oc = oc_map.get(cluster)
    if not oc:
        logging.log(level=oc.log_level, msg=oc.message)
        return None
    if not dry_run:
        annotated = resource.annotate()
        oc.create(namespace, annotated)


def delete(dry_run: bool, oc_map: OC_Map, cluster: str, namespace: str,
           resource_type: str, name: str, enable_deletion: bool,
           privileged: bool = False) -> None:
    logging.info(['delete', cluster, namespace, resource_type, name])

    if not enable_deletion:
        logging.error('\'delete\' action is disabled due to previous errors.')
        return

    oc = oc_map.get(cluster, privileged)
    if not oc:
        logging.log(level=oc.log_level, msg=oc.message)
        return None
    if not dry_run:
        oc.delete(namespace, resource_type, name)


def check_unused_resource_types(ri):
    for cluster, namespace, resource_type, data in ri:
        if not data['desired'].items():
            msg = f'[{cluster}/{namespace}] unused ' + \
                f'resource type: {resource_type}. please remove it ' + \
                'in a following PR.'
            logging.warning(msg)


def _realize_resource_data(unpacked_ri_item,
                           dry_run, oc_map: OC_Map,
                           ri: ResourceInventory,
                           take_over,
                           caller,
                           wait_for_namespace,
                           no_dry_run_skip_compare,
                           override_enable_deletion,
                           recycle_pods):
    cluster, namespace, resource_type, data = unpacked_ri_item
    actions: list[dict] = []
    if ri.has_error_registered(cluster=cluster):
        msg = (
            "[{}] skipping realize_data for "
            "cluster with errors"
        ).format(cluster)
        logging.error(msg)
        return actions

    enable_deletion = False if ri.has_error_registered() else True
    # only allow to override enable_deletion if no errors were found
    if enable_deletion is True and override_enable_deletion is False:
        enable_deletion = False

    # desired items
    for name, d_item in data['desired'].items():
        c_item = data['current'].get(name)

        if c_item is not None:
            if not dry_run and no_dry_run_skip_compare:
                msg = (
                    "[{}/{}] skipping compare of resource '{}/{}'."
                ).format(cluster, namespace, resource_type, name)
                logging.debug(msg)
            else:
                # If resource doesn't have annotations, annotate and apply
                if not c_item.has_qontract_annotations():
                    msg = (
                        "[{}/{}] resource '{}/{}' present "
                        "w/o annotations, annotating and applying"
                    ).format(cluster, namespace, resource_type, name)
                    logging.info(msg)

                # don't apply if resources match
                # if there is a caller (saas file) and this is a take over
                # we skip the equal compare as it's not covering
                # cases of a removed label (for example)
                # d_item == c_item is uncommutative
                elif not (caller and take_over) and d_item == c_item:
                    msg = (
                        "[{}/{}] resource '{}/{}' present "
                        "and matches desired, skipping."
                    ).format(cluster, namespace, resource_type, name)
                    logging.debug(msg)
                    continue

                # don't apply if sha256sum hashes match
                elif c_item.sha256sum() == d_item.sha256sum():
                    if c_item.has_valid_sha256sum():
                        msg = (
                            "[{}/{}] resource '{}/{}' present "
                            "and hashes match, skipping."
                        ).format(cluster, namespace, resource_type, name)
                        logging.debug(msg)
                        continue
                    else:
                        msg = (
                            "[{}/{}] resource '{}/{}' present and "
                            "has stale sha256sum due to manual changes."
                        ).format(cluster, namespace, resource_type, name)
                        logging.info(msg)

                logging.debug("CURRENT: " +
                              OR.serialize(OR.canonicalize(c_item.body)))
        else:
            logging.debug("CURRENT: None")

        logging.debug("DESIRED: " +
                      OR.serialize(OR.canonicalize(d_item.body)))

        try:
            privileged = data['use_admin_token'].get(name, False)
            apply(dry_run, oc_map, cluster, namespace,
                  resource_type, d_item, wait_for_namespace,
                  recycle_pods, privileged)
            action = {
                'action': ACTION_APPLIED,
                'cluster': cluster,
                'namespace': namespace,
                'kind': resource_type,
                'name': d_item.name,
                'privileged': privileged
            }
            actions.append(action)
        except StatusCodeError as e:
            ri.register_error()
            err = str(e) if resource_type != 'Secret' \
                else f'error applying Secret {d_item.name}: REDACTED'
            msg = f"[{cluster}/{namespace}] {err} " + \
                f"(error details: {d_item.error_details})"
            logging.error(msg)

    # current items
    for name, c_item in data['current'].items():
        d_item = data['desired'].get(name)
        if d_item is not None:
            continue

        if c_item.has_qontract_annotations():
            if caller and c_item.caller != caller:
                continue
        elif not take_over:
            # this is reached when the current resources:
            # - does not have qontract annotations (not managed)
            # - not taking over all resources of the current kind
            msg = f"[{cluster}/{namespace}] skipping " +\
                f"{resource_type}/{c_item.name}"
            logging.debug(msg)
            continue

        if c_item.has_owner_reference():
            continue

        try:
            privileged = data['use_admin_token'].get(name, False)
            delete(dry_run, oc_map, cluster, namespace,
                   resource_type, name, enable_deletion, privileged)
            action = {
                'action': ACTION_DELETED,
                'cluster': cluster,
                'namespace': namespace,
                'kind': resource_type,
                'name': name,
                'privileged': privileged
            }
            actions.append(action)
        except StatusCodeError as e:
            ri.register_error()
            msg = "[{}/{}] {}".format(cluster, namespace, str(e))
            logging.error(msg)

    return actions


def realize_data(dry_run, oc_map: OC_Map, ri: ResourceInventory,
                 thread_pool_size,
                 take_over=False,
                 caller=None,
                 wait_for_namespace=False,
                 no_dry_run_skip_compare=False,
                 override_enable_deletion=None,
                 recycle_pods=True):
    """
    Realize the current state to the desired state.

    :param dry_run: run in dry-run mode
    :param oc_map: a dictionary containing oc client per cluster
    :param ri: a ResourceInventory containing current and desired states
    :param thread_pool_size: Thread pool size to use for parallelism
    :param take_over: manage resource types in a namespace exclusively
    :param caller: name of the calling entity.
                   enables multiple running instances of the same integration
                   to deploy to the same namespace
    :param wait_for_namespace: wait for namespace to exist before applying
    :param no_dry_run_skip_compare: when running without dry-run, skip compare
    :param override_enable_deletion: override calculated enable_deletion value
    :param recycle_pods: should pods be recycled if a dependency changed
    """
    args = locals()
    del args['thread_pool_size']
    results = threaded.run(_realize_resource_data, ri, thread_pool_size,
                           **args)
    return list(itertools.chain.from_iterable(results))


@retry(exceptions=(ValidationError), max_attempts=100)
def validate_data(oc_map, actions):
    """
    Validate the realized desired state.

    :param oc_map: a dictionary containing oc client per cluster
    :param actions: a dictionary of performed actions
    """

    supported_kinds = [
        'Deployment',
        'DeploymentConfig',
        'StatefulSet',
        'Subscription',
        'Job',
        'ClowdApp',
        'ClowdJobInvocation'
    ]
    for action in actions:
        if action['action'] == ACTION_APPLIED:
            kind = action['kind']
            if kind not in supported_kinds:
                continue
            cluster = action['cluster']
            namespace = action['namespace']
            name = action['name']
            logging.info(['validating', cluster, namespace, kind, name])

            oc = oc_map.get(cluster)
            if not oc:
                logging.log(level=oc.log_level, msg=oc.message)
                continue
            resource = oc.get(namespace, kind, name=name)
            status = resource.get('status')
            if not status:
                raise ValidationError('status')
            # add elif to validate additional resource kinds
            if kind in ['Deployment', 'DeploymentConfig', 'StatefulSet']:
                desired_replicas = resource['spec']['replicas']
                if desired_replicas == 0:
                    continue
                replicas = status.get('replicas')
                if replicas == 0:
                    continue
                updated_replicas = status.get('updatedReplicas')
                ready_replicas = status.get('readyReplicas')
                if not desired_replicas == replicas == \
                        ready_replicas == updated_replicas:
                    logging.info(
                        f'{kind} {name} has replicas that are not ready '
                        f'({ready_replicas} ready / {desired_replicas} total)')
                    raise ValidationError(name)
            elif kind == 'Subscription':
                state = status.get('state')
                if state != 'AtLatestKnown':
                    logging.info(
                        f'Subscription {name} state is invalid. '
                        f'Current state: {state}')
                    raise ValidationError(name)
            elif kind == 'Job':
                succeeded = status.get('succeeded')
                if not succeeded:
                    logging.info(f'Job {name} has not succeeded')
                    conditions = status.get('conditions')
                    if conditions:
                        logging.info(f'Job conditions are: {conditions}')
                        logging.info(yaml.safe_dump(conditions))
                        for c in conditions:
                            if c.get('type') == 'Failed':
                                msg = f"{name}: {c.get('reason')}"
                                raise ValidationErrorJobFailed(msg)
                    raise ValidationError(name)
            elif kind == 'ClowdApp':
                deployments = status.get('deployments')
                if not deployments:
                    logging.info(
                        'ClowdApp has no deployments, status is invalid')
                    raise ValidationError(name)
                managed_deployments = deployments.get('managedDeployments')
                ready_deployments = deployments.get('readyDeployments')
                if managed_deployments != ready_deployments:
                    logging.info(
                        f'ClowdApp has deployments that are not ready '
                        f'({ready_deployments} ready / '
                        f'{managed_deployments} total)')
                    raise ValidationError(name)
            elif kind == 'ClowdJobInvocation':
                completed = status.get('completed')
                jobs = status.get('jobMap', {})
                if jobs:
                    logging.info(f'CJI {name} jobs are: {jobs}')
                    logging.info(yaml.safe_dump(jobs))
                if completed:
                    failed_jobs = []
                    for job_name, job_state in jobs.items():
                        if job_state == 'Failed':
                            failed_jobs.append(job_name)
                    if failed_jobs:
                        raise ValidationErrorJobFailed(
                            f'CJI {name} failed jobs: {failed_jobs}')
                else:
                    logging.info(f'CJI {name} has not completed')
                    conditions = status.get('conditions')
                    if conditions:
                        logging.info(f'CJI conditions are: {conditions}')
                        logging.info(yaml.safe_dump(conditions))
                    raise ValidationError(name)


def follow_logs(oc_map, actions, io_dir):
    """
    Collect the logs from the owned pods into files in io_dir.

    :param oc_map: a dictionary containing oc client per cluster
    :param actions: a dictionary of performed actions
    :param io_dir: a directory to store the logs as files
    """

    supported_kinds = [
        'Job',
        'ClowdJobInvocation'
    ]
    for action in actions:
        if action['action'] == ACTION_APPLIED:
            kind = action['kind']
            if kind not in supported_kinds:
                continue
            cluster = action['cluster']
            namespace = action['namespace']
            name = action['name']
            logging.info(['collecting', cluster, namespace, kind, name])

            oc = oc_map.get(cluster)
            if not oc:
                logging.log(level=oc.log_level, msg=oc.message)
                continue

            if kind == 'Job':
                oc.job_logs(namespace, name, follow=True, output=io_dir)
            if kind == 'ClowdJobInvocation':
                resource = oc.get(namespace, kind, name=name)
                jobs = resource.get('status', {}).get('jobMap', {})
                for jn in jobs:
                    logging.info(['collecting', cluster, namespace, kind, jn])
                    oc.job_logs(namespace, jn, follow=True, output=io_dir)


def aggregate_shared_resources(namespace_info, shared_resources_type):
    """ This function aggregates shared resources of the desired type
    from a shared resources file to the appropriate namespace section. """
    supported_shared_resources_types = [
        'openshiftResources',
        'openshiftServiceAccountTokens'
    ]
    if shared_resources_type not in supported_shared_resources_types:
        raise KeyError(
            f'shared_resource_type must be one of '
            f'{supported_shared_resources_types}.'
        )
    shared_resources = namespace_info.get('sharedResources')
    namespace_type_resources = namespace_info.get(shared_resources_type)
    if shared_resources:
        shared_type_resources_items = []
        for shared_resources_item in shared_resources:
            shared_type_resources = \
                shared_resources_item.get(shared_resources_type)
            if shared_type_resources:
                shared_type_resources_items.extend(shared_type_resources)
        if namespace_type_resources:
            namespace_type_resources.extend(shared_type_resources_items)
        else:
            namespace_type_resources = shared_type_resources_items
            namespace_info[shared_resources_type] = namespace_type_resources
