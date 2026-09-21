"""業務邏輯：肌少症分期判斷、BMI 計算、異常項目統計"""

from typing import Optional, Tuple


def calc_bmi(height_cm: Optional[float], weight_kg: Optional[float]) -> Optional[float]:
    if not height_cm or not weight_kg or height_cm <= 0:
        return None
    h = height_cm / 100.0
    return round(weight_kg / (h * h), 1)


def judge_sarcopenia(
    gender: str,
    grip: Optional[float],
    chair: Optional[float],
    walk: Optional[float],
    smi: Optional[float],
    age: Optional[int] = None,
) -> Tuple[str, int, str]:
    """
    依年齡＋性別標準值判斷肌少症分期（61–96歲表；其餘回退 AWGS 預設）。
    回傳 (stage, abnormal_count, status_text)
    """
    is_male = str(gender or "").upper() in ("M", "男")

    # 預設（AWGS）
    grip_std = 28.0 if is_male else 18.0
    smi_std = 7.0 if is_male else 5.7
    chair_std = 12.0   # 大於等於此秒數視為偏慢
    walk_std = 20.0    # 大於等於此秒數視為偏慢

    # 有年齡時套用標準值表
    try:
        from standards import get_standards
        std = get_standards(age, gender) if age is not None else None
        if std:
            if std.get("grip", {}).get("op") == "gte":
                grip_std = float(std["grip"]["value"])
            if std.get("smi", {}).get("op") == "gte":
                smi_std = float(std["smi"]["value"])
            if std.get("chair_stand", {}).get("op") == "lt":
                chair_std = float(std["chair_stand"]["value"])
            if std.get("walking", {}).get("op") == "lt":
                walk_std = float(std["walking"]["value"])
    except Exception:
        pass

    issues = []
    low_muscle = False   # 肌力或肌肉量不足
    low_function = False # 身體功能不足

    if grip is not None:
        if grip < grip_std:
            issues.append(f"握力不足 ({grip:.1f} kg，標準≧{grip_std:g})")
            low_muscle = True

    if smi is not None:
        if smi < smi_std:
            issues.append(f"SMI肌肉量偏低 ({smi:.1f}，標準≧{smi_std:g})")
            low_muscle = True

    if chair is not None:
        if chair >= chair_std:
            issues.append(f"坐站偏慢 ({chair:.2f} s，標準<{chair_std:g})")
            low_function = True

    if walk is not None:
        if walk >= walk_std:
            issues.append(f"步速偏慢 ({walk:.2f} s，標準<{walk_std:g})")
            low_function = True

    # 分期邏輯（簡化版）
    if low_muscle and low_function:
        stage = "嚴重肌少症"
    elif low_muscle:
        stage = "肌少症"
    elif low_function:
        stage = "肌少症前期"
    else:
        stage = "正常"

    # 血壓額外提示（不影響分期）
    # 這裡只在 status 顯示

    status = " / ".join(issues) if issues else "各項指標正常"
    return stage, len(issues), status


def bp_status(systolic, diastolic) -> str:
    if systolic is None or diastolic is None:
        return "未量測"
    if systolic >= 180 or diastolic >= 120:
        return "高血壓危急"
    if systolic >= 160 or diastolic >= 100:
        return "血壓偏高（需關懷）"
    if systolic >= 140 or diastolic >= 90:
        return "血壓偏高"
    if systolic < 90 or diastolic < 60:
        return "血壓偏低"
    return "血壓正常"


