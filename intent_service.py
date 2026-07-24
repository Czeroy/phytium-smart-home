import re

import ai

from device_control import (
    COMMAND_LABELS,
    current_state,
    optimize_commands,
    update_environment,
)
from runtime_store import parse_amount


def has_any(text, words):
    return any(word in text for word in words)


def add_command(commands, command):
    if command not in commands:
        commands.append(command)


ASR_CONTEXT_WORDS = [
    "空调", "风速", "风量", "风力", "风档", "风挡", "温度",
    "制冷", "制热", "除湿", "送风", "暖风", "冷风",
]


def correct_asr_text(text):
    """修正常见语音转文字偏差，再交给意图模型和规则解析。"""
    corrected = ai.normalize_text(text)
    corrections = []

    def remember(old, new, label):
        corrections.append({"from": old, "to": new, "reason": label})

    def replace_phrase(old, new, label="常见语音错字"):
        nonlocal corrected
        if old in corrected:
            corrected = corrected.replace(old, new)
            remember(old, new, label)

    phrase_replacements = [
        ("条形", "调成"),
        ("调形", "调成"),
        ("条成", "调成"),
        ("挑成", "调成"),
        ("跳成", "调成"),
        ("调曾", "调成"),
        ("支热", "制热"),
        ("之热", "制热"),
        ("只热", "制热"),
        ("治热", "制热"),
        ("支冷", "制冷"),
        ("之冷", "制冷"),
        ("只冷", "制冷"),
        ("开支热", "开制热"),
        ("开之热", "开制热"),
        ("开只热", "开制热"),
        ("开治热", "开制热"),
        ("开支冷", "开制冷"),
        ("开之冷", "开制冷"),
        ("开只冷", "开制冷"),
        ("开至冷", "开制冷"),
        ("制楞", "制冷"),
        ("质冷", "制冷"),
    ]
    for old, new in phrase_replacements:
        replace_phrase(old, new)

    smart_home_context = has_any(corrected, ASR_CONTEXT_WORDS)
    if smart_home_context and "开支" in corrected and "开支灯" not in corrected:
        target = "开制冷" if has_any(corrected, ["冷", "凉", "降温", "智能"]) else "开制热"
        before = corrected
        corrected = corrected.replace("开支", target)
        remember("开支", target, "空调上下文补全模式词")
        if corrected == before:
            corrections.pop()

    wind_context = has_any(corrected, ["风速", "风量", "风力", "风档", "风挡"])
    if wind_context:
        wind_low_patterns = [
            (
                r"((?:风速|风量|风力|风档|风挡)(?:调成|调到|设为|设置为)?)[的地得](?=$|并|和|然后|再|，|。|,)",
                r"\1低",
            ),
            (
                r"((?:调成|调到|设为|设置为))[的地得](?=$|并|和|然后|再|，|。|,)",
                r"\1低",
            ),
        ]
        for pattern, replacement in wind_low_patterns:
            updated = re.sub(pattern, replacement, corrected)
            if updated != corrected:
                remember(corrected, updated, "风速上下文把的/地/得纠成低")
                corrected = updated

    return corrected, corrections


def parse_timer_delay(text):
    normalized = ai.normalize_text(text)
    if "半小时后" in normalized or "半个小时后" in normalized:
        return 30 * 60

    match = re.search(r"(\d+|[一二两三四五六七八九十]+)(秒|分钟|分|小时|个小时)后", normalized)
    if not match:
        return None

    value = parse_amount(match.group(1))
    if value is None:
        return None
    unit = match.group(2)
    if unit == "秒":
        return value
    if unit in {"分钟", "分"}:
        return value * 60
    return value * 3600


