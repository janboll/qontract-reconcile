import logging
import textwrap
import threading
from typing import Set, Any, Optional

from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter, Retry

from sretoolbox.utils import retry

from sentry_sdk import capture_exception

from reconcile.utils.config import get_config
from reconcile.status import RunningState

from gql import gql, Client
from gql.transport.requests import RequestsHTTPTransport
from gql.transport.exceptions import TransportQueryError
from gql.transport.requests import log as requests_logger

_gqlapi = None
_local_client = None

INTEGRATIONS_QUERY = """
{
    integrations: integrations_v1 {
        name
        description
        schemas
    }
}
"""

requests_logger.setLevel(logging.WARNING)


def capture_and_forget(error):
    """fire-and-forget an exception to sentry

    :param error: exception to be captured and sent to sentry
    :type error: Exception
    """

    try:
        capture_exception(error)
    except Exception:
        pass


class GqlApiError(Exception):
    pass


class GqlApiIntegrationNotFound(Exception):
    def __init__(self, integration):
        msg = f"""
        Integration not found: {integration}

        Integration should be defined in App-Interface with the
        /app-sre/integration-1.yml schema.
        """
        super().__init__(textwrap.dedent(msg).strip())


class GqlApiErrorForbiddenSchema(Exception):
    def __init__(self, schemas):
        msg = f"""
        Forbidden schemas: {schemas}

        The `schemas` parameter in the integration file in App-Interface
        should be updated to include these schemas.
        """
        super().__init__(textwrap.dedent(msg).strip())


class GqlGetResourceError(Exception):
    def __init__(self, path, msg):
        super().__init__(
            "Error getting resource from path {}: {}".format(path, str(msg))
        )


class GqlApi:
    _valid_schemas: list[str] = []
    _queried_schemas: Set[Any] = set()

    def __init__(
        self,
        url: str,
        token: Optional[str] = None,
        int_name=None,
        validate_schemas=False,
    ) -> None:
        self.url = url
        self.token = token
        self.integration = int_name
        self.validate_schemas = validate_schemas

        if validate_schemas and not int_name:
            raise Exception(
                "Cannot validate schemas if integration name " "is not supplied"
            )

        if int_name:
            integrations = self.query(INTEGRATIONS_QUERY, skip_validation=True)

            for integration in integrations["integrations"]:
                if integration["name"] == int_name:
                    self._valid_schemas = integration["schemas"]
                    break

            if not self._valid_schemas:
                raise GqlApiIntegrationNotFound(int_name)

    @retry(exceptions=GqlApiError, max_attempts=5, hook=capture_and_forget)
    def query(
        self, query: str, variables=None, skip_validation=False
    ) -> Optional[dict[str, Any]]:
        client = _init_gql_client(self.url, self.token)
        try:
            result = client.execute(
                gql(query), variables, get_execution_result=True
            ).formatted
        except requests.exceptions.ConnectionError as e:
            raise GqlApiError("Could not connect to GraphQL server ({})".format(e))
        except TransportQueryError as e:
            raise GqlApiError("`error` returned with GraphQL response {}".format(e))
        except AssertionError:
            raise GqlApiError("`data` field missing from GraphQL response payload")
        except Exception as e:
            raise GqlApiError("Unexpected error occurred") from e

        # show schemas if log level is debug
        query_schemas = result.get("extensions", {}).get("schemas", [])
        self._queried_schemas.update(query_schemas)

        for s in query_schemas:
            logging.debug(["schema", s])

        if self.validate_schemas and not skip_validation:
            forbidden_schemas = [
                schema for schema in query_schemas if schema not in self._valid_schemas
            ]
            if forbidden_schemas:
                raise GqlApiErrorForbiddenSchema(forbidden_schemas)

        # This is to appease mypy. This exception won't be thrown as this condition
        # is already handled above with AssertionError
        if result["data"] is None:
            raise GqlApiError("`data` not received in GraphQL payload")

        return result["data"]

    def get_resource(self, path: str) -> dict[str, Any]:
        query = """
        query Resource($path: String) {
            resources: resources_v1 (path: $path) {
                path
                content
                sha256sum
            }
        }
        """

        try:
            # Do not validate schema in resources since schema support in the
            # resources is not complete.
            resources = self.query(query, {"path": path}, skip_validation=True)[
                "resources"
            ]
        except GqlApiError:
            raise GqlGetResourceError(path, "Resource not found.")

        if len(resources) != 1:
            raise GqlGetResourceError(path, "Expecting one and only one resource.")

        return resources[0]

    def get_queried_schemas(self):
        return list(self._queried_schemas)


