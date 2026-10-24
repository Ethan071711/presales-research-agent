from datetime import date

import pytest

from presales import quotation
from presales.auth import Actor
from presales.selection import Household, select_packages

SALES=Actor('sales','demo-a','sales')
SKU='hyxi-three-e200-10-21.2'
LINE=[{'product_id':SKU,'quantity':1}]


@pytest.fixture(autouse=True)
def offer_date(monkeypatch):
    class CurrentDate(date):
        @classmethod
        def today(cls):
            return cls(2026,10,25)
    monkeypatch.setattr(quotation,'date',CurrentDate)


def test_public_offer_is_conditional_and_matches_exact_package():
    q=quotation.quote(LINE,SALES,budget=4000)
    assert q['status']=='reference_for_review'
    assert q['public_reference']['amount_aud']=='3157.00'
    assert q['public_reference']['source']['url']=='https://www.ozbargain.com.au/node/977206'
    assert q['public_reference']['availability']=='unconfirmed'
    assert q['total_aud'] is None and q['known_amount_aud'] is None
    assert q['budget_status']=='unverified' and q['requires_review']
    assert q['lines'][0]['amount_aud'] is None
    single=quotation.quote([{'product_id':'hyxi-single-8-21.2','quantity':1}],SALES)
    assert single['public_reference'] is None and single['status']=='awaiting_price'
    selected=select_packages(Household(phase='three',brand='HYXiPOWER'),SALES)
    assert [p['id'] for p in selected['candidates']]==[SKU]


def test_offer_cannot_survive_its_installation_window(monkeypatch):
    class Deadline(date):
        @classmethod
        def today(cls):
            return cls(2026,11,1)
    monkeypatch.setattr(quotation,'date',Deadline)
    q=quotation.quote(LINE,SALES)
    assert q['public_reference'] is None and q['status']=='awaiting_price'
    assert q['total_aud'] is None


def test_private_pricebook_has_priority_and_stays_tenant_scoped():
    price=quotation.PriceEntry(tenant='demo-a',product_id=SKU,currency='AUD',amount_cents=450000,
        scope='installed_system',gst_included=True,expires_on=date(2026,12,1),
        reference='TEST ONLY artificial supplier fixture')
    q=quotation.quote(LINE,SALES,pricebook=[price])
    assert q['total_aud']=='4500.00' and q['public_reference'] is None
    other=quotation.quote(LINE,Actor('other','demo-b','sales'),pricebook=[price])
    assert other['total_aud'] is None
    assert other['public_reference']['amount_aud']=='3157.00'