def infer_rules(text):
    normalized = ai.normalize_text(text)
    intents = set()
    params = {}
    commands = []

    light_context = has_any(normalized, ["灯", "灯光", "照明", "台灯"])
    if light_context:
        intents.add("开关灯")
        if has_any(normalized, ["关灯", "灯关", "关闭灯", "关掉灯", "把灯关", "关闭照明"]):
            add_command(commands, "light_off")
        elif has_any(normalized, ["开灯", "灯开", "打开灯", "开启灯", "把灯打开", "打开照明"]):
            add_command(commands, "light_on")
        elif has_any(normalized, ["切换灯", "灯切换"]):
            add_command(commands, "light_toggle")

    if has_any(normalized, ["关空调", "关闭空调", "空调关机", "把空调关了", "停空调"]):
        intents.add("空调开关")
        add_command(commands, "ac_off")
    elif has_any(normalized, ["开空调", "打开空调", "开启空调", "空调开机", "启动空调"]):
        intents.add("空调开关")
        add_command(commands, "ac_on")

    wants_cool = (
        has_any(normalized, ["制冷", "冷气", "冷风", "降温"])
        or ("热" in normalized and "制热" not in normalized)
    )
    wants_heat = (
        has_any(normalized, ["制热", "暖气", "暖风", "热风", "升温"])
        or ("冷" in normalized and "制冷" not in normalized)
    )
    if wants_cool:
        intents.add("空调制冷")
        add_command(commands, "ac_on")
        add_command(commands, "mode_cool")
    if wants_heat:
        intents.add("空调制热")
        add_command(commands, "ac_on")
        add_command(commands, "mode_heat")

    if has_any(normalized, ["自动模式", "空调自动", "自动空调"]):
        intents.add("模式切换")
        params["mode"] = "auto"
        add_command(commands, "ac_on")
        add_command(commands, "mode_auto")
    elif has_any(normalized, ["除湿", "抽湿", "去湿", "祛湿"]):
        intents.add("模式切换")
        params["mode"] = "dry"
        add_command(commands, "ac_on")
        add_command(commands, "mode_dry")
    elif has_any(normalized, ["送风", "风扇模式", "吹风模式", "通风模式"]):
        intents.add("模式切换")
        params["mode"] = "fan"
        add_command(commands, "ac_on")
        add_command(commands, "mode_fan")

    wind_context = (
        has_any(normalized, ["风速", "风量", "风力", "风档", "风挡"])
        or re.search(r"(低|中|高|大|小|强|弱|自动)(风|速|档|挡)", normalized)
    )
    if wind_context:
        intents.add("风速调节")
        if "自动" in normalized:
            params["wind_speed"] = "auto"
            add_command(commands, "fan_auto")
        elif re.search(r"(低速|低风|小风|弱风|最小|风速.{0,4}低|风量.{0,4}小|风力.{0,4}弱|调低风|调小风)", normalized):
            params["wind_speed"] = "low"
            add_command(commands, "fan_low")
        elif re.search(r"(中速|中风|中档|中挡|中等|风速.{0,4}中|风量.{0,4}中|风力.{0,4}中|调成中|调到中)", normalized):
            params["wind_speed"] = "mid"
            add_command(commands, "fan_mid")
        elif re.search(r"(高速|高风|大风|强风|最大|风速.{0,4}高|风量.{0,4}大|风力.{0,4}强|调高风|调大风)", normalized):
            params["wind_speed"] = "high"
            add_command(commands, "fan_high")

    temp_match = re.search(r"([1-3]?\d)\s*度", normalized)
    if temp_match:
        target_temp = int(temp_match.group(1))
        if 16 <= target_temp <= 30:
            intents.add("温度调节")
            params["temperature"] = target_temp
            diff = target_temp - current_state["temperature"]
            step_command = "temp_up" if diff > 0 else "temp_down"
            for _ in range(min(abs(diff), 14)):
                commands.append(step_command)
    elif has_any(normalized, ["温度调高", "调高温度", "高一点温度", "升温"]):
        intents.add("温度调节")
        params["adjustment"] = "increase"
        add_command(commands, "temp_up")
    elif has_any(normalized, ["温度调低", "调低温度", "低一点温度", "降温"]):
        intents.add("温度调节")
        params["adjustment"] = "decrease"
        add_command(commands, "temp_down")

    timer_delay = parse_timer_delay(normalized)
    timer = None
    if timer_delay is not None:
        intents.add("定时控制")
        if commands:
            timer = {
                "delay_seconds": timer_delay,
                "commands": optimize_commands(commands),
                "label": text,
            }
            commands = []
    elif has_any(normalized, ["定时", "预约"]):
        intents.add("定时控制")

    commands = optimize_commands(commands)

    return {
        "intents": intents,
        "params": params,
        "commands": commands,
        "timer": timer,
        "light_context": light_context,
        "wind_context": bool(wind_context),
        "wants_cool": wants_cool,
        "wants_heat": wants_heat,
    }


