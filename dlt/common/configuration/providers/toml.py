import os
import tomlkit
from typing import Any, Optional, Tuple, Type

from dlt.common.typing import StrAny

from .provider import ConfigProvider, ConfigProviderException


class TomlProvider(ConfigProvider):

    def __init__(self, file_name: str, project_dir: str = None) -> None:
        self._file_name = file_name
        self._toml_path = os.path.join(project_dir or os.path.abspath(os.path.join(".", ".dlt")), file_name)
        try:
            self._toml = self._read_toml(self._toml_path)
        except Exception as ex:
            raise TomlProviderReadException(self.name, file_name, self._toml_path, str(ex))

    @staticmethod
    def get_key_name(key: str, *namespaces: str) -> str:
        # env key is always upper case
        if namespaces:
            namespaces = filter(lambda x: bool(x), namespaces)  # type: ignore
            env_key = ".".join((*namespaces, key))
        else:
            env_key = key
        return env_key

    def get_value(self, key: str, hint: Type[Any], *namespaces: str) -> Tuple[Optional[Any], str]:
        full_path = namespaces + (key,)
        full_key = self.get_key_name(key, *namespaces)
        node = self._toml
        try:
            for k in  full_path:
                if not isinstance(node, dict):
                    raise KeyError(k)
                node = node[k]
            return node, full_key
        except KeyError:
            return None, full_key

    @property
    def supports_namespaces(self) -> bool:
        return True

    @staticmethod
    def _read_toml(toml_path: str) -> StrAny:
        if os.path.isfile(toml_path):
            with open(toml_path, "r", encoding="utf-8") as f:
                # use whitespace preserving parser
                return tomlkit.load(f)
        else:
            return {}


class ConfigTomlProvider(TomlProvider):

    def __init__(self, project_dir: str = None) -> None:
        super().__init__("config.toml", project_dir)

    @property
    def name(self) -> str:
        return "Pipeline config.toml"

    @property
    def supports_secrets(self) -> bool:
        return False



class SecretsTomlProvider(TomlProvider):

    def __init__(self, project_dir: str = None) -> None:
        super().__init__("secrets.toml", project_dir)

    @property
    def name(self) -> str:
        return "Pipeline secrets.toml"

    @property
    def supports_secrets(self) -> bool:
        return True


class TomlProviderReadException(ConfigProviderException):
    def __init__(self, provider_name: str, file_name: str, full_path: str, toml_exception: str) -> None:
        self.file_name = file_name
        self.full_path = full_path
        msg = f"A problem encountered when loading {provider_name} from {full_path}:\n"
        msg += toml_exception
        super().__init__(provider_name, msg)