def format_alert_message(rec) -> str:
    """組成通報與 LINE 訊息內容（含異常標記）。"""
    stage = getattr(rec, "sarcopenia_stage", None) or "正常"
    abn = getattr(rec, "abnormal_count", 0) or 0
    bp = bp_status(getattr(rec, "systolic", None), getattr(rec, "diastolic", None))
    gender = getattr(rec, "gender", None) or ""
    g = "男" if str(gender).upper() in ("M", "男") else ("女" if str(gender).upper() in ("F", "女") else gender)

    grip = getattr(rec, "grip_strength", None)
    chair = getattr(rec, "chair_stand_time", None)
    walk = getattr(rec, "walking_time", None)
    smi = getattr(rec, "smi", None)

    def mark(ok: bool, text: str) -> str:
        return (("⚠️ " if not ok else "✓ ") + text)

    age = getattr(rec, "age", None)
    try:
        from standards import get_standards
        std = get_standards(age, gender) if age is not None else None
    except Exception:
        std = None

    grip_cut = 28.0 if str(gender).upper() in ("M", "男") else 18.0
    smi_cut = 7.0 if str(gender).upper() in ("M", "男") else 5.7
    chair_cut = 12.0
    walk_cut = 20.0
    if std:
        if std.get("grip", {}).get("op") == "gte":
            grip_cut = float(std["grip"]["value"])
        if std.get("smi", {}).get("op") == "gte":
            smi_cut = float(std["smi"]["value"])
        if std.get("chair_stand", {}).get("op") == "lt":
            chair_cut = float(std["chair_stand"]["value"])
        if std.get("walking", {}).get("op") == "lt":
            walk_cut = float(std["walking"]["value"])

    grip_ok = True if grip is None else grip >= grip_cut
    chair_ok = True if chair is None else chair < chair_cut
    walk_ok = True if walk is None else walk < walk_cut
    smi_ok = True if smi is None else smi >= smi_cut
    bp_ok = bp in ("血壓正常", "未量測")

    grip_txt = f"握力：{grip if grip is not None else '-'} kg"
    chair_txt = f"五次坐站：{chair if chair is not None else '-'} 秒"
    walk_txt = f"走路時間：{walk if walk is not None else '-'} 秒"
    smi_txt = f"SMI：{smi if smi is not None else '-'}"
    bp_txt = f"血壓：{rec.systolic or '-'}/{rec.diastolic or '-'} mmHg（{bp}）"

    return (
        "【寶貝機異常通報】\n"
        f"個案：{rec.user_name}（{rec.id_card}）\n"
        f"性別/年齡：{g} / {rec.age or '-'} 歲\n"
        f"檢測時間：{rec.measure_time or rec.measure_date or '-'}\n"
        "────────────────\n"
        f"{mark(grip_ok, grip_txt)}\n"
        f"{mark(chair_ok, chair_txt)}\n"
        f"{mark(walk_ok, walk_txt)}\n"
        f"{mark(smi_ok, smi_txt)}\n"
        f"{mark(bp_ok, bp_txt)}\n"
        f"脈搏：{rec.pulse or '-'} bpm\n"
        f"BMI：{rec.bmi if rec.bmi is not None else '-'}　身高/體重：{rec.height or '-'} cm / {rec.weight or '-'} kg\n"
        "────────────────\n"
        f"肌少症分期：{stage}\n"
        f"異常項目數：{abn}\n"
        f"說明：{rec.status or '-'}\n"
        "請護理師／照顧服務員盡快關懷並記錄處理。"
    )


def normalize_measure_time(raw: Optional[str]) -> Tuple[str, str]:
    """回傳 (measure_date, measure_time)"""
    from datetime import datetime
    if not raw:
        now = datetime.now()
        return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M:%S")

    raw = str(raw).strip().replace("/", "-")
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    # fallback
    now = datetime.now()
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M:%S")


def get_exercise_advice(stage: Optional[str]) -> dict:
    """依肌少症分期回傳實用運動／復健建議（可執行、安全優先）。"""
    stage = stage or "正常"
    catalog = {
        "正常": {
            "title": "維持期運動建議",
            "summary": "各項指標大致正常，建議維持規律活動與均衡飲食。",
            "retest_days": 90,
            "items": [
                "每週至少 150 分鐘中等強度有氧（快走、太極、游泳），可分段完成。",
                "每週 2～3 次阻力訓練：啞鈴／彈力帶／自體重量（深蹲、椅上起身）。",
                "蛋白質建議：每公斤體重約 1.0～1.2 g／日，並分散於三餐。",
                "保持日常步行與社交活動，避免長時間久坐。",
            ],
        },
        "肌少症前期": {
            "title": "肌少症前期 · 強化建議",
            "summary": "出現肌力或功能警訊，建議增加阻力訓練與蛋白質，並於 2～4 週複測。",
            "retest_days": 21,
            "items": [
                "阻力訓練優先：每週 3 次，每次 20～30 分鐘（坐站、靠牆深蹲、彈力帶划船）。",
                "步態練習：每日快走 20～30 分鐘，必要時使用助行器確保安全。",
                "蛋白質：每公斤體重約 1.2～1.5 g／日，早餐與運動後補充。",
                "平衡訓練：單腳站立（扶穩）、腳跟對腳尖走路，每次 5～10 分鐘。",
                "若有疼痛或暈眩，立即停止並告知護理／醫師。",
            ],
        },
        "肌少症": {
            "title": "肌少症 · 復健追蹤建議",
            "summary": "符合肌少症指標，建議轉介專業評估，並安排追蹤與復健運動。",
            "retest_days": 14,
            "items": [
                "轉介物理治療或復健科評估，制定個人化阻力與步態計畫。",
                "居家簡易阻力：椅上坐站 10 下 × 2～3 組／日；彈力帶上肢 2～3 組。",
                "蛋白質與熱量充足，必要時營養師介入；維生素 D 可依醫囑補充。",
                "跌倒預防：清除居家障礙物、加裝扶手、夜間照明。",
                "建議 2 週內複測握力／坐站／步速，並記錄關懷結果。",
            ],
        },
        "嚴重肌少症": {
            "title": "嚴重肌少症 · 高優先關懷",
            "summary": "多重指標異常，建議儘速關懷並轉介醫療，必要時安排居家安全評估。",
            "retest_days": 7,
            "items": [
                "儘速轉介醫師／復健，評估是否需住院或密集復健。",
                "在專業指導下進行低負荷阻力與床邊／椅邊活動，避免跌倒。",
                "營養支持：高蛋白、適量熱量；吞嚥困難者需評估。",
                "居家安全與照顧人力評估；必要時申請長照資源。",
                "一週內複測並持續電話／到宅關懷，記錄處理結果。",
            ],
        },
    }
    return catalog.get(stage, catalog["正常"])


