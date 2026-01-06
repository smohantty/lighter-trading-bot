import pytest
from decimal import Decimal
from src.strategy.types import Spread

class TestSpread:
    def test_spread_initialization(self):
        # Test initialization with float
        s1 = Spread(0.1)
        assert s1.value == Decimal("0.1")
        assert str(s1) == "Spread(0.1%)"

        # Test initialization with string
        s2 = Spread("0.25")
        assert s2.value == Decimal("0.25")
        assert str(s2) == "Spread(0.25%)"

        # Test initialization with Decimal
        s3 = Spread(Decimal("0.5"))
        assert s3.value == Decimal("0.5")
        assert str(s3) == "Spread(0.5%)"

    def test_markup(self):
        # 0.1% markup on 1000
        # 1000 * (1 + 0.001) = 1001
        spread = Spread(0.1)
        
        # Test with int
        assert spread.markup(1000) == Decimal("1001")
        
        # Test with float
        assert spread.markup(1000.0) == Decimal("1001")
        
        # Test with Decimal
        assert spread.markup(Decimal("1000")) == Decimal("1001")
        
        # Test with complex float
        # 45.672 * 1.001 = 45.717672
        assert spread.markup(45.672) == Decimal("45.717672")

        # 1% markup on 200
        # 200 * 1.01 = 202
        spread2 = Spread(1.0)
        assert spread2.markup(200) == Decimal("202")

    def test_markdown(self):
        # 0.1% markdown on 1000
        # 1000 * (1 - 0.001) = 999
        spread = Spread(0.1)
        
        # Test with int
        assert spread.markdown(1000) == Decimal("999")
        
        # Test with float
        assert spread.markdown(1000.0) == Decimal("999")
        
        # Test with Decimal
        assert spread.markdown(Decimal("1000")) == Decimal("999")
        
        # Test with complex float
        # 45.672 * 0.999 = 45.626328
        assert spread.markdown(45.672) == Decimal("45.626328")

        # 0.5% markdown on 100
        # 100 * (1 - 0.005) = 99.5
        spread2 = Spread(0.5)
        assert spread2.markdown(100) == Decimal("99.5")

    def test_precision(self):
        # Test with high precision values
        spread = Spread("0.12345")
        val = Decimal("10000")
        
        # Markup: 10000 * (1 + 0.0012345) = 10012.345
        assert spread.markup(val) == Decimal("10012.345")
        
        # Markdown: 10000 * (1 - 0.0012345) = 9987.655
        assert spread.markdown(val) == Decimal("9987.655")
