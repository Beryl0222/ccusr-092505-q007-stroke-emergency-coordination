"""分诊规则：把登记内容映射为分级提示。

规则只产生"系统提示"，每一条提示都携带命中依据并显式标注
``is_diagnosis=False``；是否构成诊断由专业人员判断单独记录
（见 aggregate.py 中的 PROFESSIONAL_DECISION_RECORDED）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Mapping, Sequence


class TriageLevel(IntEnum):
    """分级数值越大越紧急，便于做单调升级比较。"""

    GREEN = 1
    YELLOW = 2
    ORANGE = 3
    RED = 4

    @property
    def label(self) -> str:
        return {1: "绿色", 2: "黄色", 3: "橙色", 4: "红色"}[self.value]


# 关键词表保持在交换层之外：接线员原样登记症状原话，
# 这里只做可解释的关键词命中，命中词会写进依据。
RED_KEYWORDS: Mapping[str, Sequence[str]] = {
    "突发剧烈头痛": ("剧烈头痛", "炸裂样头痛", "这辈子最痛", "雷击样"),
    "言语障碍": ("言语含糊", "说话不清", "说不出话", "言语不清"),
    "面瘫": ("口角歪斜", "嘴歪", "面部歪斜"),
    "单侧肢体无力或麻木": ("一侧肢体无力", "半边无力", "一侧手脚麻木", "偏瘫", "抬不起来"),
    "意识障碍": ("意识不清", "昏迷", "嗜睡叫不醒", "意识模糊"),
    "抽搐": ("抽搐", "抽风", "痉挛发作"),
    "突发视力异常": ("突然看不见", "视物重影", "一侧视野缺损"),
}

ORANGE_KEYWORDS: Mapping[str, Sequence[str]] = {
    "头痛伴呕吐": ("呕吐", "喷射性呕吐", "呕出", "吐了"),
    "持续加重头痛": ("头痛加重", "一直头痛"),
}

YELLOW_KEYWORDS: Mapping[str, Sequence[str]] = {
    "头痛": ("头痛", "头疼"),
    "呕吐不适": ("想吐", "恶心"),
    "头晕眩晕": ("头晕", "眩晕", "站不稳"),
}

# 既往风险：单独出现不抬到红色，但与其他信号叠加要升级。
RISK_FACTOR_KEYWORDS: Mapping[str, Sequence[str]] = {
    "既往脑卒中或短暂性脑缺血": ("脑梗", "脑出血", "中风", "卒中", "短暂性脑缺血", "TIA"),
    "高血压病史": ("高血压",),
    "糖尿病病史": ("糖尿病",),
}

# 抗凝/抗血小板用药在疑似卒中场景下显著抬高出血风险。
ANTICOAGULANT_KEYWORDS: Sequence[str] = (
    "华法林",
    "利伐沙班",
    "阿哌沙班",
    "达比加群",
    "肝素",
    "阿司匹林",
    "氯吡格雷",
    "替格瑞洛",
    "抗凝药",
    "抗血小板",
)


@dataclass(frozen=True)
class MatchedRule:
    code: str
    level: TriageLevel
    description: str
    evidence: str


@dataclass(frozen=True)
class TriageResult:
    level: TriageLevel
    matched: tuple[MatchedRule, ...] = field(default_factory=tuple)
    is_diagnosis: bool = False  # 系统提示永远不是诊断结论

    @property
    def hint_text(self) -> str:
        reasons = "；".join(rule.description for rule in self.matched) or "未命中高危关键词"
        return (
            f"系统分诊提示：{self.level.label}级别（{reasons}）。"
            "本提示由登记规则自动生成，不是诊断结论，最终分级以专业人员判断为准。"
        )


def _hit(keyword_groups: Mapping[str, Sequence[str]], text: str) -> tuple[str, str]:
    for description, keywords in keyword_groups.items():
        for keyword in keywords:
            if keyword in text:
                return description, keyword
    return "", ""


def evaluate_triage(
    symptom_quotes: Sequence[str],
    risk_history: Sequence[str] = (),
    medications: Sequence[str] = (),
) -> TriageResult:
    """根据症状原话、既往风险、用药评估分级。

    入参全部使用登记时的原始文本，避免接线员在录入环节做医学转述；
    评估只做关键词命中，升级规则写在本函数里、可逐条复盘。
    """
    matched: list[MatchedRule] = []
    level = TriageLevel.GREEN
    orange_via_vomit_and_headache = False
    headache_seen = False
    vomit_seen = False

    for quote in symptom_quotes:
        text = quote or ""
        desc, keyword = _hit(RED_KEYWORDS, text)
        if desc:
            level = TriageLevel.RED
            matched.append(
                MatchedRule("RED_SYMPTOM", TriageLevel.RED, desc, f"原话命中“{keyword}”：{text}")
            )
            continue
        desc, keyword = _hit(ORANGE_KEYWORDS, text)
        if desc:
            if "呕吐" in desc:
                vomit_seen = True
            if "头痛" in desc:
                headache_seen = True
            level = max(level, TriageLevel.ORANGE)
            matched.append(
                MatchedRule("ORANGE_SYMPTOM", TriageLevel.ORANGE, desc, f"原话命中“{keyword}”：{text}")
            )
            continue
        desc, keyword = _hit(YELLOW_KEYWORDS, text)
        if desc:
            if desc == "头痛":
                headache_seen = True
            level = max(level, TriageLevel.YELLOW)
            matched.append(
                MatchedRule("YELLOW_SYMPTOM", TriageLevel.YELLOW, desc, f"原话命中“{keyword}”：{text}")
            )

    if vomit_seen and headache_seen:
        orange_via_vomit_and_headache = True
        level = max(level, TriageLevel.ORANGE)
        matched.append(
            MatchedRule(
                "HEADACHE_WITH_VOMITING",
                TriageLevel.ORANGE,
                "头痛合并呕吐",
                "登记内容同时包含头痛与呕吐",
            )
        )

    history_text = " ".join(risk_history)
    hit_factors: list[str] = []
    for desc, keywords in RISK_FACTOR_KEYWORDS.items():
        if any(keyword in history_text for keyword in keywords):
            hit_factors.append(desc)
    on_anticoagulant = any(
        any(keyword in (med or "") for keyword in ANTICOAGULANT_KEYWORDS) for med in medications
    )
    if on_anticoagulant:
        hit_factors.append("正在使用抗凝/抗血小板药物")

    if hit_factors and level < TriageLevel.RED:
        # 呕吐伴头痛本就是橙色；既往卒中史或抗凝药叠加任何头面部/神经症状抬到红色阈值
        neuro_seen = any(
            rule.code in {"ORANGE_SYMPTOM", "HEADACHE_WITH_VOMITING"} for rule in matched
        )
        serious_history = any("脑卒中" in factor or "抗凝" in factor for factor in hit_factors)
        if neuro_seen and serious_history:
            level = TriageLevel.RED
            matched.append(
                MatchedRule(
                    "RISK_FACTOR_ESCALATION",
                    TriageLevel.RED,
                    "神经症状叠加既往卒中史或抗凝用药",
                    "；".join(hit_factors),
                )
            )
        elif level < TriageLevel.YELLOW:
            level = TriageLevel.YELLOW
            matched.append(
                MatchedRule(
                    "RISK_FACTOR_ONLY", TriageLevel.YELLOW, "存在既往风险因素", "；".join(hit_factors)
                )
            )

    # 去重（同一条规则可能被多条原话命中多次），保持首次出现顺序
    unique: dict[str, MatchedRule] = {}
    for rule in matched:
        unique.setdefault(rule.code, rule)
    return TriageResult(level=level, matched=tuple(unique.values()))


#: 达到红色阈值后必须向现场（家属/目击人）传达的等待期间禁忌与照护动作。
LOCKED_GUIDANCE: tuple[str, ...] = (
    "保持患者呼吸道通畅：解开领口，昏迷或呕吐时将头部偏向一侧，及时清理呕吐物",
    "不要给患者喂水、喂食或喂任何药物（包括降压药、止痛药）",
    "不要随意搬动患者，尤其避免头部剧烈晃动；如需移动应整体平移",
    "让患者安静平卧，记录最后正常（发病）时间，等待急救人员",
    "如出现呼吸心跳停止，按调度员指导立即开始心肺复苏",
)

GUIDANCE_DISCLAIMER = "以上为系统按高危阈值自动发出的现场指导，不是诊断结论；专业人员可补充或调整判断。"