def init(
    url: str, token: Optional[str] = None, integration=None, validate_schemas=False
):
    global _gqlapi
    _gqlapi = GqlApi(url, token, integration, validate_schemas)
    return _gqlapi


def get_resource(path: str) -> dict[str, Any]:
    return get_api().get_resource(path)


class PersistentRequestsHTTPTransport(RequestsHTTPTransport):
    def connect(self):
        if self.session is None:
            # Copied over from RequestsHTTPTransport
            self.session = requests.Session()
            if self.retries > 0:
                adapter = HTTPAdapter(
                    max_retries=Retry(
                        total=self.retries,
                        backoff_factor=0.1,
                        status_forcelist=[500, 502, 503, 504],
                    )
                )
            for prefix in "http://", "https://":
                self.session.mount(prefix, adapter)

    def close(self) -> None:
        pass


def _init_gql_client(url: str, token: Optional[str]) -> Client:
    global _local_client
    if not _local_client:
        _local_client = threading.local()

    req_headers = None
    if token:
        # The token stored in vault is already in the format 'Basic ...'
        req_headers = {"Authorization": token}
    client = getattr(_local_client, "client", None)
    if not client:
        # Here we are explicitly using sync strategy
        _local_client.client = Client(
            transport=PersistentRequestsHTTPTransport(
                url, headers=req_headers, timeout=5, retries=3
            )
        )
    return _local_client.client


@retry(exceptions=requests.exceptions.HTTPError, max_attempts=5)
def get_sha(server, token=None):
    sha_endpoint = server._replace(path="/sha256")
    headers = {"Authorization": token} if token else None
    response = requests.get(sha_endpoint.geturl(), headers=headers)
    response.raise_for_status()
    sha = response.content.decode("utf-8")
    return sha


@retry(exceptions=requests.exceptions.HTTPError, max_attempts=5)
def get_git_commit_info(sha, server, token=None):
    git_commit_info_endpoint = server._replace(path=f"/git-commit-info/{sha}")
    headers = {"Authorization": token} if token else None
    response = requests.get(git_commit_info_endpoint.geturl(), headers=headers)
    response.raise_for_status()
    git_commit_info = response.json()
    return git_commit_info


@retry(exceptions=requests.exceptions.ConnectionError, max_attempts=5)
def init_from_config(
    sha_url=True, integration=None, validate_schemas=False, print_url=True
):
    config = get_config()

    server_url = urlparse(config["graphql"]["server"])
    server = server_url.geturl()

    token = config["graphql"].get("token")
    if sha_url:
        sha = get_sha(server_url, token)
        server = server_url._replace(path=f"/graphqlsha/{sha}").geturl()

        runing_state = RunningState()
        git_commit_info = get_git_commit_info(sha, server_url, token)
        runing_state.timestamp = git_commit_info.get("timestamp")
        runing_state.commit = git_commit_info.get("commit")

    if print_url:
        logging.info(f"using gql endpoint {server}")
    return init(server, token, integration, validate_schemas)


def get_api():
    global _gqlapi

    if not _gqlapi:
        raise GqlApiError("gql module has not been initialized.")

    return _gqlapi
