"""Deterministic preliminary filtering of advertised household packages."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from .quotation import CATALOG


class Household(BaseModel):
    model_config = ConfigDict(extra='forbid')
    phase: Literal['single','three','unknown'] = 'unknown'
    min_capacity_kwh: float = Field(default=20,gt=0,le=100,allow_inf_nan=False)
    min_package_power_kw: float = Field(default=5,gt=0,le=30,allow_inf_nan=False)
    brand: Literal['any','FoxESS','Sigenergy','Tesla','Growatt','HYXiPOWER','GoodWe'] = 'any'
    existing_solar: bool = False
    existing_inverter: str = Field(default='',max_length=120)
    backup_required: bool = False
    budget_aud: float | None = Field(default=None,ge=0,allow_inf_nan=False)


def select_packages(home: Household, actor):
    visible=[p for p in CATALOG if actor.can_read(p)]
    eligible=[p for p in visible if p['selection_status']=='listed_package']
    questions,review=[],['相制和功率仅按公开套餐标签初筛；具体型号、接线和安装条件由安装方复核。',
                         '公开促销仅作有条件参考：预算尚未验证，需要取得有效价格单。',
                         '容量口径未注明，尚未核实可用电量；不承诺备电时长。']
    if home.phase=='unknown':
        questions.append('请确认电表/供电为单相还是三相，再进行套餐初筛。')
    if home.existing_solar:
        review.append('已有光伏改造：核对旧逆变器型号 '+(home.existing_inverter or '（尚未提供）')+'、耦合方案和厂商兼容表。')
    if home.backup_required:
        review.append('备电需求：另核实网关/切换设备、备电回路和负载；套餐功率不等于已验证的离网输出。')
    candidates=[p for p in eligible if home.phase!='unknown' and p['phase']==home.phase
                and p['advertised_capacity_kwh']>=home.min_capacity_kwh
                and p['advertised_power_kw']>=home.min_package_power_kw
                and (home.brand=='any' or p['brand']==home.brand)]
    candidates.sort(key=lambda p:(p['advertised_capacity_kwh'],p['advertised_power_kw'],p['id']))
    if not candidates and not questions:
        review.append('公开目录没有满足全部条件的套餐；需询问供应商，不自动拼接跨品牌设备。')
    return {'status':'clarification_needed' if questions else ('preliminary_candidates' if candidates else 'no_match'),
            'candidates':candidates,'clarifications':questions,'review_reasons':review,
            'quarantined_packages':sum(p['selection_status']=='quarantined' for p in visible),
            'budget_status':'unverified','requires_review':True,'profile':home.model_dump()}
