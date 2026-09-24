"""Disjoint wall-clock stages with optional concurrent child measurements."""

import math
import time


STAGES = {
    "analyzer_preparation": "分析器准备",
    "cu_full_old": "原图CU全页提取",
    "cu_full_new": "调整图CU全页提取",
    "cu_full_pair": "双图CU全页提取",
    "model_coarse": "全页模型语义配对（含图像准备与来源核验）",
    "cu_crop_preparation": "局部高清裁剪准备",
    "cu_crop_old": "原图局部CU复读",
    "cu_crop_new": "调整图局部CU复读",
    "cu_crop_pair": "双图局部CU复读",
    "model_fine": "模型细粒度文字核对（含来源核验）",
    "model_visual": "模型图形复核（含本地残差核验）",
    "model_visual_presence": "单侧图形定位与对侧搜索",
    "semantic_text_pairing": "文本语义配对",
    "local_table_comparison": "本地表格对比",
    "local_graphics_comparison": "本地图形对比",
    "result_preparation": "结果整理",
}


def _seconds(start, end):
    elapsed = end - start
    return max(0.0, elapsed) if math.isfinite(elapsed) else 0.0


class JobTiming:
    """Access under the owning Store lock; only top-level stages are additive."""

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._queued = self._clock()
        self._started = None
        self._finished = None
        self._stages = []

    def start(self):
        if self._started is None:
            self._started = self._clock()

    def stage(self, identifier, *, execution=None):
        if self._finished is not None:
            raise RuntimeError("Cannot add a stage to a finished timing journal")
        if execution not in (None, "parallel", "sequential"):
            raise ValueError("Invalid stage execution mode")
        now = self._clock()
        self._close_stage(now, "completed")
        occurrence = 1 + sum(stage["kind"] == identifier for stage in self._stages)
        unique_id = identifier if occurrence == 1 else f"{identifier}_{occurrence}"
        label = STAGES[identifier] + (f" · 第{occurrence}段" if occurrence > 1 else "")
        self._stages.append({"id": unique_id, "kind": identifier, "label": label,
                             "status": "running", "start": now, "end": None})
        if execution is not None:
            self._stages[-1].update(execution=execution, steps=[])

    def step(self, identifier, status):
        if self._finished is not None or not self._stages:
            raise RuntimeError("No active stage for a child measurement")
        group = self._stages[-1]
        if "steps" not in group or group["end"] is not None:
            raise RuntimeError("Child measurements require an active group")
        now = self._clock()
        if status == "running":
            if any(step["id"] == identifier for step in group["steps"]):
                raise ValueError("Duplicate child measurement")
            group["steps"].append({"id": identifier, "label": STAGES[identifier],
                                   "status": status, "start": now, "end": None})
        elif status in ("completed", "failed"):
            step = next((s for s in group["steps"] if s["id"] == identifier), None)
            if step is None or step["end"] is not None:
                raise ValueError("Child measurement is not running")
            step.update(status=status, end=now)
        else:
            raise ValueError("Invalid child measurement status")

    @property
    def active_stage(self):
        return self._stages[-1]["kind"] if self._stages and self._finished is None else None

    def _close_stage(self, now, status):
        if self._stages and self._stages[-1]["end"] is None:
            stage = self._stages[-1]
            for step in stage.get("steps", []):
                if step["end"] is None:
                    step.update(end=now, status=status)
            stage.update(end=now, status=status)

    def finish(self, *, failed=False):
        if self._finished is None:
            self._finished = self._clock()
            self._close_stage(self._finished, "failed" if failed else "completed")

    def snapshot(self):
        now = self._finished if self._finished is not None else self._clock()

        def public(stage):
            result = {"id": stage["id"], "label": stage["label"], "status": stage["status"],
                      "elapsed_seconds": _seconds(
                          stage["start"], stage["end"] if stage["end"] is not None else now)}
            if "steps" in stage:
                result.update(execution=stage["execution"],
                              steps=[public(step) for step in stage["steps"]])
            return result

        return {
            "total_seconds": _seconds(self._queued, now),
            "queue_seconds": _seconds(self._queued, self._started if self._started is not None else now),
            "stages": [public(stage) for stage in self._stages],
        }
