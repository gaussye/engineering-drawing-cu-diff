"""Server-approved direct models, independent of CU deployment/cache provenance."""

from copy import deepcopy

from .model_compare import options


class AnalysisOptions:
    def __init__(self, config):
        self._config = deepcopy(config)
        configured = options(self._config)
        raw = self._config.get("model_comparison", {})
        entries = raw.get("models")
        if entries is None:
            deployment = configured["deployment"] or self._config.get(
                "model_deployments", {}).get(self._config.get("completion_model"))
            identifier = deployment or self._config.get("completion_model", "configured")
            entries = [{
                "id": identifier, "label": identifier, "deployment": deployment,
                "deployment_version": configured["deployment_version"],
            }]
            self._legacy = True
        else:
            self._legacy = False
            if not isinstance(entries, list) or not entries:
                raise ValueError("model_comparison.models must be a nonempty approved model list")
            for entry in entries:
                if (not isinstance(entry, dict)
                        or set(entry) != {"id", "label", "deployment", "deployment_version"}
                        or any(not isinstance(value, str) or not value.strip()
                               for value in entry.values())):
                    raise ValueError("Each approved model needs id, label, deployment and deployment_version")
                if (len(entry["id"]) > 80 or not entry["id"].isascii()
                        or any(not (c.isalnum() or c in "-_.") for c in entry["id"])):
                    raise ValueError("Approved model IDs must be short ASCII identifiers")
        self._models = {entry["id"]: deepcopy(entry) for entry in entries}
        if len(self._models) != len(entries):
            raise ValueError("Approved model IDs must be unique")
        default = raw.get("default_model")
        if default is None:
            default = next((entry["id"] for entry in entries
                            if entry["deployment"] == configured["deployment"]), None)
            if default is None and configured["deployment"] is None:
                default = entries[0]["id"]
        if not isinstance(default, str) or default not in self._models:
            raise ValueError("model_comparison.default_model must name an approved model")
        self.default = default

    def bootstrap(self):
        return {"use_cache": True, "model": self.default,
                "models": [{"id": entry["id"], "label": entry["label"]}
                           for entry in self._models.values()]}

    def snapshot(self, use_cache=True, model=None):
        if type(use_cache) is not bool:
            raise ValueError("use_cache必须为布尔值。")
        selected = self.default if model is None else model
        if not isinstance(selected, str) or selected not in self._models:
            raise ValueError("model必须为服务端已批准的模型标识。")
        entry = self._models[selected]
        config = deepcopy(self._config)
        if not self._legacy:
            config.setdefault("model_comparison", {}).update(
                deployment=entry["deployment"], deployment_version=entry["deployment_version"])
        return config, {"use_cache": use_cache, "model": selected, "model_label": entry["label"]}
