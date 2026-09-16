"""Deterministic roadmap layout shared by SVG and PDF renderers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping

from .models import RoadmapPresentation, RoadmapNodePresentation


@dataclass(frozen=True)
class PositionedNode:
    node: RoadmapNodePresentation
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class RoadmapLayout:
    width: float
    height: float
    header_height: float
    lane_header_width: float
    lane_height: float
    node_width: float
    node_height: float
    positioned: tuple[PositionedNode, ...]
    node_index: Mapping[str, PositionedNode]


class RoadmapLayoutEngine:
    """Place nodes deterministically using dates when available and order otherwise."""

    def layout(self, roadmap: RoadmapPresentation) -> RoadmapLayout:
        lane_header_width = 150.0
        header_height = 132.0
        lane_height = 122.0
        node_width = 210.0
        node_height = 78.0
        margin_right = 64.0
        timeline_left = lane_header_width + 42.0
        lane_nodes = {
            lane: sorted(
                (node for node in roadmap.nodes if node.lane == lane),
                key=lambda node: (node.order, node.id),
            )
            for lane in roadmap.lanes
        }
        max_per_lane = max((len(nodes) for nodes in lane_nodes.values()), default=1)
        width = max(1180.0, timeline_left + max_per_lane * 242.0 + margin_right)
        height = header_height + max(1, len(roadmap.lanes)) * lane_height + 72.0
        dates = [self._date(node.start or node.target) for node in roadmap.nodes]
        dated = [value for value in dates if value is not None]
        min_day = min(dated) if dated else None
        max_day = max(dated) if dated else None
        span = (max_day - min_day).days if min_day and max_day else 0
        usable = max(1.0, width - timeline_left - node_width - margin_right)

        positioned: list[PositionedNode] = []
        for lane_index, lane in enumerate(roadmap.lanes):
            previous_x = timeline_left - 242.0
            for ordinal, node in enumerate(lane_nodes.get(lane, [])):
                when = self._date(node.start or node.target)
                if when is not None and min_day is not None and span > 0:
                    candidate = timeline_left + usable * ((when - min_day).days / span)
                    x = max(candidate, previous_x + node_width + 30.0)
                else:
                    x = timeline_left + ordinal * 242.0
                x = min(x, width - margin_right - node_width)
                y = header_height + lane_index * lane_height + 22.0
                placed = PositionedNode(node, x, y, node_width, node_height)
                positioned.append(placed)
                previous_x = x
        index = {item.node.id: item for item in positioned}
        return RoadmapLayout(
            width=width,
            height=height,
            header_height=header_height,
            lane_header_width=lane_header_width,
            lane_height=lane_height,
            node_width=node_width,
            node_height=node_height,
            positioned=tuple(positioned),
            node_index=index,
        )

    @staticmethod
    def _date(value: str) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
