import logging
import os
import sys

from reconcile.change_owners.self_service_roles import fetch_self_service_roles

from reconcile.change_owners.change_owners import fetch_change_type_processors, \
    cover_changes

from reconcile.change_owners.bundle import (
    QontractServerFileDiffResolver,
)

from reconcile.change_owners.changes import (
    fetch_bundle_changes,
)
from reconcile.utils import gql
from reconcile.utils.semver_helper import make_semver


QONTRACT_INTEGRATION = "change-admission"
QONTRACT_INTEGRATION_VERSION = make_semver(0, 1, 0)


def run(
    dry_run: bool,
    comparison_sha: str,
) -> None:
    comparison_gql_api = gql.get_api_for_sha(
        comparison_sha, QONTRACT_INTEGRATION, validate_schemas=False
    )


    file_diff_resolver = QontractServerFileDiffResolver(comparison_sha=comparison_sha)

    try:
        # fetch change-types from current bundle to verify they are syntactically correct.
        # this is a cheap way to figure out if a newly introduced change-type works.
        # needs a lot of improvements!
        fetch_change_type_processors(gql.get_api(), file_diff_resolver)
        # also verify that self service roles are configured correctly, e.g. if change-types
        # are brought together only with compatible schema files
        fetch_self_service_roles(gql.get_api())
    except Exception as e:
        logging.error(e)
        sys.exit(1)

    # get change types from the comparison bundle to prevent privilege escalation
    logging.info(
        f"fetching change types and permissions from comparison bundle "
        f"(sha={comparison_sha}, commit_id={comparison_gql_api.commit}, "
        f"build_time {comparison_gql_api.commit_timestamp_utc})"
    )
    change_type_processors = fetch_change_type_processors(
        comparison_gql_api, file_diff_resolver
    )

    #
    #   C H A N G E   C O V E R A G E
    #
    changes = fetch_bundle_changes(comparison_sha)
    logging.info(
        f"detected {len(changes)} changed files "
        f"with {sum(c.raw_diff_count() for c in changes)} differences "
        f"and {len([c for c in changes if c.metadata_only_change])} metadata-only changes"
    )
    cover_changes(
        changes,
        change_type_processors,
        comparison_gql_api,
    )

    for change in changes:
        for dc in change.diff_coverage:
            for c in dc.coverage:
                if c.change_type_processor.restrictive:
                    approvers = { a.org_username for a in c.approvers}
                    if os.environ["gitlabUserEmail"].split("@")[0] not in approvers:
                        logging.error(
                            f"change type {c.change_type_processor.name} "
                            f"because {c.change_type_processor.restrictive}"
                        )
                        sys.exit(1)


