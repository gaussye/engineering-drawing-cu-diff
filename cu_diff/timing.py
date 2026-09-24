"""Sequential wall-clock stages; service/cache metadata never contributes time."""

import math
import time


STAGES = {
    "analyzer_preparation": "分析器准备",
    "cu_full_old": "原图CU全页提取",
    "cu_full_new": "调整图CU全页提取",
    "model_coarse": "全页模型语义配对（含图像准备与来源核验）",
    "cu_crop_preparation": "局部高清裁剪准备",
    "cu_crop_old": "原图局部CU复读",
    "cu_crop_new": "调整图局部CU复读",
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
    """Access under the owning Store lock. Stages are disjoint, not nested."""

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._queued = self._clock()
        self._started = None
        self._finished = None
        self._stages = []

    def start(self):
        if self._started is None:
            self._started = self._clock()

    def stage(self, identifier):
        if self._finished is not None:
            raise RuntimeError("Cannot add a stage to a finished timing journal")
        now = self._clock()
        self._close_stage(now, "completed")
        occurrence = 1 + sum(stage["kind"] == identifier for stage in self._stages)
        unique_id = identifier if occurrence == 1 else f"{identifier}_{occurrence}"
        label = STAGES[identifier] + (f" · 第{occurrence}段" if occurrence > 1 else "")
        self._stages.append({"id": unique_id, "kind": identifier, "label": label,
                             "status": "running", "start": now, "end": None})

    @property
    def active_stage(self):
        return self._stages[-1]["kind"] if self._stages and self._finished is None else None

    def _close_stage(self, now, status):
        if self._stages and self._stages[-1]["end"] is None:
            self._stages[-1].update(end=now, status=status)

    def finish(self, *, failed=False):
        if self._finished is None:
            self._finished = self._clock()
            self._close_stage(self._finished, "failed" if failed else "completed")

    def snapshot(self):
        now = self._finished if self._finished is not None else self._clock()
        return {
            "total_seconds": _seconds(self._queued, now),
            "queue_seconds": _seconds(self._queued, self._started if self._started is not None else now),
            "stages": [{"id": stage["id"], "label": stage["label"], "status": stage["status"],
                        "elapsed_seconds": _seconds(
                            stage["start"], stage["end"] if stage["end"] is not None else now)}
                       for stage in self._stages],
        }
