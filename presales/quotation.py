"""Deterministic, integer-fen calculation. LLMs cannot set tenant, role, or prices."""
import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from .auth import Actor

CATALOG = json.loads((Path(__file__).resolve().parents[1]/'data/catalog.json').read_text(encoding='utf-8'))


def quote(items, actor: Actor, discount='1.00', budget=None):
    if actor.role not in {'sales', 'manager'}:
        raise PermissionError('当前身份不能生成报价')
    rate = Decimal(str(discount))
    if not rate.is_finite() or not Decimal('0.50') <= rate <= 1:
        raise ValueError('折扣必须在0.50到1.00之间')
    if not 1 <= len(items) <= 20:
        raise ValueError('报价需要1到20个产品行')
    visible = {p['id']: p for p in CATALOG if actor.can_read(p)}
    seen, lines, reasons = set(), [], []
    for item in items:
        sku, quantity = item.get('product_id'), item.get('quantity')
        if sku in seen:
            raise ValueError('相同产品请合并数量')
        seen.add(sku)
        if sku not in visible:
            raise ValueError('产品不存在或无权访问')
        if type(quantity) is not int or not 1 <= quantity <= 100:
            raise ValueError('数量必须为1到100的整数')
        product = visible[sku]
        amount_fen = int((Decimal(product['unit_price'])*100*quantity*rate).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        lines.append({'product_id': sku, 'name': product['name'], 'quantity': quantity,
                      'unit_price_fen': product['unit_price']*100, 'amount_fen': amount_fen})
        if quantity > product['stock']:
            reasons.append('超出现有模拟库存，需确认交期')
    total = sum(line['amount_fen'] for line in lines)
    if actor.role == 'sales' and rate < Decimal('.95'):
        reasons.append('销售折扣低于95折，需经理批准')
    if total > 5000000:
        reasons.append('含税总额超过50000元，需人工审核')
    if budget is not None:
        limit = Decimal(str(budget))
        if not limit.is_finite() or limit < 0:
            raise ValueError('预算必须是非负有限金额')
        if Decimal(total)/100 > limit:
            reasons.append('报价超出客户预算，需重新确认')
    skus = {line['product_id'] for line in lines}
    if 'DEMO-SW-B' in skus and any(s.startswith('DEMO-AP-') for s in skus):
        reasons.append('基础交换机不供PoE，接入点需额外电源或更换交换机')
    return {'currency': 'CNY', 'total_fen': total, 'total_yuan': f'{Decimal(total)/100:.2f}',
            'discount': str(rate), 'lines': lines, 'requires_review': bool(reasons),
            'review_reasons': list(dict.fromkeys(reasons)), 'valid_days': 7,
            'data_provenance': 'new-synthetic-2026-10-24', 'status': 'draft'}
