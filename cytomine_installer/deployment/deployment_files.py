import enum
import json
import os
import yaml
from collections import defaultdict

from .errors import (
    MissingConfigFileError,
    NoDockerComposeYamlFileError,
    UnknownConfigSection,
    UnknownServiceError,
)
from .enums import ConfigSectionEnum
from .env_store import DictExportable, EnvStore, MergeEnvStorePolicy

DOCKER_COMPOSE_FILENAME = "docker-compose.yml"
DOCKER_COMPOSE_OVERRIDE_FILENAME = "docker-compose.override.yml"


class UnknownServerError(ValueError):
    def __init__(self, server, *args: object) -> None:
        super().__init__(f"unknown server '{server}'", *args)


class ConfigFile(DictExportable):
    """parses a yml config file"""

    def __init__(
        self, path="./", filename="cytomine.yml", file_must_exists=False
    ) -> None:
        self._filename = filename
        self._path = path

        file_exists = os.path.isfile(self.filepath)
        if not file_exists and file_must_exists:
            raise MissingConfigFileError(path, filename)

        # empty configuration
        self._global_envs = EnvStore()
        self._servers_env_stores = defaultdict(EnvStore)

        if not file_exists:
            return

        with open(self.filepath, "r", encoding="utf8") as file:
            raw_config = yaml.load(file, Loader=yaml.Loader)

        # both top-level sections are optional
        for section in raw_config.keys():
            try:
                ConfigSectionEnum(section)
            except ValueError:
                raise UnknownConfigSection(section)

        for ns, entries in raw_config.get(ConfigSectionEnum.GLOBAL.value, {}).items():
            self._global_envs.add_namespace(ns, entries)

        for server, envs in raw_config.get(
            ConfigSectionEnum.SERVICES.value, {}
        ).items():
            for ns, entries in envs.items():
                self._servers_env_stores[server].add_namespace(
                    ns, entries, store=self._global_envs
                )

    @property
    def filename(self):
        return self._filename

    @property
    def path(self):
        return self._path

    @property
    def filepath(self):
        return os.path.join(self.path, self.filename)

    @property
    def global_envs(self):
        return self._global_envs

    @property
    def servers(self):
        return list(self._servers_env_stores.keys())

    def services(self, server: str):
        """Returns the list of services for a given server"""
        if server not in self._servers_env_stores:
            raise UnknownServerError(server)
        return list(self._servers_env_stores[server].keys())

    def server_store(self, server: str):
        """Returns the env store for a given server"""
        if server not in self._servers_env_stores:
            raise UnknownServerError(server)
        return self._servers_env_stores.get(server, None)

    def export_dict(self):
        target_dict = dict()
        target_dict["global"] = self._global_envs.export_dict()
        target_dict["services"] = dict()
        for server, env_store in self._servers_env_stores.items():
            target_dict["services"][server] = env_store.export_dict()
        # https://stackoverflow.com/a/32303615
        # convert to plain dict
        return json.loads(json.dumps(target_dict))

    @staticmethod
    def merge(
        config_file1,
        config_file2,
        merge_policy: MergeEnvStorePolicy = MergeEnvStorePolicy.PRESERVE,
    ):
        new_config_file = ConfigFile()
        new_config_file._global_envs = EnvStore.merge(
            config_file1._global_envs,
            config_file2._global_envs,
            merge_policy=merge_policy,
        )
        # merge existing servers
        for server_name1, env_store1 in config_file1._servers_env_stores.items():
            env_store2 = config_file2._servers_env_stores.get(server_name1, EnvStore())
            new_config_file._servers_env_stores[server_name1] = EnvStore.merge(
                env_store1, env_store2, merge_policy=merge_policy
            )
        # add new servers from config file 2
        new_servers = set(config_file2._servers_env_stores.keys()).difference(
            config_file1._servers_env_stores.keys()
        )
        for server_name2 in new_servers:
            env_store2 = config_file2._servers_env_stores[server_name2]
            env_store2 = EnvStore.merge(env_store2, EnvStore())  # deep copy
            new_config_file._servers_env_stores[server_name2] = env_store2
        return new_config_file


class DockerComposeFile:
    """light parsing of docker compose files"""

    def __init__(self, path, filename=DOCKER_COMPOSE_FILENAME) -> None:
        self._path = path
        self._filename = filename

        if not os.path.isfile(self.filepath):
            raise NoDockerComposeYamlFileError(self._path)

        with open(self.filepath, "r", encoding="utf8") as file:
            self._content = yaml.load(file, Loader=yaml.Loader)

    @property
    def filepath(self):
        return os.path.join(self._path, self._filename)

    @property
    def filename(self):
        return self._filename

    @property
    def services(self):
        return list(self._content.get("services", {}).keys())

    @property
    def version(self):
        return self._content.get("version")


class EditableDockerCompose:
    """A class for creating and changing a docker compose (intentionally very limited scope).
    Supports edition of:
    - service 'env_file'
    - service 'volumes'
    """

    def __init__(self, version="3.9") -> None:
        self._compose = dict()
        self._compose["services"] = {}
        self._compose["version"] = version

    def _get_service_dict(self, service):
        if service not in self._compose["services"]:
            self._compose["services"][service] = {}
        return self._compose["services"][service]

    def set_service_env_file(self, service, filepath):
        self._get_service_dict(service)["env_file"] = filepath

    def get_service_volumes(self, service):
        if service not in self._compose["services"]:
            raise UnknownServiceError(service)
        return self._compose["services"][service]["volumes"]

    def add_service_volume(self, service, mapping):
        service_dict = self._get_service_dict(service)
        if "volumes" not in service_dict:
            self._compose["services"][service]["volumes"] = list()
        self._compose["services"][service]["volumes"].append(mapping)

    def clear_service_volumes(self, service):
        if (
            service in self._compose["services"]
            and "volumes" in self._compose["services"][service]
        ):
            del self._compose["services"][service]["volumes"]

    def write_to(self, path, filename="docker-compose.yml"):
        filepath = os.path.join(path, filename)
        with open(filepath, "w", encoding="utf8") as file:
            yaml.dump(self._compose, file)
