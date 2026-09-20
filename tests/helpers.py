"""测试共享夹具:构造内存库服务与标准引用数据。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clearance.service import ClearanceService  # noqa: E402
from clearance.store import Store  # noqa: E402
from clearance.timeutil import parse_instant  # noqa: E402

# 固定评估时刻:2026-09-20 12:00 (Asia/Shanghai)
NOW = parse_instant("2026-09-20T12:00:00+08:00").timestamp()


def make_service(db_path=":memory:", now=NOW):
    return ClearanceService(Store(db_path), clock=lambda: now)


def register_basics(svc):
    """登记标准机型与操作员,返回 (model_id, operator_id)。"""
    svc.put_aircraft_model(
        "m300",
        {
            "max_altitude_m": 500,
            "max_range_m": 50000,
            "endurance_minutes": 240,
            "max_speed_mps": 25,
            "supports_hover": True,
        },
    )
    svc.put_operator(
        "op-01",
        {
            "license_valid_until": "2027-06-01T00:00:00+08:00",
            "categories": ["multirotor"],
            "endorsements": ["night", "survey"],
        },
    )
    return "m300", "op-01"


def square(west, south, east, north):
    """构造正方形 GeoJSON Polygon。"""
    return {
        "type": "Polygon",
        "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
    }


def make_plan(model_id="m300", operator_id="op-01", segments=None, **extra):
    plan = {
        "aircraft_model_id": model_id,
        "operator_id": operator_id,
        "aircraft_category": "multirotor",
        "required_endorsements": ["survey"],
        "segments": segments if segments is not None else default_segments(),
    }
    plan.update(extra)
    return plan


def default_segments():
    """默认四段任务:起飞→航路→悬停→备降,全部远离禁飞区。"""
    return [
        {
            "segment_id": "to", "phase": "takeoff",
            "geometry": {"type": "Point", "coordinates": [117.0, 39.0]},
            "start": "2026-09-20T19:00:00+08:00", "end": "2026-09-20T19:05:00+08:00",
            "altitude_m": 50,
        },
        {
            "segment_id": "enr", "phase": "enroute",
            "geometry": {"type": "LineString", "coordinates": [[117.0, 39.0], [117.01, 39.0]]},
            "start": "2026-09-20T19:05:00+08:00", "end": "2026-09-20T19:30:00+08:00",
            "altitude_m": 120,
        },
        {
            "segment_id": "hov", "phase": "hover",
            "geometry": {"type": "Point", "coordinates": [117.01, 39.0]},
            "start": "2026-09-20T19:30:00+08:00", "end": "2026-09-20T19:40:00+08:00",
            "altitude_m": 80,
        },
        {
            "segment_id": "alt", "phase": "alternate",
            "geometry": {"type": "LineString", "coordinates": [[117.01, 39.0], [117.02, 39.0]]},
            "start": "2026-09-20T19:40:00+08:00", "end": "2026-09-20T19:50:00+08:00",
            "altitude_m": 100,
        },
    ]


def submit(svc, mission_id, key=None, plan=None, base=0, **extra):
    payload = {
        "mission_id": mission_id,
        "idempotency_key": key or f"key-{mission_id}",
        "expected_base_version": base,
        "plan": plan if plan is not None else make_plan(),
    }
    payload.update(extra)
    return svc.submit_mission(payload, actor="tester")
