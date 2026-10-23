"""AUD quote preparation. Unknown vendor prices remain unknown."""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from .auth import Actor

CATALOG=json.loads((Path(__file__).resolve().parents[1]/'data/catalog.json').read_text(encoding='utf-8'))


class PriceEntry(BaseModel):
    model_config=ConfigDict(extra='forbid')
    tenant: Literal['demo-a','demo-b']
    product_id: str
    currency: Literal['AUD']
    amount_cents: int = Field(strict=True,gt=0,le=100_000_000)
    scope: Literal['equipment_only','installed_system']
    gst_included: bool
    expires_on: date
    reference: str = Field(min_length=3,max_length=200)


def load_pricebook(path=None):
    if not path:
        return []
    entries=[PriceEntry.model_validate(p) for p in json.loads(Path(path).read_text(encoding='utf-8'))]
    ids={(p.tenant,p.product_id) for p in entries}
    if len(ids)!=len(entries) or any(p.product_id not in {c['id'] for c in CATALOG} for p in entries):
        raise ValueError('价格单含重复或不存在的套餐')
    return entries


def quote(items,actor:Actor,budget=None,pricebook=None):
    if actor.role not in {'sales','manager'}:
        raise PermissionError('当前身份不能生成询价/报价草稿')
    if len(items)!=1 or type(items[0].get('quantity')) is not int or items[0]['quantity']!=1:
        raise ValueError('一份家庭方案选择一个完整套餐，数量为1；不能直接相加多个整套系统')
    p=next((p for p in CATALOG if p['id']==items[0].get('product_id') and actor.can_read(p)),None)
    if p is None:
        raise ValueError('套餐不存在或无权访问')
    if p['selection_status']=='quarantined':
        raise ValueError('该套餐资料存在冲突或歧义，核实前不能生成报价')
    if budget is not None and (not Decimal(str(budget)).is_finite() or Decimal(str(budget))<0):
        raise ValueError('预算必须为非负有限金额')
    price=next((e for e in (pricebook or []) if e.product_id==p['id'] and e.tenant==actor.tenant),None)
    reasons=['需确认精确型号、安装勘测、备电配件及供应商正式报价；经理确认仅代表内部草稿审核。']
    if price and price.expires_on<date.today():
        reasons.append('已配置价格单过期，金额作废并重新询价。')
        price=None
    amount=f'{Decimal(price.amount_cents)/100:.2f}' if price else None
    complete=bool(price and price.scope=='installed_system' and price.gst_included)
    if not price:
        reasons.append('缺少有效供应商价格单，设备、安装、运输和税费待询价。')
    elif not complete:
        reasons.append('仅有设备价或未明确含税：完整安装总价仍待确认。')
    if budget is not None:
        if complete and Decimal(amount)>Decimal(str(budget)):
            reasons.append('已配置的完整安装价格超过客户预算。')
        elif not complete:
            reasons.append('完整价格未知，尚不能判断预算是否足够。')
    return {'currency':'AUD','status':'draft_for_review' if price else 'awaiting_price',
            'lines':[{'product_id':p['id'],'name':p['name'],'quantity':1,'amount_aud':amount}],
            'known_amount_aud':amount,'total_aud':amount if complete else None,
            'price_scope':price.scope if price else None,'gst_included':price.gst_included if price else None,
            'price_reference':price.reference if price else None,
            'price_expires_on':price.expires_on.isoformat() if price else None,
            'budget_status':('over_budget' if Decimal(amount)>Decimal(str(budget)) else 'within_configured_price') if complete and budget is not None else 'unverified',
            'requires_review':True,'review_reasons':reasons,'sources':p['sources'],
            'data_provenance':'public-battery-facts-2026-10-24; prices only from an explicitly configured tenant pricebook'}
