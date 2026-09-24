"""Bounded CU-only concurrency; model transport and billing provenance stay unchanged."""

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from copy import deepcopy
from inspect import getattr_static


def cu_workers(config):
    if not isinstance(config, dict):
        raise ValueError("configuration must be an object")
    performance = config.get("performance", {})
    if not isinstance(performance, dict):
        raise ValueError("performance must be an object")
    workers = performance.get("cu_workers", 2)
    if type(workers) is not int or workers not in (1, 2):
        raise ValueError("performance.cu_workers must be 1 or 2")
    return workers


def _client_workers(client):
    try:
        getattr_static(client, "config")
    except AttributeError:
        config = {}
    else:
        config = client.config
    return cu_workers(config)


def _scoped_context_enabled(client):
    return (getattr(client, "supports_parallel_cu", False) is True
            and callable(getattr(client, "usage_scope", None)))


def parallel_cu_enabled(client):
    """Return the validated execution mode used by run_cu_pair."""
    return _client_workers(client) == 2 and _scoped_context_enabled(client)


@contextmanager
def _serial_scope(client, context):
    absent = object()
    previous = getattr(client, "usage_context", absent)
    client.usage_context = deepcopy(context)
    try:
        yield
    finally:
        if previous is absent:
            del client.usage_context
        else:
            client.usage_context = previous


def run_cu_pair(client, tasks, *, contexts, progress=None):
    """Run ordered tasks with at most two workers and return in input key order.

    Progress is coordinator-only. All submitted tasks finish before an error is
    raised; failed tasks are not retried, and no partial result is returned.
    A progress failure stops further submissions, but submitted tasks still
    report completion/failure before the first progress exception is re-raised.
    Unknown/duck-typed clients run serially unless they explicitly advertise
    supports_parallel_cu=True and implement usage_scope(context).
    """
    if (not isinstance(tasks, Mapping) or not isinstance(contexts, Mapping)
            or set(tasks) != set(contexts)
            or any(not callable(task) for task in tasks.values())
            or any(not isinstance(context, Mapping) for context in contexts.values())):
        raise ValueError("CU tasks require matching context mappings and callable tasks")
    workers = 2 if parallel_cu_enabled(client) else 1
    tasks = dict(tasks)
    contexts = deepcopy(dict(contexts))
    scope = getattr(client, "usage_scope", None)
    safe = _scoped_context_enabled(client)
    observer = progress or (lambda side, status: None)
    results, failures = {}, {}
    progress_failures = []

    def notify(side, status):
        try:
            observer(side, status)
        except BaseException as error:
            progress_failures.append(error)
            return False
        return True

    def invoke(side):
        with scope(contexts[side]) if safe else _serial_scope(client, contexts[side]):
            return tasks[side]()

    def receive(side, operation):
        try:
            results[side] = operation()
        except BaseException as error:
            failures[side] = error
            notify(side, "failed")
        else:
            notify(side, "completed")

    if workers == 1 or len(tasks) <= 1:
        for side in tasks:
            if not notify(side, "running"):
                break
            receive(side, lambda side=side: invoke(side))
            if progress_failures:
                break
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cu-pair") as executor:
            futures = {}
            for side in tasks:
                if not notify(side, "running"):
                    break
                futures[executor.submit(invoke, side)] = side
            for future in as_completed(futures):
                receive(futures[future], future.result)
    if progress_failures:
        raise progress_failures[0]
    for side in tasks:
        if side in failures:
            raise failures[side]
    return {side: results[side] for side in tasks}
