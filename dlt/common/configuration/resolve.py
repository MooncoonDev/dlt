import ast
import inspect
from collections.abc import Mapping as C_Mapping
from typing import Any, Dict, ContextManager, List, Optional, Sequence, Tuple, Type, TypeVar, get_origin

from dlt.common import json, logger
from dlt.common.typing import AnyType, StrAny, TSecretValue, is_optional_type, extract_inner_type
from dlt.common.schema.utils import coerce_type, py_type_to_sc_type

from dlt.common.configuration.specs.base_configuration import BaseConfiguration, CredentialsConfiguration, ContainerInjectableContext, get_config_if_union
from dlt.common.configuration.specs.config_namespace_context import ConfigNamespacesContext
from dlt.common.configuration.container import Container
from dlt.common.configuration.specs.config_providers_context import ConfigProvidersContext
from dlt.common.configuration.exceptions import (LookupTrace, ConfigFieldMissingException, ConfigurationWrongTypeException, ConfigValueCannotBeCoercedException, ValueNotSecretException, InvalidNativeValue)

TConfiguration = TypeVar("TConfiguration", bound=BaseConfiguration)


def resolve_configuration(config: TConfiguration, *, namespaces: Tuple[str, ...] = (), explicit_value: Any = None, accept_partial: bool = False) -> TConfiguration:
    if not isinstance(config, BaseConfiguration):
        raise ConfigurationWrongTypeException(type(config))

    # try to get the native representation of the top level configuration using the config namespace as a key
    # allows, for example, to store connection string or service.json in their native form in single env variable or under single vault key
    if config.__namespace__ and explicit_value is None:
        explicit_value, _ = _resolve_config_field(config.__namespace__, AnyType, None, explicit_value, config, None, namespaces, (), accept_partial)

    return _resolve_configuration(config, namespaces, (), explicit_value, accept_partial)


def deserialize_value(key: str, value: Any, hint: Type[Any]) -> Any:
    try:
        if hint != Any:
            hint_dt = py_type_to_sc_type(hint)
            value_dt = py_type_to_sc_type(type(value))

            # eval only if value is string and hint is "complex"
            if value_dt == "text" and hint_dt == "complex":
                if hint is tuple:
                    # use literal eval for tuples
                    value = ast.literal_eval(value)
                else:
                    # use json for sequences and mappings
                    value = json.loads(value)
                # exact types must match
                if not isinstance(value, hint):
                    raise ValueError(value)
            else:
                # for types that are not complex, reuse schema coercion rules
                if value_dt != hint_dt:
                    value = coerce_type(hint_dt, value_dt, value)
        return value
    except ConfigValueCannotBeCoercedException:
        raise
    except Exception as exc:
        raise ConfigValueCannotBeCoercedException(key, value, hint) from exc


def serialize_value(value: Any) -> Any:
    if value is None:
        raise ValueError(value)
    # return literal for tuples
    if isinstance(value, tuple):
        return str(value)
    # coerce type to text which will use json for mapping and sequences
    value_dt = py_type_to_sc_type(type(value))
    return coerce_type("text", value_dt, value)


def inject_namespace(namespace_context: ConfigNamespacesContext, merge_existing: bool = True) -> ContextManager[ConfigNamespacesContext]:
    """Adds `namespace` context to container, making it injectable. Optionally merges the context already in the container with the one provided

    Args:
        namespace_context (ConfigNamespacesContext): Instance providing a pipeline name and namespace context
        merge_existing (bool, optional): Gets `pipeline_name` and `namespaces` from existing context if they are not provided in `namespace` argument. Defaults to True.

    Yields:
        Iterator[ConfigNamespacesContext]: Context manager with current namespace context
    """
    container = Container()
    existing_context = container[ConfigNamespacesContext]

    if merge_existing:
        namespace_context.pipeline_name = namespace_context.pipeline_name or existing_context.pipeline_name
        namespace_context.namespaces = namespace_context.namespaces or existing_context.namespaces

    return container.injectable_context(namespace_context)


def _resolve_configuration(
        config: TConfiguration,
        explicit_namespaces: Tuple[str, ...],
        embedded_namespaces: Tuple[str, ...],
        explicit_value: Any,
        accept_partial: bool
    ) -> TConfiguration:

    # do not resolve twice
    if config.is_resolved():
        return config

    config.__exception__ = None
    try:
        try:
            # use initial value to resolve the whole configuration. if explicit value is a mapping it will be applied field by field later
            if explicit_value and not isinstance(explicit_value, C_Mapping):
                try:
                    config.parse_native_representation(explicit_value)
                except ValueError as v_err:
                    # provide generic exception
                    raise InvalidNativeValue(type(config), type(explicit_value), embedded_namespaces, v_err)
                except NotImplementedError:
                    pass
                # explicit value was consumed
                explicit_value = None

            # if native representation didn't fully resolve the config, we try to resolve field by field
            if not config.is_resolved():
                _resolve_config_fields(config, explicit_value, explicit_namespaces, embedded_namespaces, accept_partial)

            _call_method_in_mro(config, "on_resolved")
            # full configuration was resolved
            config.__is_resolved__ = True
        except ConfigFieldMissingException as cm_ex:
            # store the ConfigEntryMissingException to have full info on traces of missing fields
            config.__exception__ = cm_ex
            _call_method_in_mro(config, "on_partial")
            # if resolved then do not raise
            if config.is_resolved():
                _call_method_in_mro(config, "on_resolved")
            else:
                if not accept_partial:
                    raise
    except Exception as ex:
        # store the exception that happened in the resolution process
        config.__exception__ = ex
        raise

    return config


