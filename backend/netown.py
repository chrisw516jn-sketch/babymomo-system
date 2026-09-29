"""
NETOWN API v2.4 量測／運動數據接收與欄位對應。
設備 POST JSON → 寫入檢測紀錄（同日合併）並回傳 MessageCode。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# 阻力設備型號
RESISTANCE_NAMES = {
    "RA01": "划船機",
    "RA02": "上臂(肩推)機",
    "RA03": "擴胸(蝴蝶)機",
    "RA04": "手臂彎舉機",
    "RL01": "屈伸腿機",
    "RL02": "蹬腿機",
    "RL03": "腿部開合機",
    "RC01": "屈腹挺背機",
}

AEROBIC_NAMES = {
    "CL01": "直立式腳踏車",
    "CL02": "斜背式腳踏車",
    "CL05": "手足腳踏車",
}

VITAL_TYPES = {
    "BloodPressure",
    "BodyFat",
    "GripStrength",
    "SitToStandTest",
    "TimedUpAndGoTest",
}

EXERCISE_TYPES = {
    "ResistanceExercise",
    "AerobicExercise",
    "GripTraining",
}

ALL_TYPES = VITAL_TYPES | EXERCISE_TYPES | {"GripTraining"}


def is_netown_payload(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    return (
        "Type" in body
        and "Values" in body
        and ("ID" in body or "Id" in body or "id" in body)
    )


def _s(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _f(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        x = float(v)
        if x == 0:
            return None  # 未量測以 0 表示
        return x
    except (TypeError, ValueError):
        return None


def _f0(v: Any) -> Optional[float]:
    """允許 0（例如次數）。"""
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def normalize_netown_time(raw: Optional[str]) -> Tuple[str, str]:
    """yyyy/MM/dd HH:mm:ss → (date, datetime str)"""
    s = _s(raw).replace("-", "/")
    if not s:
        now = datetime.now()
        return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M:%S")
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    # fallback
    try:
        from utils import normalize_measure_time
        return normalize_measure_time(s)
    except Exception:
        now = datetime.now()
        return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M:%S")


def parse_netown(body: dict) -> dict:
    """
    解析並驗證 Netown JSON。
    成功回傳 dict；失敗 raise ValueError。
    """
    id_card = _s(body.get("ID") or body.get("Id") or body.get("id"))
    machine = _s(body.get("MachineNumber") or body.get("machineNumber"))
    version = _s(body.get("Version") or body.get("version") or "2.4")
    typ = _s(body.get("Type") or body.get("type"))
    measure_time = _s(body.get("MeasureTime") or body.get("measureTime"))
    values = body.get("Values") or body.get("values") or []

    if not id_card:
        raise ValueError("缺少 ID")
    if not typ:
        raise ValueError("缺少 Type")
    if not isinstance(values, list):
        raise ValueError("Values 必須為陣列")
    if typ not in ALL_TYPES and typ not in (
        "ResistanceExercise", "AerobicExercise", "BloodPressure", "BodyFat",
        "GripStrength", "SitToStandTest", "TimedUpAndGoTest", "GripTraining",
    ):
        # 仍接受未知 Type，當運動紀錄存
        pass

    measure_date, mt = normalize_netown_time(measure_time)
    vals = [_s(x) for x in values]

    result = {
        "id_card": id_card.upper(),
        "machine_number": machine,
        "version": version,
        "type": typ,
        "measure_date": measure_date,
        "measure_time": mt,
        "values": vals,
        "is_vital": typ in VITAL_TYPES,
        "fields": {},
        "exercise_summary": "",
    }

    if typ == "BloodPressure":
        # 收縮、舒張、脈搏
        result["fields"] = {
            "systolic": _i(vals[0]) if len(vals) > 0 else None,
            "diastolic": _i(vals[1]) if len(vals) > 1 else None,
            "pulse": _i(vals[2]) if len(vals) > 2 else None,
        }
    elif typ == "BodyFat":
        # 身高、體重、BMI、體脂、肌肉量、體水分、內臟脂肪、體年齡、SMI、腰圍、小腿圍
        result["fields"] = {
            "height": _f(vals[0]) if len(vals) > 0 else None,
            "weight": _f(vals[1]) if len(vals) > 1 else None,
            "bmi": _f(vals[2]) if len(vals) > 2 else None,
            "body_fat": _f(vals[3]) if len(vals) > 3 else None,
            "smi": _f(vals[8]) if len(vals) > 8 else None,
        }
        # 肌肉量 kg 若無 SMI 可略
        muscle_kg = _f(vals[4]) if len(vals) > 4 else None
        if muscle_kg:
            result["fields"]["_muscle_kg"] = muscle_kg
            result["exercise_summary"] = f"肌肉量 {muscle_kg} kg"
            # 除脂肪量約等於 體重-脂肪量；若無 SMI 仍保留肌肉量在摘要
        if muscle_kg and not result["fields"].get("smi"):
            pass
    elif typ == "GripStrength":
        # 握力值、部位(1右2左) — 若僅 2 個值為量測；若 7 個值則為訓練格式誤標
        if len(vals) >= 7:
            # 訓練格式（文件第 9 點曾用 GripStrength）
            result["is_vital"] = False
            result["exercise_summary"] = (
                f"握力訓練 時間{vals[0]}秒 施握{vals[1]}次 達成{vals[2]}次 "
                f"達成率{vals[3]}% 部位{'右' if vals[4]=='1' else '左'} "
                f"目標{vals[5]}kg×{vals[6]}"
            )
        else:
            grip = _f(vals[0]) if len(vals) > 0 else None
            result["fields"] = {"grip_strength": grip}
            side = _s(vals[1]) if len(vals) > 1 else ""
            if side == "1":
                result["exercise_summary"] = "右手握力"
            elif side == "2":
                result["exercise_summary"] = "左手握力"
    elif typ == "SitToStandTest":
        result["fields"] = {
            "chair_stand_time": _f0(vals[0]) if len(vals) > 0 else None,
        }
    elif typ == "TimedUpAndGoTest":
        # 起身行走秒數 → 對應系統走路時間
        result["fields"] = {
            "walking_time": _f0(vals[0]) if len(vals) > 0 else None,
        }
    elif typ == "ResistanceExercise":
        code = vals[0] if vals else ""
        name = RESISTANCE_NAMES.get(code, code)
        result["exercise_summary"] = (
            f"阻力運動 {name} 時間{vals[1] if len(vals)>1 else '-'}秒 "
            f"次數{vals[2] if len(vals)>2 else '-'} "
            f"消耗{vals[3] if len(vals)>3 else '-'}kcal "
            f"達成率{vals[4] if len(vals)>4 else '-'}%"
        )
    elif typ == "AerobicExercise":
        code = vals[0] if vals else ""
        name = AEROBIC_NAMES.get(code, code)
        result["exercise_summary"] = (
            f"有氧運動 {name} 時間{vals[1] if len(vals)>1 else '-'}秒 "
            f"消耗{vals[2] if len(vals)>2 else '-'}kcal "
            f"距離{vals[3] if len(vals)>3 else '-'}km "
            f"瓦特{vals[4] if len(vals)>4 else '-'} 轉速{vals[5] if len(vals)>5 else '-'}"
        )
    elif typ == "GripTraining":
        result["exercise_summary"] = (
            f"握力訓練 時間{vals[0] if vals else '-'}秒 "
            f"施握{vals[1] if len(vals)>1 else '-'}次 "
            f"達成{vals[2] if len(vals)>2 else '-'}次 "
            f"達成率{vals[3] if len(vals)>3 else '-'}% "
            f"部位{'右' if len(vals)>4 and vals[4]=='1' else '左'} "
            f"目標{vals[5] if len(vals)>5 else '-'}kg×{vals[6] if len(vals)>6 else '-'}"
        )
    else:
        result["exercise_summary"] = f"{typ}: {','.join(vals[:8])}"

    return result


def netown_api_key_ok(headers: dict) -> bool:
    """若環境變數 NETOWN_API_KEY 有設定，則要求 X-API-Key 或 Authorization Bearer 相符。"""
    expected = (os.getenv("NETOWN_API_KEY") or "").strip()
    if not expected:
        return True
    key = (headers.get("x-api-key") or headers.get("X-API-Key") or "").strip()
    if key and key == expected:
        return True
    auth = (headers.get("authorization") or headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer ") and auth[7:].strip() == expected:
        return True
    return False
