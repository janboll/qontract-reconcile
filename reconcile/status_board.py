import logging
from abc import (
    ABC,
    abstractmethod,
)
from typing import (
    Any,
    Iterable,
    Mapping,
    Optional,
)

from pydantic import BaseModel

from reconcile.gql_definitions.status_board.status_board import StatusBoardV1
from reconcile.typed_queries.status_board import (
    get_selected_app_names,
    get_status_board,
)
from reconcile.utils.ocm.status_board import (
    create_application,
    create_product,
    delete_application,
    delete_product,
    get_managed_products,
    get_product_applications,
)
from reconcile.utils.ocm_base_client import (
    OCMBaseClient,
    init_ocm_base_client,
)
from reconcile.utils.runtime.integration import QontractReconcileIntegration

QONTRACT_INTEGRATION = "status-board-exporter"


class AbstractStatusBoard(ABC, BaseModel):
    """Abstract class for upgrade policies
    Used to create and delete upgrade policies in OCM."""

    id: Optional[str]
    name: str
    fullname: str
    metadata: Optional[dict[str, Any]]

    @abstractmethod
    def create(self, ocm: OCMBaseClient) -> None:
        pass

    @abstractmethod
    def delete(self, ocm: OCMBaseClient) -> None:
        pass

    @abstractmethod
    def summarize(self) -> str:
        pass


class Product(AbstractStatusBoard):
    applications: Optional[list["Application"]]

    def create(self, ocm: OCMBaseClient) -> None:
        spec = self.dict(by_alias=True)
        spec.pop("applications")
        spec.pop("id")
        create_product(ocm, spec)

    def delete(self, ocm: OCMBaseClient) -> None:
        if not self.id:
            logging.error(f'Trying to delete Product "{self.name}" without id')
            return
        delete_product(ocm, self.id)

    def summarize(self) -> str:
        return f'Product: "{self.name}"'


class Application(AbstractStatusBoard):
    product: Optional["Product"]

    def create(self, ocm: OCMBaseClient) -> None:
        spec = self.dict(by_alias=True)
        spec.pop("id")
        product = spec.pop("product")
        spec["product"] = {"id": product["id"]}
        create_application(ocm, spec)

    def delete(self, ocm: OCMBaseClient) -> None:
        if not self.id:
            logging.error(f'Trying to delete Application "{self.name}" without id')
            return
        delete_application(ocm, self.id)

    def summarize(self) -> str:
        return f'Application: "{self.name}" "{self.fullname}"'


class StatusBoardHandler(BaseModel):
    action: str
    status_board_object: AbstractStatusBoard

    def act(self, dry_run: bool, ocm: OCMBaseClient) -> None:
        logging.info(f"{self.action} - {self.status_board_object.summarize()}")
        if dry_run:
            return

        if not self.action:
            pass
        elif self.action == "delete":
            self.status_board_object.delete(ocm)
        elif self.action == "create":
            self.status_board_object.create(ocm)


class StatusBoardExporterIntegration(QontractReconcileIntegration):
    @property
    def name(self) -> str:
        return QONTRACT_INTEGRATION

    @staticmethod
    def get_product_apps(sb: StatusBoardV1) -> dict[str, set[str]]:
        global_selectors = (
            sb.global_app_selectors.exclude or [] if sb.global_app_selectors else []
        )
        return {
            p.product_environment.product.name: get_selected_app_names(
                global_selectors, p
            )
            for p in sb.products
        }

    @staticmethod
    def get_current_products_applications(ocm_api: OCMBaseClient) -> list[Product]:
        products_raw = get_managed_products(ocm_api)
        products = [Product(**p) for p in products_raw]

        for p in products:
            if not p.id:
                logging.error(f'Product "{p.name}" has no id')
                continue
            p.applications = [
                Application(**a) for a in get_product_applications(ocm_api, p.id)
            ]

        return products

    @staticmethod
    def get_diff(
        desired_product_apps: Mapping[str, set[str]],
        current_products_applications: Iterable[Product],
    ) -> list[StatusBoardHandler]:
        return_list: list[StatusBoardHandler] = []
        current_products = [p.name for p in current_products_applications]
        for product_name, app_names in desired_product_apps.items():
            if product_name not in current_products:
                return_list.append(
                    StatusBoardHandler(
                        action="create",
                        status_board_object=Product(
                            name=product_name, fullname=product_name, applications=[]
                        ),
                    )
                )
            else:
                # else, only create App if Product exists
                product_object = next(
                    p for p in current_products_applications if p.name == product_name
                )
                current_applications = [
                    a.name for a in product_object.applications or []
                ]
                for app_name in app_names:
                    if app_name not in current_applications:
                        return_list.append(
                            StatusBoardHandler(
                                action="create",
                                status_board_object=Application(
                                    name=app_name,
                                    fullname=f"{product_name}/{app_name}",
                                    product=product_object,
                                ),
                            )
                        )

        for product in current_products_applications:
            if product.name not in desired_product_apps:
                # if product was removed, we have to delete all apps
                for application in product.applications or []:
                    return_list.append(
                        StatusBoardHandler(
                            action="delete", status_board_object=application
                        )
                    )
                return_list.append(
                    StatusBoardHandler(action="delete", status_board_object=product)
                )
            else:
                for app in product.applications or []:
                    if app.name not in desired_product_apps.get(product.name, []):
                        return_list.append(
                            StatusBoardHandler(action="delete", status_board_object=app)
                        )

        return return_list

    def run(self, dry_run: bool) -> None:
        for sb in get_status_board():
            ocm_api = init_ocm_base_client(sb.ocm, self.secret_reader)
            desired_product_apps: dict[str, set[str]] = self.get_product_apps(sb)

            current_products_applications = self.get_current_products_applications(
                ocm_api
            )

            diff = self.get_diff(desired_product_apps, current_products_applications)

            for d in diff:
                d.act(dry_run, ocm_api)