def corrected_intent_result(text):
    corrected_text, asr_corrections = correct_asr_text(text)
    normalized = ai.normalize_text(corrected_text)
    model_active, probs, model_params = ai.predict(corrected_text, ai.model, ai.vocab, ai.config.MAX_LEN)
    probabilities = {
        intent: round(float(prob), 3)
        for intent, prob in zip(ai.config.INTENTS, probs)
    }
    rules = infer_rules(corrected_text)
    sorted_scores = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)
    top_intent, top_score = sorted_scores[0] if sorted_scores else ("", 0.0)
    second_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0.0

    active = set(model_active)
    if not rules["light_context"]:
        active.discard("开关灯")
        probabilities["开关灯"] = min(probabilities.get("开关灯", 0.0), 0.2)
    if not rules["wind_context"]:
        active.discard("风速调节")
        probabilities["风速调节"] = min(probabilities.get("风速调节", 0.0), 0.2)

    if rules["wants_cool"] and not rules["wants_heat"]:
        active.discard("空调制热")
        probabilities["空调制热"] = min(probabilities.get("空调制热", 0.0), 0.2)
    if rules["wants_heat"] and not rules["wants_cool"]:
        active.discard("空调制冷")
        probabilities["空调制冷"] = min(probabilities.get("空调制冷", 0.0), 0.2)

    active.update(rules["intents"])
    for intent in rules["intents"]:
        probabilities[intent] = max(probabilities.get(intent, 0.0), 0.99)

    params = {**model_params, **rules["params"]}
    ordered_active = [intent for intent in ai.config.INTENTS if intent in active]
    ordered_rule_intents = [intent for intent in ai.config.INTENTS if intent in rules["intents"]]
    has_rule_result = bool(rules["intents"] or rules["commands"] or rules["timer"])
    conflict = rules["wants_cool"] and rules["wants_heat"]
    model_only_control = bool(ordered_active) and not has_rule_result
    model_only_low_confidence = (
        bool(ordered_active)
        and not has_rule_result
        and 0.5 <= top_score < 0.75
    )
    close_scores = bool(ordered_active) and top_score >= 0.5 and (top_score - second_score) < 0.12
    needs_confirmation = bool(
        conflict
        or model_only_control
    )
    confirmation_reason = ""
    if conflict:
        confirmation_reason = "同时出现制冷和制热相关表达"
    elif model_only_low_confidence:
        confirmation_reason = "模型置信度偏低"
    elif close_scores:
        confirmation_reason = "多个意图分数接近"
    elif model_only_control:
        confirmation_reason = "仅模型判断，未命中明确规则"

    final_scores = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)
    final_top_intent, final_top_score = final_scores[0] if final_scores else ("", 0.0)
    final_second_score = final_scores[1][1] if len(final_scores) > 1 else 0.0

    return {
        "success": True,
        "text": text,
        "original_text": text,
        "corrected_text": corrected_text,
        "asr_corrected": corrected_text != ai.normalize_text(text),
        "asr_corrections": asr_corrections,
        "normalized_text": normalized,
        "intents": ordered_active,
        "model_intents": model_active,
        "rule_intents": ordered_rule_intents,
        "probabilities": probabilities,
        "params": params,
        "commands": rules["commands"],
        "command_labels": [COMMAND_LABELS.get(command, command) for command in rules["commands"]],
        "timer": rules["timer"],
        "environment": update_environment() if "查环境" in ordered_active else None,
        "confidence": {
            "top_intent": final_top_intent,
            "top_score": round(float(final_top_score), 3),
            "second_score": round(float(final_second_score), 3),
            "rule_based": has_rule_result,
        },
        "needs_confirmation": needs_confirmation,
        "confirmation_reason": confirmation_reason,
    }
