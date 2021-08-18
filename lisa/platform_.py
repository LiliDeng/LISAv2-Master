# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import partial
from typing import Any, Dict, List, Type, cast

from lisa import schema
from lisa.environment import Environment, EnvironmentStatus
from lisa.feature import Feature, Features
from lisa.node import RemoteNode
from lisa.notifier import MessageBase
from lisa.parameter_parser.runbook import RunbookBuilder
from lisa.util import (
    InitializableMixin,
    LisaException,
    constants,
    hookimpl,
    plugin_manager,
    subclasses,
)
from lisa.util.logger import Logger, get_logger
from lisa.util.perf_timer import create_timer

_get_init_logger = partial(get_logger, "init", "platform")

PlatformStatus = Enum(
    "TestRunStatus",
    [
        "INITIALIZED",
    ],
)


@dataclass
class PlatformMessage(MessageBase):
    type: str = "Platform"
    name: str = ""
    status: PlatformStatus = PlatformStatus.INITIALIZED


class WaitMoreResourceError(Exception):
    pass


class Platform(subclasses.BaseClassWithRunbookMixin, InitializableMixin):
    def __init__(self, runbook: schema.Platform) -> None:
        super().__init__(runbook)
        self._log = get_logger("", self.type_name())
        plugin_manager.register(self)

    @classmethod
    def type_schema(cls) -> Type[schema.TypedSchema]:
        return schema.Platform

    @classmethod
    def supported_features(cls) -> List[Type[Feature]]:
        """
        Indicates which feature classes should be used to instance a feature.

        For example, StartStop needs platform implemention, and LISA doesn't know which
        type uses to start/stop for Azure. So Azure platform needs to return a type
        like azure.StartStop. The azure.StartStop use same feature string as
        lisa.features.StartStop. When test cases reference a feature by string, it can
        be instanced to azure.StartStop.
        """
        raise NotImplementedError()

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        """
        platform specified initialization
        """
        pass

    def _prepare_environment(self, environment: Environment, log: Logger) -> bool:
        """
        What to be prepared for an environment
        1. check if platform can meet requirement of this environment
        2. if #1 is yes, specified platform context,
            so that the environment can be created in deploy phase
            with same spec as prepared.
        3. set cost for environment priority.

        return True, if environment can be deployed. False, if cannot.
        """
        raise NotImplementedError()

    def _deploy_environment(self, environment: Environment, log: Logger) -> None:
        raise NotImplementedError()

    def _delete_environment(self, environment: Environment, log: Logger) -> None:
        raise NotImplementedError()

    def _get_environment_information(self, environment: Environment) -> Dict[str, str]:
        return {}

    @hookimpl
    def get_environment_information(self, environment: Environment) -> Dict[str, str]:
        information: Dict[str, str] = {}

        assert environment.platform
        if environment.platform.type_name() != self.type_name():
            # prevent multiple platform can be activated in future, it should call for
            #  right platform only.
            return information

        information["platform"] = environment.platform.type_name()
        try:
            information.update(
                self._get_environment_information(environment=environment)
            )
        except Exception as identifier:
            self._log.exception(
                "failed to get environment information on platform", exc_info=identifier
            )

        return information

    def prepare_environment(self, environment: Environment) -> Environment:
        """
        return prioritized environments.
            user defined environment is higher priority than test cases,
            and then lower cost is prior to higher.
        """
        log = get_logger(f"prepare[{environment.name}]", parent=self._log)

        # check and fill connection information for RemoteNode. So that the
        # RemoteNodes can share the same connection information with created
        # nodes.
        platform_runbook = cast(schema.Platform, self.runbook)
        for node in environment.nodes.list():
            if isinstance(node, RemoteNode):
                node.set_connection_info_by_runbook(
                    default_username=platform_runbook.admin_username,
                    default_password=platform_runbook.admin_password,
                    default_private_key_file=platform_runbook.admin_private_key_file,
                )

        is_success = self._prepare_environment(environment, log)
        if is_success:
            environment.status = EnvironmentStatus.Prepared
        else:
            raise LisaException(
                f"no capability found for environment: {environment.runbook}"
            )

        return environment

    def deploy_environment(self, environment: Environment) -> None:
        log = get_logger(f"deploy[{environment.name}]", parent=self._log)
        log.info(f"deploying environment: {environment.name}")
        timer = create_timer()
        environment.platform = self
        self._deploy_environment(environment, log)
        environment.status = EnvironmentStatus.Deployed

        # initialize features
        # features may need platform, so create it in platform
        for node in environment.nodes.list():
            node.features = Features(node, self)
        log.info(f"deployed in {timer}")

    def delete_environment(self, environment: Environment) -> None:
        log = get_logger(f"del[{environment.name}]", parent=self._log)

        environment.close()
        if self.runbook.keep_environment == constants.ENVIRONMENT_KEEP_ALWAYS:
            log.info(
                f"skipped to delete environment {environment.name}, "
                f"as runbook set to keep environment."
            )
        else:
            log.debug("deleting")
            self._delete_environment(environment, log)
            log.debug("deleted")
        environment.status = EnvironmentStatus.Deleted


def load_platform(platforms_runbook: List[schema.Platform]) -> Platform:
    log = _get_init_logger()
    # we may extend it later to support multiple platforms
    platform_count = len(platforms_runbook)
    if platform_count != 1:
        raise LisaException("There must be 1 and only 1 platform")

    factory = subclasses.Factory[Platform](Platform)
    default_platform: Platform = factory.create_by_runbook(runbook=platforms_runbook[0])
    log.debug(f"activated platform '{default_platform.type_name()}'")

    return default_platform


def load_platform_from_builder(runbook_builder: RunbookBuilder) -> Platform:
    platform_runbook_data = runbook_builder.partial_resolve(constants.PLATFORM)
    platform_runbook = schema.load_by_type_many(schema.Platform, platform_runbook_data)
    platform = load_platform(platform_runbook)
    return platform