def suggested_retest_date(stage: Optional[str], from_date: Optional[str] = None) -> str:
    """依分期建議複檢日期（YYYY-MM-DD）。"""
    from datetime import datetime, timedelta
    advice = get_exercise_advice(stage)
    days = advice.get("retest_days", 30)
    base = datetime.now()
    if from_date:
        try:
            base = datetime.strptime(str(from_date)[:10], "%Y-%m-%d")
        except Exception:
            pass
    return (base + timedelta(days=days)).strftime("%Y-%m-%d")


# 預設真茂科技運動輔具目錄（管理員可於後台覆寫）
DEFAULT_EQUIPMENT_CATALOG = [
    {
        "id": "rower",
        "name": "划船健身機",
        "why": "改善上背與握力，對握力不足、肌少分期較有幫助。",
        "how": "1～2 組 × 8～12 下。雙手輕握把手、背部打直，往胸口方向拉。節奏放慢、吐氣出力、不要憋氣。感覺還能再做 3 下再停。",
        "caution": "須有人員在旁、以輕負荷為主，有胸悶、暈眩或血壓明顯偏高時停止。",
        "triggers": ["grip", "stage", "always_abnormal"],
    },
    {
        "id": "pec_fly",
        "name": "擴胸蝴蝶機",
        "why": "訓練胸肌與上肢推的力量，協助維持上半身肌量。",
        "how": "1～2 組 × 8～12 下。雙手打開再往中間合攏，肩頸放鬆。節奏放慢、吐氣出力、不要憋氣。感覺還能再做 3 下再停。",
        "caution": "血壓偏高時先以輕負荷為主。",
        "triggers": ["grip", "smi", "stage"],
    },
    {
        "id": "shoulder_press",
        "name": "上臂肩推機",
        "why": "強化肩膀與上臂，日常舉手、拿物品較穩。",
        "how": "1～2 組 × 8～12 下。座椅調到手肘約與肩同高，向上推到快伸直即停。節奏放慢、吐氣出力、不要憋氣。感覺還能再做 3 下再停。",
        "caution": "血壓偏高時先不做肩推，改用划船或蝴蝶機輕負荷。",
        "triggers": ["grip", "stage"],
    },
    {
        "id": "leg_press",
        "name": "蹬腿機",
        "why": "強化大腿與臀部，對坐站偏慢、步速偏慢最直接。",
        "how": "1～2 組 × 8～12 下。雙腳與肩同寬，膝蓋朝腳尖方向，不要完全鎖死。節奏放慢、吐氣出力、不要憋氣。感覺還能再做 3 下再停。",
        "caution": "膝蓋不適者減量或改半程。",
        "triggers": ["chair", "walk", "smi", "stage"],
    },
    {
        "id": "leg_curl",
        "name": "屈伸腿機",
        "why": "訓練大腿前側／後側，協助站起、上下階與走路穩定。",
        "how": "1～2 組 × 8～12 下。先做伸腿再做屈腿，活動到舒適角度即可。節奏放慢、吐氣出力、不要憋氣。感覺還能再做 3 下再停。",
        "caution": "膝蓋疼痛時減少角度與負荷。",
        "triggers": ["chair", "walk", "smi", "stage"],
    },
]


def get_default_equipment_catalog() -> list:
    return [dict(x) for x in DEFAULT_EQUIPMENT_CATALOG]