def _resolve_config_fields(
        config: BaseConfiguration,
        explicit_values: StrAny,
        explicit_namespaces: Tuple[str, ...],
        embedded_namespaces: Tuple[str, ...],
        accept_partial: bool
    ) -> None:

    fields = config.get_resolvable_fields()
    unresolved_fields: Dict[str, Sequence[LookupTrace]] = {}

    for key, hint in fields.items():
        # get default and explicit values
        if explicit_values:
            explicit_value = explicit_values.get(key)
        else:
            explicit_value = None
        default_value = getattr(config, key, None)
        current_value, traces = _resolve_config_field(key, hint, default_value, explicit_value, config, config.__namespace__, explicit_namespaces, embedded_namespaces, accept_partial)

        # check if hint optional
        is_optional = is_optional_type(hint)
        # collect unresolved fields
        if not is_optional and current_value is None:
            unresolved_fields[key] = traces
        # set resolved value in config
        setattr(config, key, current_value)

    if unresolved_fields:
        raise ConfigFieldMissingException(type(config).__name__, unresolved_fields)


def _resolve_config_field(
        key: str,
        hint: Type[Any],
        default_value: Any,
        explicit_value: Any,
        config: BaseConfiguration,
        config_namespace: str,
        explicit_namespaces: Tuple[str, ...],
        embedded_namespaces: Tuple[str, ...],
        accept_partial: bool
    ) -> Tuple[Any, List[LookupTrace]]:
    # extract hint from Optional / Literal / NewType hints
    inner_hint = extract_inner_type(hint)
    # get base configuration from union type
    inner_hint = get_config_if_union(inner_hint) or inner_hint
    # extract origin from generic types (ie List[str] -> List)
    inner_hint = get_origin(inner_hint) or inner_hint

    if explicit_value:
        value = explicit_value
        traces: List[LookupTrace] = []
    else:
        # resolve key value via active providers passing the original hint ie. to preserve TSecretValue
        value, traces = _resolve_single_value(key, hint, inner_hint, config_namespace, explicit_namespaces, embedded_namespaces)
        _log_traces(config, key, hint, value, traces)

    # contexts must be resolved as a whole
    if inspect.isclass(inner_hint) and issubclass(inner_hint, ContainerInjectableContext):
        pass
    # if inner_hint is BaseConfiguration then resolve it recursively
    elif inspect.isclass(inner_hint) and issubclass(inner_hint, BaseConfiguration):
        if isinstance(value, BaseConfiguration):
            # if resolved value is instance of configuration (typically returned by context provider)
            embedded_config = value
            value = None
        elif isinstance(default_value, BaseConfiguration):
            # if default value was instance of configuration, use it
            embedded_config = default_value
            default_value = None
        else:
            embedded_config = inner_hint()

        if embedded_config.is_resolved():
            # injected context will be resolved
            value = embedded_config
        else:
            # only config with namespaces may look for initial values
            if embedded_config.__namespace__ and value is None:
                # config namespace becomes the key if the key does not start with, otherwise it keeps its original value
                initial_key, initial_embedded = _apply_embedded_namespaces_to_config_namespace(embedded_config.__namespace__, embedded_namespaces + (key,))
                # it must be a secret value is config is credentials
                initial_hint = TSecretValue if isinstance(embedded_config, CredentialsConfiguration) else AnyType
                value, initial_traces = _resolve_single_value(initial_key, initial_hint, AnyType, None, explicit_namespaces, initial_embedded)
                traces.extend(initial_traces)

            # check if hint optional
            is_optional = is_optional_type(hint)
            # accept partial becomes True if type if optional so we do not fail on optional configs that do not resolve fully
            accept_partial = accept_partial or is_optional
            # create new instance and pass value from the provider as initial, add key to namespaces
            value = _resolve_configuration(embedded_config, explicit_namespaces, embedded_namespaces + (key,), value or default_value, accept_partial)
    else:
        # if value is resolved, then deserialize and coerce it
        if value is not None:
            # do not deserialize explicit values
            if value is not explicit_value:
                value = deserialize_value(key, value, inner_hint)

    return value or default_value, traces


