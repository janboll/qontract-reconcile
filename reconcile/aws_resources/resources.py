from abc import ABC
from typing import Optional, Iterable

from pydantic import BaseModel

from reconcile.gql_definitions.aws_resources.aws_resources import \
    NamespaceAWSResourceSQSV1


class ResourceProperty(BaseModel):
    value: Optional[str]
    forces_new: bool
    requires_downtime: bool

class AWSTags(BaseModel):
    key: str
    value: str

class AWSResource(BaseModel, ABC):
    arn: Optional[str]
    tags: Optional[list[AWSTags]]

    class Config:
        arbitrary_types_allowed = True

    def tags_as_dict(self) -> dict[str, str]:
        return {tag.key: tag.value for tag in self.tags}

    def create(self, aws_client) -> str:
        raise NotImplementedError()

    def delete(self, aws_client) -> None:
        raise NotImplementedError()

    def update(self, aws_client) -> None:
        raise NotImplementedError()



class IAMUser(AWSResource):
    name = ResourceProperty(forces_new=True, requires_downtime=False)

    def create(self, aws_client) -> str:
        pass

    def delete(self, aws_client) -> None:
        pass

    def update(self, aws_client) -> None:
        pass


class IAMPolicy(AWSResource):
    name = ResourceProperty(forces_new=True, requires_downtime=False)
    policy: str
    class Config:
        arbitrary_types_allowed = True

    def create(self, aws_client) -> str:
        pass

    def delete(self, aws_client) -> None:
        pass

    def update(self, aws_client) -> None:
        pass

class IAMPolicyAttachement(AWSResource):
    user: str
    policy: str

    def create(self, aws_client) -> str:
        pass

    def delete(self, aws_client) -> None:
        pass

    def update(self, aws_client) -> None:
        pass

class SQSQueue(AWSResource):
    # not a resource property, queues are special in this regard
    queue_url: Optional[str]

    name = ResourceProperty(forces_new=False, requires_downtime=True)

    delay_seconds = ResourceProperty(forces_new=False, requires_downtime=False)
    maximum_message_size = ResourceProperty(forces_new=False, requires_downtime=False)

    class Config:
        arbitrary_types_allowed = True

    def attributes_as_dict(self)-> dict[str, any]:
        return {
            "DelaySeconds": self.delay_seconds.value,
            "MaximumMessageSize": self.maximum_message_size.value,
        }

    def create(self, aws_client) -> str:
        tags=self.tags_as_dict()
        resp = aws_client.create_queue(self.name.value, Attributes=self.attributes_as_dict(), Tags=tags)
        return resp["QueueUrl"]

    def delete(self, aws_client) -> None:
        if not self.queue_url:
            raise Exception("Queue not created yet")
        aws_client.delete_queue(self.queue_url)

    def update(self, aws_client) -> None:
        aws_client.set_queue_attributes(self.name.value, Attributes=self.attributes_as_dict())
        aws_client.tag_queue(self.name.value, Tags=self.tags_as_dict())



class ResourceState(BaseModel):
    current: Optional[AWSResource]
    desired: AWSResource

    class Config:
        arbitrary_types_allowed = True

class AWSSQSHandler(BaseModel):
    queues: list[ResourceState]
    # user = ResourceState
    # iam_policy = ResourceState
    # iam_policy_attachement = ResourceState

    class Config:
        arbitrary_types_allowed = True


    def has_diff(self) -> bool:
        diff = False
        for queue in self.queues:
            if queue.current != queue.desired:
                diff = True
        # if self.user.current != self.user.desired:
        #     diff = True
        # if self.iam_policy.current != self.iam_policy.desired:
        #     diff = True
        # if self.iam_policy_attachement.current != self.iam_policy_attachement.desired:
        #     diff = True
        return diff

    def create(self, aws_client) -> None:
        for queue in self.queues:
            if not queue.current:
                queue.desired.create(aws_client)
        # if not self.user.current:
        #     self.user.desired.create()
        # if not self.iam_policy.current:
        #     self.iam_policy.desired.create()
        # if not self.iam_policy_attachement.current:
        #     self.iam_policy_attachement.desired.create()


def sqs_from_query(x: NamespaceAWSResourceSQSV1) -> AWSSQSHandler:
    queues: list[ResourceState] = []
    for s in x.specs:
        for desired in s.queues:
            q = SQSQueue()
            q.name.value = desired.value
            queues.append(ResourceState(desired=q))

    return AWSSQSHandler(queues= queues)