def get_intervention_plan(
    stage: Optional[str] = None,
    grip: Optional[float] = None,
    chair: Optional[float] = None,
    walk: Optional[float] = None,
    smi: Optional[float] = None,
    gender: Optional[str] = None,
    bp_status_text: Optional[str] = None,
    equipment_catalog: Optional[list] = None,
) -> dict:
    """
    異常個案應對方案（含真茂科技運動輔具建議）。
    equipment_catalog 可由管理員在後台覆寫。
    """
    stage = stage or "正常"
    is_male = str(gender or "").upper() in ("M", "男")
    grip_low = grip is not None and grip < (28.0 if is_male else 18.0)
    chair_slow = chair is not None and chair >= 12.0
    walk_slow = walk is not None and walk >= 18.0
    smi_low = smi is not None and smi < (7.0 if is_male else 5.7)
    bp_care = bool(bp_status_text and bp_status_text not in ("血壓正常", "未量測", None))
    has_stage = stage in ("肌少症前期", "肌少症", "嚴重肌少症")
    any_abn = grip_low or chair_slow or walk_slow or smi_low or has_stage or bp_care

    # 立即處置
    immediate = []
    if stage == "嚴重肌少症":
        immediate.append("高優先：儘速由護理師／醫師評估，必要時轉介復健或急診。")
        immediate.append("確認是否有跌倒、進食困難、體重驟降，並通知家屬。")
    elif stage == "肌少症":
        immediate.append("安排 1～2 週內復健／體適能指導，並建立關懷追蹤。")
    elif stage == "肌少症前期":
        immediate.append("納入強化運動名單，2～4 週後複測握力／坐站／步速。")
    else:
        immediate.append("維持規律活動，定期追蹤即可。")

    if grip_low:
        immediate.append("握力不足：優先安排上肢阻力訓練（可搭配划船機低阻抗）。")
    if chair_slow:
        immediate.append("坐站偏慢：加強下肢肌力與功能性起身訓練。")
    if walk_slow:
        immediate.append("步速偏慢：安排步態與有氧訓練，注意防跌。")
    if smi_low:
        immediate.append("肌肉量偏低：阻力訓練＋蛋白質營養並行。")
    if bp_care:
        immediate.append(f"血壓需關懷（{bp_status_text}）：運動前先量測，異常勿勉強訓練。")

    # 真茂科技運動輔具建議（可編輯目錄）
    catalog = equipment_catalog if equipment_catalog is not None else get_default_equipment_catalog()
    flag_map = {
        "grip": grip_low,
        "chair": chair_slow,
        "walk": walk_slow,
        "smi": smi_low,
        "bp": bp_care,
        "stage": has_stage,
        "always_abnormal": any_abn,
        "always": True,
    }
    equipment = []
    for item in catalog:
        triggers = item.get("triggers") or ["always_abnormal"]
        if any(flag_map.get(t, False) for t in triggers):
            equipment.append({
                "id": item.get("id") or "",
                "name": item.get("name") or "",
                "why": item.get("why") or "",
                "how": item.get("how") or "",
                "caution": item.get("caution") or "",
            })
    if not equipment and catalog:
        # 無符合條件時仍顯示第一筆作為維持建議
        first = catalog[0]
        equipment.append({
            "id": first.get("id") or "",
            "name": first.get("name") or "維持訓練輔具",
            "why": first.get("why") or "維持肌力與心肺。",
            "how": first.get("how") or "每週 2～3 次，量力而為。",
            "caution": first.get("caution") or "不適即停。",
        })

    # 訓練原則
    protocol = []
    if stage == "嚴重肌少症":
        protocol = [
            "僅在專業人員看視下使用輔具，先以被動／輔助動作為主。",
            "單次訓練總時不超過 15 分鐘，可拆成多次短時段。",
            "訓練前後量血壓與主觀不適，並寫入關懷紀錄。",
        ]
    elif stage == "肌少症":
        protocol = [
            "每週至少 3 次，阻力＋有氧各佔一部分；划船機可作為主軸之一。",
            "同一動作以「能完成、略感費力」為原則，隔日再練。",
            "2 週後複測握力、五次坐站、走路時間。",
        ]
    elif stage == "肌少症前期":
        protocol = [
            "每週 3 次，每次 20～30 分鐘；划船機 10 分鐘＋下肢或步態 10～15 分鐘。",
            "逐漸增加阻抗或次數，避免一次加太多。",
            "3～4 週後複測並調整方案。",
        ]
    else:
        protocol = [
            "每週 2～3 次維持訓練即可。",
            "可搭配真茂划船機作全身性活動。",
        ]

    care = [
        "將本方案告知個案／家屬，並於關懷紀錄註記「已建議輔具與運動」。",
        "若使用輔具後疼痛加劇、跌倒或血壓異常，暫停並回報護理師。",
        f"建議複檢日：約 {get_exercise_advice(stage).get('retest_days', 30)} 天後再測體適能。",
    ]

    title_map = {
        "正常": "維持期應對方案",
        "肌少症前期": "肌少症前期 · 強化應對方案",
        "肌少症": "肌少症 · 復健與輔具方案",
        "嚴重肌少症": "嚴重肌少症 · 高優先應對方案",
    }

    return {
        "title": title_map.get(stage, "異常應對方案"),
        "stage": stage,
        "immediate": immediate,
        "equipment": equipment,
        "protocol": protocol,
        "care": care,
        "brand_note": "依本次異常項目對應館內器材。須有人員在旁、以輕負荷為主，有胸悶、暈眩或血壓明顯偏高時停止。",
    }