def _log_traces(config: BaseConfiguration, key: str, hint: Type[Any], value: Any, traces: Sequence[LookupTrace]) -> None:
    if logger.is_logging() and logger.log_level() == "DEBUG":
        logger.debug(f"Field {key} with type {hint} in {type(config).__name__} {'NOT RESOLVED' if value is None else 'RESOLVED'}")
        # print(f"Field {key} with type {hint} in {type(config).__name__} {'NOT RESOLVED' if value is None else 'RESOLVED'}")
        for tr in traces:
            # print(str(tr))
            logger.debug(str(tr))


def _call_method_in_mro(config: BaseConfiguration, method_name: str) -> None:
    # python multi-inheritance is cooperative and this would require that all configurations cooperatively
    # call each other class_method_name. this is not at all possible as we do not know which configs in the end will
    # be mixed together.

    # get base classes in order of derivation
    mro = type.mro(type(config))
    for c in mro:
        # check if this class implements on_resolved (skip pure inheritance to not do double work)
        if method_name in c.__dict__ and callable(getattr(c, method_name)):
            # pass right class instance
            c.__dict__[method_name](config)


def _resolve_single_value(
        key: str,
        hint: Type[Any],
        inner_hint: Type[Any],
        config_namespace: str,
        explicit_namespaces: Tuple[str, ...],
        embedded_namespaces: Tuple[str, ...]
    ) -> Tuple[Optional[Any], List[LookupTrace]]:

    traces: List[LookupTrace] = []
    value = None

    container = Container()
    # get providers from container
    providers_context = container[ConfigProvidersContext]
    # we may be resolving context
    if inspect.isclass(inner_hint) and issubclass(inner_hint, ContainerInjectableContext):
        # resolve context with context provider and do not look further
        value, _ = providers_context.context_provider.get_value(key, inner_hint)
        return value, traces
    if inspect.isclass(inner_hint) and issubclass(inner_hint, BaseConfiguration):
        # cannot resolve configurations directly
        return value, traces

    # resolve a field of the config
    config_namespace, embedded_namespaces = _apply_embedded_namespaces_to_config_namespace(config_namespace, embedded_namespaces)
    providers = providers_context.providers
    # get additional namespaces to look in from container
    namespaces_context = container[ConfigNamespacesContext]


    # start looking from the top provider with most specific set of namespaces first

    def look_namespaces(pipeline_name: str = None) -> Any:
        for provider in providers:
            if provider.supports_namespaces:
                # if explicit namespaces are provided, ignore the injected context
                if explicit_namespaces:
                    ns = list(explicit_namespaces)
                else:
                    ns = list(namespaces_context.namespaces)
                # always extend with embedded namespaces
                ns.extend(embedded_namespaces)
            else:
                # if provider does not support namespaces and pipeline name is set then ignore it
                if pipeline_name:
                    continue
                else:
                    # pass empty namespaces
                    ns = []

            value = None
            while True:
                if (pipeline_name or config_namespace) and provider.supports_namespaces:
                    full_ns = ns.copy()
                    # pipeline, when provided, is the most outer and always present
                    if pipeline_name:
                        full_ns.insert(0, pipeline_name)
                    # config namespace, is always present and innermost
                    if config_namespace:
                        full_ns.append(config_namespace)
                else:
                    full_ns = ns
                value, ns_key = provider.get_value(key, hint, *full_ns)
                # if secret is obtained from non secret provider, we must fail
                cant_hold_it: bool = not provider.supports_secrets and _is_secret_hint(hint)
                if value is not None and cant_hold_it:
                    raise ValueNotSecretException(provider.name, ns_key)

                # create trace, ignore providers that cant_hold_it
                if not cant_hold_it:
                    traces.append(LookupTrace(provider.name, full_ns, ns_key, value))

                if value is not None:
                    # value found, ignore other providers
                    return value
                if len(ns) == 0:
                    # check next provider
                    break
                # pop optional namespaces for less precise lookup
                ns.pop()

    # first try with pipeline name as namespace, if present
    if namespaces_context.pipeline_name:
        value = look_namespaces(namespaces_context.pipeline_name)
    # then without it
    if value is None:
        value = look_namespaces()

    return value, traces


def _apply_embedded_namespaces_to_config_namespace(config_namespace: str, embedded_namespaces: Tuple[str, ...]) -> Tuple[str, Tuple[str, ...]]:
    # for the configurations that have __namespace__ (config_namespace) defined and are embedded in other configurations,
    # the innermost embedded namespace replaces config_namespace
    if embedded_namespaces:
        # do not add key to embedded namespaces if it starts with _, those namespaces must be ignored
        if not embedded_namespaces[-1].startswith("_"):
            config_namespace = embedded_namespaces[-1]
        embedded_namespaces = embedded_namespaces[:-1]

    # remove all embedded ns starting with _
    return config_namespace, tuple(ns for ns in embedded_namespaces if not ns.startswith("_"))


def _is_secret_hint(hint: Type[Any]) -> bool:
    return hint is TSecretValue or (inspect.isclass(hint) and issubclass(hint, CredentialsConfiguration))